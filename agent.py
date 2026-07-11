"""Pocket Agent core loop: Ollama tool-calling agent over the shared tool registry."""
import asyncio
import json
import sys

import ollama

import config
import tools_fs

SYSTEM_PROMPT = f"""You are Jerry, an AI assistant co-driving the user's real Microsoft Edge session on their behalf.

Environment: Windows. The user's home directory is {config.SANDBOX_ROOT} and their Desktop is {config.DESKTOP}. File access only works inside {config.SANDBOX_ROOT} — always use these exact absolute paths. Never use placeholders like %USERNAME% or ~, and never guess a username (the name in the profile is NOT the Windows username).

Not every message is a work task. If the user is greeting you, chatting, or asking about you or your abilities ("what can you do?", "who are you?"), reply directly and naturally WITHOUT calling any tools. When asked what you can do, describe it plainly: from their phone you can work their PC (create/organize files, open apps, screenshots, volume/brightness/media, lock — sleep/shutdown need their approval), drive their real Edge browser (research with sources, scrape data, fill forms and job applications from their profile and uploaded resume — anything that submits/sends/pays always shows an approval card on their phone first), read and act on their Gmail (send/reply/draft are approval-gated), search Reddit and YouTube, run long multi-site web missions, remember facts and files they send, run scheduled recurring tasks, and take voice notes. Be honest about limits: you never solve CAPTCHAs, never handle their passwords in plain text, and high-impact actions always wait for their approval.

Rules:
- Always act through the provided tools. Never merely describe what you would do — do it.
- Never invent personal data (names, emails, addresses, card numbers). Call read_profile to get the user's real details.
- If required information is missing or the request is ambiguous, call ask_user to ask the user directly.
- Navigation, research, scraping, drafting, and form filling are autonomous. SUBMITTING, SENDING, POSTING, PURCHASING, BOOKING, PAYING, DELETING, or changing an account triggers a system-enforced approval on the user's phone. A denial must never be retried.
- Never ask the user for permission yourself before a risky action — just attempt it; the system automatically asks the user to approve. Only use ask_user when information is genuinely missing.
- For passwords or one-time codes, call request_secret. It returns an opaque handle; pass that handle to fill_secret_field. Never ask for credentials with ask_user and never echo a secret handle unnecessarily.
- NEVER invent or fabricate data to fill a field. If a required field's value is unknown, or a choice is ambiguous, call ask_user. If the user gives you a fact worth reusing (e.g. work authorization, a phone number), save it with remember_fact.
- When you cannot complete a task (a site blocks you, a page won't load, info is missing), REPORT the blockage plainly. NEVER create a substitute artifact (a fake sample file, made-up results) and present it as if the task was done.
- CAPTCHAs / "verify you are human" / bot checks: a browser tool may return a STOP message about a verification challenge. Do NOT try to solve or bypass it. Call ask_user to have the user complete it themselves, then continue. A paused task is correct here, not a failure.
- The user often has a page open already; a task may say "(The user is currently looking at this page: URL)". Act on THAT tab: use browser_goto with that URL (it targets the open tab), or list_tabs / switch_tab. Open a new_tab only when the task needs a different page.
- To find information on the web: browser_goto "https://duckduckgo.com/html/?q=your+search+terms", then read_page for the results, then click_element or browser_goto a promising result link. Base answers on what you actually read, and include the source URL.
- For research or itinerary work, use scrape_page when structured results are useful. Use JSON format for nested data; row data defaults to CSV. Open new tabs as needed and preserve source URLs.
- If ordinary DOM controls are insufficient, use visual_inspect and visual_click. Opaque coordinate clicks are approval-gated. Never use vision on a CAPTCHA or verification challenge. Color/paint swatches, icon-only buttons, and image pickers usually have NO text label in the DOM — go straight to visual_inspect ("the red paint swatch") for those instead of guessing at unlabeled elements.
- After changing a selectable option, verify the requested option is reported as checked/selected before finishing. Never claim success from a click alone.
- On large/dynamic pages, use find_elements to search controls omitted from a truncated digest and choose_option to select an exact semantic radio/checkbox choice. If the requested choice does not exist or is ambiguous, ask the user; never silently choose the closest label.
- For web forms: browser_goto the page, use the field digest to fill_field/select_option each field from the profile (call read_profile first); for file/attachment fields use pick_file to locate the file then upload_file; click the submit button LAST (the system shows the user a full breakdown and asks them to approve it).
- Files the user sent from their phone are saved permanently. Use list_uploads to see them (newest first, with paths). If a task mentions "(Most recently uploaded file: ...)" or "(Uploaded file available at: ...)", that note tells you what "it"/"this file"/"the file I sent" refers to — use that path directly rather than asking which file. NEVER ask the user for a file path until you have checked list_uploads AND read_profile (resume_path) first — the file they mean is almost always already on disk.
- Keep going until the task is done or truly blocked; work step by step.
- When finished, reply the way a sharp, friendly personal assistant would text the user: plain, natural, concise. Lead with the answer. Short bullets only when listing things; otherwise a sentence or two. No markdown bold/headers, no preamble like "Here's a summary of...", no meta about steps or tools. Just the useful result.""" + ("""
- For a LONG autonomous web mission (multi-site research or comparison, a many-page flow like a full job application) you may delegate ONCE to web_agent with a complete task description — it drives the same Edge for several minutes and returns a sourced summary, and its submit-like clicks are phone-approval-gated the same as yours. For the user's current tab, quick lookups, or precise single-page work, use the direct browser tools instead. Never call web_agent for something a few direct tool calls can do.""" if config.BROWSER_USE_ENABLED else "")


async def _default_status(step: str):
    print(f"[step] {step}")


async def _default_ask(question: str) -> str:
    return await asyncio.to_thread(input, f"[agent asks] {question}\n> ")


async def _default_confirm(summary: str) -> bool:
    ans = await asyncio.to_thread(input, f"[confirm] {summary}\nApprove? y/n: ")
    return ans.strip().lower() in ("y", "yes")


def _short(args: dict, limit: int = 80) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def run_task(
    text: str,
    *,
    status_cb=None,
    ask_user_cb=None,
    confirm_cb=None,
    request_secret_cb=None,
    audit_cb=None,
    extra_tools: dict | None = None,
    history: str = "",
) -> str:
    status_cb = status_cb or _default_status
    ask_user_cb = ask_user_cb or _default_ask
    confirm_cb = confirm_cb or _default_confirm

    tools_fs.configure(confirm=confirm_cb)

    async def _ask_user(args: dict) -> str:
        try:
            return str(await ask_user_cb(str(args.get("question", ""))))
        except Exception as e:
            return f"Error: {e}"

    async def _request_confirmation(args: dict) -> str:
        try:
            ok = await confirm_cb(str(args.get("summary", "")))
            return "approved" if ok else "denied"
        except Exception as e:
            return f"Error: {e}"

    async def _request_secret(args: dict) -> str:
        if request_secret_cb is None:
            return "Error: Telegram secret entry is not configured."
        try:
            return str(await request_secret_cb(
                str(args.get("question", "Enter the requested secret")),
                kind=str(args.get("kind", "secret")),
                domain=str(args.get("domain", "")),
            ))
        except Exception as e:
            return f"Error: {e}"

    registry: dict[str, dict] = dict(tools_fs.TOOLS)
    if extra_tools:
        registry.update(extra_tools)
    registry["ask_user"] = {
        "schema": {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": "Ask the user a question on their phone and wait for their answer. "
                "Use when information is missing or the request is ambiguous.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question to ask"}
                    },
                    "required": ["question"],
                },
            },
        },
        "fn": _ask_user,
    }
    registry["request_confirmation"] = {
        "schema": {
            "type": "function",
            "function": {
                "name": "request_confirmation",
                "description": "Ask the user to approve a risky action (submit, send, purchase, delete). "
                "Returns 'approved' or 'denied'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "One-line summary of the action"}
                    },
                    "required": ["summary"],
                },
            },
        },
        "fn": _request_confirmation,
    }
    registry["request_secret"] = {
        "schema": {
            "type": "function",
            "function": {
                "name": "request_secret",
                "description": "Request a password or one-time code from Telegram. Returns an opaque handle for fill_secret_field; the raw value is never shown to you.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "kind": {"type": "string", "enum": ["password", "otp", "secret"]},
                        "domain": {"type": "string"},
                    },
                    "required": ["question", "kind", "domain"],
                },
            },
        },
        "fn": _request_secret,
    }

    client = ollama.AsyncClient(host=config.OLLAMA_HOST)
    # History goes INSIDE the one system message — chat templates commonly drop
    # or mangle a second system message, which silently kills the context feature.
    system = SYSTEM_PROMPT
    if history:
        system += ("\n\nRecent tasks in this session (the user may refer back to these, "
                   "e.g. 'delete it' means the thing just created):\n" + history)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    schemas = [t["schema"] for t in registry.values()]

    last_failed_key = None
    for step in range(config.MAX_STEPS):
        try:
            resp = await asyncio.wait_for(
                client.chat(
                    model=config.MODEL,
                    messages=messages,
                    tools=schemas,
                    options={"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE},
                    keep_alive=config.KEEP_ALIVE,
                ),
                timeout=config.MODEL_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return (f"INCOMPLETE: The model took longer than {config.MODEL_CALL_TIMEOUT}s to respond "
                    f"at step {step + 1} — task aborted. (Check GPU load / ollama ps.)")
        msg = resp["message"]
        msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        messages.append(msg_dict)

        tool_calls = msg_dict.get("tool_calls") or []
        if not tool_calls:
            return msg_dict.get("content") or ""

        for tc in tool_calls:
            fn_info = tc["function"] if isinstance(tc, dict) else tc.function
            name = fn_info["name"] if isinstance(fn_info, dict) else fn_info.name
            args = fn_info["arguments"] if isinstance(fn_info, dict) else fn_info.arguments
            if not isinstance(args, dict):
                try:
                    args = json.loads(args) if args else {}
                except Exception:
                    args = {}

            await status_cb(f"{name}({_short(args)})")
            if audit_cb is not None:
                await audit_cb("tool", "start", {"name": name, "args": args})
            if name not in registry:
                result = f"Error: no tool named {name}. Available: {', '.join(registry)}"
            else:
                try:
                    result = await registry[name]["fn"](args)
                except Exception as e:
                    result = f"Error: {e}"
            result = str(result)
            if len(result) > config.TOOL_RESULT_MAX:
                result = result[: config.TOOL_RESULT_MAX] + "\n…(truncated)"

            # detect the model repeating an identical failing call
            key = (name, json.dumps(args, sort_keys=True, default=str))
            if result.startswith("Error"):
                if key == last_failed_key:
                    result = (
                        "You already tried this and it failed. "
                        "Try a different approach or use ask_user."
                    )
                else:
                    last_failed_key = key
            else:
                last_failed_key = None

            if audit_cb is not None:
                await audit_cb(
                    "tool", "finish",
                    {"name": name, "args": args, "result": result[:500]},
                    ok=not result.startswith("Error"),
                )

            messages.append({"role": "tool", "tool_name": name, "content": result})

    return (
        f"INCOMPLETE: Stopped after {config.MAX_STEPS} steps without finishing. Steps taken: "
        + "; ".join(
            m.get("tool_name", "?") for m in messages if m.get("role") == "tool"
        )
    )


async def warm_up():
    """One trivial chat call so the model is loaded and kept warm."""
    client = ollama.AsyncClient(host=config.OLLAMA_HOST)
    await client.chat(
        model=config.MODEL,
        messages=[{"role": "user", "content": "Say ready"}],
        options={"num_ctx": config.NUM_CTX},
        keep_alive=config.KEEP_ALIVE,
    )


async def ollama_ready() -> tuple[bool, str]:
    """Check that Ollama is reachable and the configured model is pulled."""
    try:
        client = ollama.AsyncClient(host=config.OLLAMA_HOST)
        resp = await client.list()
        names = [m.model for m in resp.models]
    except Exception as e:
        return False, f"Ollama not reachable at {config.OLLAMA_HOST}: {e}"
    base = config.MODEL.split(":")[0]
    if any(n == config.MODEL or n.split(":")[0] == base for n in names):
        return True, f"Ollama up, model {config.MODEL} available."
    return False, (
        f"Ollama up but model {config.MODEL} not pulled. "
        f"Run: ollama pull {config.MODEL}. Installed: {', '.join(names) or '(none)'}"
    )


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        print('usage: python agent.py "task text"')
        sys.exit(1)
    result = asyncio.run(run_task(argv[0]))
    print(result)
