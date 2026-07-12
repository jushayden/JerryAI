"""Applied-jobs dedup persistence.

Tracks whether `apply_to_job` has already submitted an application for a given
URL, so a re-run (or a duplicate task) doesn't apply twice. Follows the same
tolerant-load JSON-file pattern as state.CooldownStore, but with an atomic
write (tmp + os.replace) since losing job-application state is more
consequential than losing cooldown timestamps.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid", "ref", "source",
}


def normalize_url(url: str) -> str:
    """Best-effort dedup key: strip fragment/tracking params, lowercase host,
    no trailing slash. Not perfect — job boards vary query params per visit —
    but good enough to catch the common re-click-the-same-link case."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


@dataclass
class JobRecord:
    url: str
    status: str  # "in_progress" | "applied" | "failed" | "denied"
    applied_at: str | None
    updated_at: str
    updated_at_epoch: float
    notes: str = ""


class JobStore:
    def __init__(self, path: Path, clock=time.time, stale_after_s: float = 1800.0):
        self.path = path
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, url: str) -> JobRecord | None:
        key = normalize_url(url)
        with self._lock:
            data = self._load()
        rec = data.get(key)
        if rec is None:
            return None
        return JobRecord(
            url=rec.get("url", url),
            status=rec["status"],
            applied_at=rec.get("applied_at"),
            updated_at=rec["updated_at"],
            updated_at_epoch=rec.get("updated_at_epoch", 0.0),
            notes=rec.get("notes", ""),
        )

    def has_applied(self, url: str) -> bool:
        rec = self.get(url)
        return rec is not None and rec.status == "applied"

    def is_in_progress(self, url: str) -> bool:
        """True only for a genuinely-fresh in_progress record — a crash mid-run
        leaves a stale record that must not permanently block retries."""
        rec = self.get(url)
        if rec is None or rec.status != "in_progress":
            return False
        return (self._clock() - rec.updated_at_epoch) < self._stale_after_s

    def _set(self, url: str, status: str, notes: str = "", applied: bool = False) -> None:
        key = normalize_url(url)
        now_epoch = self._clock()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now_epoch))
        with self._lock:
            data = self._load()
            entry = data.get(key, {})
            entry["url"] = url
            entry["status"] = status
            entry["updated_at"] = now_iso
            entry["updated_at_epoch"] = now_epoch
            entry["notes"] = notes
            if applied:
                entry["applied_at"] = now_iso
            data[key] = entry
            self._save(data)

    def mark_in_progress(self, url: str) -> None:
        self._set(url, "in_progress")

    def mark_applied(self, url: str, notes: str = "") -> None:
        self._set(url, "applied", notes=notes, applied=True)

    def mark_failed(self, url: str, notes: str = "") -> None:
        self._set(url, "failed", notes=notes)

    def mark_denied(self, url: str, notes: str = "") -> None:
        self._set(url, "denied", notes=notes)
