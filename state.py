"""Task records, queue, and event log for Pocket Agent (Track A)."""
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

import config


@dataclass
class TaskRecord:
    id: str
    text: str
    status: str = "queued"  # queued|running|done|failed|needs_attention|cancelled
    started: float | None = None
    finished: float | None = None
    steps: list[str] = field(default_factory=list)
    needs: str | None = None
    result: str | None = None
    preauthorized: bool = False


tasks: list[TaskRecord] = []
queue: asyncio.Queue = asyncio.Queue()
current: TaskRecord | None = None


def new_task(text: str) -> TaskRecord:
    """Create a TaskRecord from user text ("!" prefix = preauthorized), enqueue it."""
    preauth = False
    if text.startswith("!"):
        preauth = True
        text = text[1:].strip()
    task = TaskRecord(id=uuid.uuid4().hex[:6], text=text, preauthorized=preauth)
    tasks.append(task)
    queue.put_nowait(task)
    log_event({"event": "task_created", "task": task.id, "text": task.text,
               "preauthorized": task.preauthorized})
    return task


def mark(task: TaskRecord, status: str, result: str | None = None) -> None:
    """Set task status (stamping started/finished) and optional result."""
    task.status = status
    if status == "running":
        task.started = time.time()
    elif status in ("done", "failed", "needs_attention", "cancelled"):
        task.finished = time.time()
    if result is not None:
        task.result = result
    log_event({"event": "task_status", "task": task.id, "status": status,
               "result": result})


def add_step(task: TaskRecord, step: str) -> None:
    """Append a step to the task and log it."""
    task.steps.append(step)
    log_event({"event": "task_step", "task": task.id, "step": step})


def log_event(d: dict) -> None:
    """Append one JSON line (with ts) to EVENTS_LOG. Never raises."""
    try:
        d = dict(d)
        d["ts"] = time.time()
        with open(config.EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _dur(task: TaskRecord) -> str:
    if task.started is not None and task.finished is not None:
        return f" [{task.finished - task.started:.0f}s]"
    return ""


def compose_brief() -> str:
    """Telegram-friendly plain-text summary of all task activity."""
    done = [t for t in tasks if t.status == "done"]
    needs = [t for t in tasks if t.status == "needs_attention"]
    queued = [t for t in tasks if t.status == "queued"]
    parts: list[str] = []
    if done:
        lines = [f"Done ({len(done)}):"]
        for t in done[-5:]:
            lines.append(f"  {t.id} {t.text[:60]}{_dur(t)}")
        parts.append("\n".join(lines))
    if current is not None:
        latest = current.steps[-1] if current.steps else "starting"
        parts.append(f"Running: {current.id} {current.text[:60]} → {latest}")
    if needs:
        lines = [f"Needs you ({len(needs)}):"]
        for t in needs:
            lines.append(f"  {t.id} {t.text[:60]} — {t.needs or '?'}")
        parts.append("\n".join(lines))
    if queued:
        parts.append(f"Queued ({len(queued)})")
    if not parts:
        return "Nothing yet — send me a task."
    return "\n".join(parts)
