"""Notifier/Approver protocols + ToolContext — the seam between tools and the owner.

Tools never import Telegram code. They receive a ToolContext whose `notifier`
is either the console implementation below (CLI mode, tests) or the Telegram
implementation in bridge.py.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from config import Config
    from gate import Gate
    from job_store import JobStore
    from social.base import SocialAdapter
    from state import CooldownStore, EventLog, SearchCache


class Notifier(Protocol):
    async def send_text(self, text: str) -> None: ...
    async def send_photos(self, paths: list[Path], caption: str = "") -> None: ...


class Approver(Protocol):
    async def request_approval(
        self, *, platform: str, action: str, payload_text: str, meta: dict, timeout_s: int
    ) -> bool | None:
        """True=approved, False=denied, None=timed out."""
        ...


@dataclass
class ToolContext:
    """Everything a tool needs, injected once at registration time."""

    cfg: "Config"
    log: "EventLog"
    notifier: Notifier
    cooldowns: "CooldownStore"
    cache: "SearchCache"
    adapters: dict[str, "SocialAdapter"] = field(default_factory=dict)
    task_id: str = ""
    pre_authorized: bool = False
    job_store: "JobStore | None" = None
    gate: "Gate | None" = None


def _safe_print(text: str) -> None:
    # Windows consoles may be cp1252; never crash on emoji in output.
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


class CLINotifier:
    """Console notifier + y/N approver for `main.py --cli` and tests."""

    async def send_text(self, text: str) -> None:
        _safe_print(text)

    async def send_photos(self, paths: list[Path], caption: str = "") -> None:
        for p in paths:
            _safe_print(f"[photo] {p}" + (f" — {caption}" if caption else ""))

    async def request_approval(
        self, *, platform: str, action: str, payload_text: str, meta: dict, timeout_s: int
    ) -> bool | None:
        card = (
            f"\n{'=' * 60}\n"
            f"APPROVAL REQUIRED — {action} on {platform}\n"
            f"{'-' * 60}\n"
            f"{payload_text}\n"
            f"{'=' * 60}"
        )
        _safe_print(card)
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(input, "Approve? [y/N] "), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            return None
        return answer.strip().lower() in ("y", "yes")


class AutoApproveNotifier(CLINotifier):
    """Approves everything — for `main.py --cli-yes` and tests only."""

    async def request_approval(self, **kwargs) -> bool | None:
        _safe_print(f"[auto-approved] {kwargs.get('action')} on {kwargs.get('platform')}")
        return True
