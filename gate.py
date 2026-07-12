"""Deterministic action gate.

Whether a tool is gated is a pure table lookup on the tool name — the model
never decides. Gated actions require owner approval (Telegram card or CLI
prompt) unless the task was pre-authorized with a leading "!".
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal

from notify import Approver, ToolContext
from state import EventLog

# Autonomous (documented for clarity; anything NOT in GATED_TOOLS is allowed):
#   fs_read_file, fs_list_dir, fs_write_file (workspace-sandboxed),
#   browser_goto, browser_extract, browser_screenshot,
#   social_search, social_get_feed, social_get_profile, send_social_digest
GATED_TOOLS = {
    "social_post",
    "fs_delete",
    "browser_fill_and_submit",
}

Reason = Literal["autonomous", "pre_authorized", "owner_approved", "owner_denied", "timeout"]


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: Reason


def build_card_text(tool_name: str, args: dict) -> tuple[str, str, str]:
    """(platform, action, payload) shown to the owner — the EXACT pending action."""
    if tool_name == "social_post":
        platform = str(args.get("platform", "?"))
        payload = str(args.get("text", ""))
        if args.get("media_path"):
            payload += f"\n[media: {args['media_path']}]"
        return platform, f"Post to {platform.upper()}", payload
    if tool_name == "fs_delete":
        return "filesystem", "Delete file", str(args.get("path", "?"))
    if tool_name == "browser_fill_and_submit":
        fields = args.get("fields", {})
        lines = "\n".join(f"{k}: {v}" for k, v in dict(fields).items()) if fields else ""
        return "browser", "Submit form", f"URL: {args.get('url', '?')}\n{lines}".strip()
    return "unknown", tool_name, str(args)


class Gate:
    def __init__(self, approver: Approver, log: EventLog, timeout_s: int = 300):
        self.approver = approver
        self.log = log
        self.timeout_s = timeout_s

    @staticmethod
    def is_gated(tool_name: str) -> bool:
        return tool_name in GATED_TOOLS

    async def _request_approval(self, *, tool_name: str, platform: str, action: str,
                                payload: str, ctx: ToolContext, timeout_s: int,
                                log_args: dict | None) -> tuple[bool, Reason]:
        """Shared request+log flow used by both check() and check_custom()."""
        self.log.append("approval_requested", task_id=ctx.task_id, tool=tool_name,
                        platform=platform, payload=payload)
        verdict = await self.approver.request_approval(
            platform=platform, action=action, payload_text=payload,
            meta={"tool": tool_name, "task_id": ctx.task_id,
                 **({"args": log_args} if log_args is not None else {})},
            timeout_s=timeout_s,
        )
        if verdict is True:
            reason: Reason = "owner_approved"
        elif verdict is False:
            reason = "owner_denied"
        else:
            reason = "timeout"
        allowed = verdict is True
        self.log.append("approval_resolved", task_id=ctx.task_id, tool=tool_name,
                        allowed=allowed, reason=reason)
        extra = {"args": log_args} if log_args is not None else {}
        self.log.append("gate_decision", task_id=ctx.task_id, tool=tool_name,
                        allowed=allowed, reason=reason, **extra)
        return allowed, reason

    async def check(self, tool_name: str, args: dict, ctx: ToolContext) -> GateDecision:
        if not self.is_gated(tool_name):
            self.log.append("gate_decision", task_id=ctx.task_id, tool=tool_name,
                            allowed=True, reason="autonomous")
            return GateDecision(True, "autonomous")

        if ctx.pre_authorized:
            self.log.append("gate_decision", task_id=ctx.task_id, tool=tool_name,
                            allowed=True, reason="pre_authorized", args=args)
            return GateDecision(True, "pre_authorized")

        platform, action, payload = build_card_text(tool_name, args)
        allowed, reason = await self._request_approval(
            tool_name=tool_name, platform=platform, action=action, payload=payload,
            ctx=ctx, timeout_s=self.timeout_s, log_args=args,
        )
        return GateDecision(allowed, reason)

    async def check_custom(self, action_label: str, payload_text: str, ctx: ToolContext,
                           timeout_s: int | None = None) -> bool:
        """For actions with no ToolSpec of their own — e.g. a callback fired from
        inside a nested agent (browser-use's own reasoning loop) — that must
        ALWAYS request approval. No is_gated table lookup, and deliberately no
        `pre_authorized` bypass either: this is for consequential one-shot
        actions (e.g. actually submitting a job application) where even a
        `!`-prefixed task must not skip the final human checkpoint.

        Callers must pass an already-redacted `payload_text` — this method
        does not know how to redact sensitive values, and (unlike `check()`)
        never logs a raw `args` dict, only the caller-provided text."""
        allowed, _reason = await self._request_approval(
            tool_name=action_label, platform="custom", action=action_label,
            payload=payload_text, ctx=ctx, timeout_s=timeout_s or self.timeout_s,
            log_args=None,
        )
        return allowed


def card_html(action: str, platform: str, payload: str) -> str:
    """Telegram HTML approval card (used by bridge.py)."""
    return (
        f"🔐 <b>Approval required</b>\n"
        f"<b>{html.escape(action)}</b> — platform: <code>{html.escape(platform)}</code>\n\n"
        f"<pre>{html.escape(payload[:3500])}</pre>"
    )
