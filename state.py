"""Persistent state: audit log, per-platform cooldowns, short-lived search cache.

All classes are thread-safe — adapter calls run in worker threads while the
agent loop runs on the asyncio loop.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

EVENT_KINDS = {
    "task_received",
    "agent_iteration",
    "tool_call",
    "tool_result",
    "gate_decision",
    "approval_requested",
    "approval_resolved",
    "adapter_path",
    "cooldown_block",
    "error",
    "task_done",
}

_RESULT_TRUNCATE = 500


class EventLog:
    """Append-only JSONL audit log (state/events.jsonl)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, **fields) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        if "result" in fields and isinstance(fields["result"], str):
            fields["result"] = fields["result"][:_RESULT_TRUNCATE]
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": kind,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()


class CooldownStore:
    """Sliding-window rate limits per platform, persisted to JSON.

    Only platforms registered via `configure()` are limited; everything else
    passes through (official APIs enforce their own limits).
    """

    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._limits: dict[str, tuple[int, float]] = {}  # platform -> (per_hour, min_delay_s)
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)

    def configure(self, platform: str, per_hour: int, min_delay_s: float = 0.0) -> None:
        self._limits[platform] = (per_hour, min_delay_s)

    def check(self, platform: str) -> tuple[bool, float]:
        """(allowed, wait_seconds). Unlimited platforms are always allowed."""
        if platform not in self._limits:
            return True, 0.0
        per_hour, min_delay = self._limits[platform]
        now = self._clock()
        with self._lock:
            entry = self._data.get(platform, {})
            stamps = [t for t in entry.get("timestamps", []) if now - t < 3600]
            entry["timestamps"] = stamps
            self._data[platform] = entry
            if len(stamps) >= per_hour:
                return False, 3600 - (now - stamps[0])
            last = entry.get("last_action", 0)
            if now - last < min_delay:
                return False, min_delay - (now - last)
            return True, 0.0

    def record(self, platform: str) -> None:
        now = self._clock()
        with self._lock:
            entry = self._data.setdefault(platform, {})
            entry.setdefault("timestamps", []).append(now)
            entry["last_action"] = now
            self._save()


class SearchCache:
    """In-memory TTL cache for identical social searches (default 10 min)."""

    def __init__(self, ttl_s: float = 600.0, clock=time.time):
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, object]] = {}

    @staticmethod
    def key(platform: str, method: str, query: str, limit: int) -> str:
        return f"{platform}:{method}:{query.strip().lower()}:{limit}"

    def get(self, key: str):
        now = self._clock()
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            ts, value = hit
            if now - ts > self.ttl_s:
                del self._store[key]
                return None
            return value

    def put(self, key: str, value) -> None:
        with self._lock:
            self._store[key] = (self._clock(), value)
