"""Ollama tool loop.

Hardened for small local models (qwen2.5/llama3.1 class):
- lenient tool-call extraction (stringified arguments, JSON emitted in content)
- unknown-tool and malformed-call recovery via corrective tool results / nudges
- repeat-call guard and iteration cap
- every tool exception becomes an "ERROR: ..." tool result, never a crash
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import ollama

from config import Config
from gate import Gate
from notify import Notifier, ToolContext
from state import EventLog

TOOL_TIMEOUT_S = 120
MAX_NUDGES = 2
REPEAT_LIMIT = 3

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_WORD_BEFORE_BRACE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[\s:\-]*$")


def _find_balanced_json_objects(text: str) -> list[tuple[int, int]]:
    """Spans of top-level {...} objects, tolerant of junk characters around them."""
    spans = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append((start, i + 1))
    return spans


@dataclass
class ToolSpec:
    name: str
    func: Callable[..., Awaitable[str]]  # async, returns a string ALWAYS
    schema: dict  # {"type": "function", "function": {...}}
    gated: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def ollama_tools(self) -> list[dict]:
        return [spec.schema for spec in self._tools.values()]


@dataclass
class ToolCall:
    name: str
    arguments: dict


def _coerce_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _call_from_obj(obj: dict) -> ToolCall | None:
    """Match {"name": ..., "arguments"|"parameters": {...}} shapes."""
    if not isinstance(obj, dict):
        return None
    inner = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = inner.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = inner.get("arguments", inner.get("parameters", {}))
    return ToolCall(name=name, arguments=_coerce_arguments(args))


def extract_tool_calls(message: Any) -> tuple[list[ToolCall], bool]:
    """(calls, malformed) — lenient extraction from an Ollama response message.

    malformed=True means the model clearly attempted a tool call but it could
    not be parsed (caller should nudge).
    """
    tool_calls = getattr(message, "tool_calls", None) or (
        message.get("tool_calls") if isinstance(message, dict) else None
    )
    if tool_calls:
        calls = []
        for tc in tool_calls:
            fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
            if fn is None:
                continue
            name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None)
            raw_args = getattr(fn, "arguments", None)
            if raw_args is None and isinstance(fn, dict):
                raw_args = fn.get("arguments")
            if name:
                calls.append(ToolCall(name=name, arguments=_coerce_arguments(raw_args)))
        if calls:
            return calls, False

    content = getattr(message, "content", None) or (
        message.get("content") if isinstance(message, dict) else ""
    ) or ""

    # Fallback: model wrote the tool call as JSON in content
    candidates = []
    stripped = content.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    candidates += _JSON_BLOCK.findall(content)
    attempted = False
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            if '"name"' in cand or '"function"' in cand:
                attempted = True
            continue
        call = _call_from_obj(obj)
        if call:
            return [call], False
        if isinstance(obj, dict) and ("name" in obj or "function" in obj):
            attempted = True

    # Fallback: model wrapped the call in junk, e.g. ")(((tool_name {...})))"
    # — find any balanced {...} substring and treat the bare word right
    # before it as the tool name, with the object itself as the arguments.
    for start, end in _find_balanced_json_objects(content):
        try:
            obj = json.loads(content[start:end])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        call = _call_from_obj(obj)
        if call:
            return [call], False
        match = _WORD_BEFORE_BRACE.search(content[:start])
        if match:
            return [ToolCall(name=match.group(1), arguments=obj)], False
        attempted = True

    return [], attempted


def _msg_content(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return content or ""


SYSTEM_PROMPT = """You are JerryAI, a personal assistant agent running on the owner's PC, \
commanded via Telegram. Use the available tools to complete the owner's task, then give a \
short final answer.

Rules:
- Call tools with valid arguments; use one tool at a time when unsure.
- For social media searches use social_search; for the owner's feeds use social_get_feed.
- After gathering social results, use send_social_digest to deliver them nicely.
- If a tool returns an ERROR or says a platform is not set up, tell the owner plainly — do not retry the same call.
- Posting and deleting require owner approval; if a result says DENIED, accept it and report back.
- Keep final answers short and factual.{profile}"""


def build_system_prompt(profile: dict) -> str:
    extra = ""
    if profile:
        owner = profile.get("owner", {})
        social = profile.get("social", {})
        persona = (profile.get("agent") or {}).get("persona")
        bits = []
        if owner.get("name"):
            bits.append(f"Owner: {owner['name']}")
        if owner.get("interests"):
            bits.append("Interests: " + ", ".join(map(str, owner["interests"])))
        handles = {p: v.get("handle") for p, v in social.items()
                   if isinstance(v, dict) and v.get("handle")}
        if handles:
            bits.append("Handles: " + ", ".join(f"{p}=@{h}" for p, h in handles.items()))
        if persona:
            bits.append(f"Style: {persona}")
        if bits:
            extra = "\n\nOwner profile: " + "; ".join(bits)
    return SYSTEM_PROMPT.format(profile=extra)


class Agent:
    def __init__(self, cfg: Config, registry: ToolRegistry, gate: Gate,
                 log: EventLog, profile: dict | None = None,
                 client: Any | None = None):
        self.cfg = cfg
        self.registry = registry
        self.gate = gate
        self.log = log
        self.profile = profile or {}
        self.client = client or ollama.AsyncClient(host=cfg.ollama_host)

    async def run_task(self, task_text: str, ctx: ToolContext) -> str:
        self.log.append("task_received", task_id=ctx.task_id, text=task_text,
                        pre_authorized=ctx.pre_authorized)
        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(self.profile)},
            {"role": "user", "content": task_text},
        ]
        nudges = 0
        call_counts: dict[str, int] = {}

        for iteration in range(self.cfg.max_agent_iterations):
            self.log.append("agent_iteration", task_id=ctx.task_id, n=iteration)
            resp = await self.client.chat(
                model=self.cfg.ollama_model,
                messages=messages,
                tools=self.registry.ollama_tools(),
                options={"num_ctx": self.cfg.ollama_num_ctx},
            )
            message = resp["message"] if isinstance(resp, dict) else resp.message
            calls, malformed = extract_tool_calls(message)

            if not calls:
                if malformed and nudges < MAX_NUDGES:
                    nudges += 1
                    messages.append({"role": "user", "content":
                                     "Your last tool call was malformed JSON. Emit a valid "
                                     "tool call, or give your final answer as plain text."})
                    continue
                final = _msg_content(message).strip() or "(no answer produced)"
                self.log.append("task_done", task_id=ctx.task_id, answer=final[:500])
                return final

            # echo the assistant turn (tool calls included) back into history
            messages.append({
                "role": "assistant",
                "content": _msg_content(message),
                "tool_calls": [
                    {"function": {"name": c.name, "arguments": c.arguments}} for c in calls
                ],
            })

            for call in calls:
                result = await self._handle_call(call, ctx, call_counts)
                messages.append({"role": "tool", "content": result, "tool_name": call.name})

        self.log.append("task_done", task_id=ctx.task_id, answer="(iteration cap)")
        return ("I hit my step limit before finishing. Partial progress is logged; "
                "try a more specific request.")

    async def _handle_call(self, call: ToolCall, ctx: ToolContext,
                           call_counts: dict[str, int]) -> str:
        spec = self.registry.get(call.name)
        if spec is None:
            self.log.append("error", task_id=ctx.task_id, tool=call.name, err="unknown tool")
            return (f"ERROR: unknown tool '{call.name}'. Available tools: "
                    f"{', '.join(self.registry.names())}")

        sig = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
        call_counts[sig] = call_counts.get(sig, 0) + 1
        if call_counts[sig] > REPEAT_LIMIT:
            return ("ERROR: you already called this tool with these exact arguments. "
                    "Use the earlier result or give your final answer.")

        self.log.append("tool_call", task_id=ctx.task_id, tool=call.name, args=call.arguments)

        decision = await self.gate.check(call.name, call.arguments, ctx)
        if not decision.allowed:
            if decision.reason == "timeout":
                return "DENIED: approval timed out — the action was cancelled."
            return "DENIED by owner: do not retry this action; report back instead."

        try:
            result = await asyncio.wait_for(
                spec.func(ctx, **call.arguments), timeout=TOOL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            self.log.append("error", task_id=ctx.task_id, tool=call.name, err="timeout")
            result = f"ERROR: {call.name} timed out after {TOOL_TIMEOUT_S}s"
        except TypeError as e:
            # bad/missing arguments from the model — recoverable
            self.log.append("error", task_id=ctx.task_id, tool=call.name, err=str(e))
            result = f"ERROR: bad arguments for {call.name}: {e}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log.append("error", task_id=ctx.task_id, tool=call.name,
                            err=f"{type(e).__name__}: {e}")
            result = f"ERROR: {type(e).__name__}: {e}"

        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        self.log.append("tool_result", task_id=ctx.task_id, tool=call.name, result=result)
        return result
