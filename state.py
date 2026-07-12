"""Task records, queue, audit trails, and event logging for Pocket Agent."""
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import config


TaskOrigin = Literal["telegram", "badge", "internal"]
_SENSITIVE_KEYS = re.compile(r"pass(word)?|secret|token|otp|code|credential|authorization", re.I)
_secret_values: set[str] = set()


def register_secret(value: str) -> None:
    """Register a live secret so every logger/audit renderer can redact it."""
    if value:
        _secret_values.add(value)


def forget_secret(value: str) -> None:
    _secret_values.discard(value)


def redact_text(value: Any) -> str:
    text = str(value)
    for secret in sorted(_secret_values, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _sanitize(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


@dataclass
class TaskRecord:
    id: str
    text: str
    origin: TaskOrigin = "telegram"
    source_url: str | None = None
    source_tab_token: str | None = None
    attachments: list[str] = field(default_factory=list)
    status: str = "queued"  # queued|running|done|failed|needs_attention|cancelled
    started: float | None = None
    finished: float | None = None
    steps: list[str] = field(default_factory=list)
    needs: str | None = None
    result: str | None = None
    status_msg_id: int | None = None
    screenshot: str | None = None
    artifacts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)


tasks: list[TaskRecord] = []
queue: asyncio.Queue = asyncio.Queue()
current: TaskRecord | None = None


def new_task(
    text: str,
    *,
    origin: TaskOrigin = "telegram",
    source_url: str | None = None,
    source_tab_token: str | None = None,
    attachments: list[str] | None = None,
) -> TaskRecord:
    """Create and enqueue a task. The old ``!`` approval bypass is intentionally gone."""
    task = TaskRecord(
        id=uuid.uuid4().hex[:6],
        text=text.strip(),
        origin=origin,
        source_url=source_url or None,
        source_tab_token=source_tab_token or None,
        attachments=list(attachments or []),
    )
    tasks.append(task)
    queue.put_nowait(task)
    log_event({
        "event": "task_created",
        "task": task.id,
        "text": task.text,
        "origin": task.origin,
        "source_url": task.source_url,
        "attachments": [Path(p).name for p in task.attachments],
    })
    audit_event(task, "task", "created", {
        "origin": origin, "source_url": source_url,
        "attachments": [Path(p).name for p in task.attachments],
    })
    return task


def mark(task: TaskRecord, status: str, result: str | None = None) -> None:
    task.status = status
    if status == "running":
        task.started = time.time()
    elif status in ("done", "failed", "needs_attention", "cancelled"):
        task.finished = time.time()
    if result is not None:
        task.result = redact_text(result)
    log_event({"event": "task_status", "task": task.id, "status": status, "result": result})
    audit_event(task, "task", status, {"result": result} if result is not None else None)


def add_step(task: TaskRecord, step: str) -> None:
    safe = redact_text(step)
    task.steps.append(safe)
    log_event({"event": "task_step", "task": task.id, "step": safe})


def audit_event(
    task: TaskRecord,
    category: str,
    action: str,
    details: Any = None,
    *,
    ok: bool | None = None,
) -> None:
    event = {
        "ts": time.time(),
        "category": category,
        "action": action,
        "details": _sanitize(details) if details is not None else None,
    }
    if ok is not None:
        event["ok"] = ok
    task.audit.append(event)
    log_event({"event": "task_audit", "task": task.id, **event})


def add_artifact(task: TaskRecord, path: str | Path) -> None:
    value = str(path)
    if value not in task.artifacts:
        task.artifacts.append(value)
        audit_event(task, "artifact", "created", {"path": value})


def add_source(task: TaskRecord, url: str) -> None:
    if url and url not in task.sources:
        task.sources.append(url)
        audit_event(task, "source", "observed", {"url": url})


def write_audit(task: TaskRecord) -> Path:
    """Render a human-readable, Telegram-friendly audit artifact."""
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.ARTIFACT_DIR / f"task_{task.id}_audit.txt"
    lines = [
        f"Jerry task audit: {task.id}",
        f"Origin: {task.origin}",
        f"Request: {redact_text(task.text)}",
        f"Status: {task.status}",
    ]
    if task.source_url:
        lines.append(f"Source page: {task.source_url}")
    lines.append("")
    for event in task.audit:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        detail = event.get("details")
        suffix = "" if detail in (None, "", {}) else " | " + json.dumps(detail, ensure_ascii=False, default=str)
        ok = "" if "ok" not in event else (" | ok" if event["ok"] else " | failed")
        lines.append(f"[{stamp}] {event['category']}: {event['action']}{ok}{suffix}")
    if task.sources:
        lines.extend(["", "Sources:", *[f"- {u}" for u in task.sources]])
    path.write_text(redact_text("\n".join(lines)) + "\n", encoding="utf-8")
    return path


def log_event(d: dict) -> None:
    """Append one sanitized JSON line. Logging must never break a task."""
    try:
        safe = _sanitize(dict(d))
        safe["ts"] = safe.get("ts", time.time())
        with open(config.EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _dur(task: TaskRecord) -> str:
    if task.started is not None and task.finished is not None:
        return f" [{task.finished - task.started:.0f}s]"
    return ""


def compose_brief() -> str:
    done = [t for t in tasks if t.status == "done"]
    needs = [t for t in tasks if t.status == "needs_attention"]
    queued = [t for t in tasks if t.status == "queued"]
    parts: list[str] = []
    if current is not None:
        parts.append(f"Working now\n• {current.text[:120]}")
    if needs:
        lines = ["Needs your input"]
        lines += [f"• {(t.needs or t.result or t.text)[:160]}" for t in needs[-5:]]
        parts.append("\n".join(lines))
    if queued:
        noun = "task" if len(queued) == 1 else "tasks"
        parts.append(f"Queued\n• {len(queued)} {noun}")
    if done:
        lines = ["Recently finished"]
        lines += [f"• {t.text[:100]}{_dur(t)}" for t in done[-3:]]
        parts.append("\n".join(lines))
    return ("\n\n".join(parts) if parts
            else "Nothing is running. Send me anything you'd like help with.")
