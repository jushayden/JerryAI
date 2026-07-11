"""Plain-assert tests for state.py. Run: python test_state.py"""
import json
import os
import tempfile
import time
from pathlib import Path

import config

# Redirect the event log to a temp file BEFORE exercising state.
_fd, _name = tempfile.mkstemp(suffix=".jsonl", prefix="events_test_")
os.close(_fd)
_tmp = Path(_name)
config.EVENTS_LOG = _tmp
config.ARTIFACT_DIR = Path(tempfile.mkdtemp(prefix="jerry_audit_test_"))

import state  # noqa: E402


def reset():
    state.tasks.clear()
    state.current = None
    while not state.queue.empty():
        state.queue.get_nowait()


# --- empty brief ---
reset()
assert state.compose_brief() == "Nothing yet — send me a task.", state.compose_brief()

# --- new_task: origin/source + literal ! + queue depth ---
t1 = state.new_task("!wipe the drive", origin="badge", source_url="https://example.test/form")
assert t1.text == "!wipe the drive"
assert t1.origin == "badge" and t1.source_url == "https://example.test/form"
assert t1.status == "queued"
assert len(t1.id) == 6

t2 = state.new_task("send an email", origin="telegram")
assert t2.text == "send an email"

assert state.queue.qsize() == 2
assert state.tasks == [t1, t2]

# --- mark / add_step ---
state.mark(t1, "running")
assert t1.status == "running" and t1.started is not None and t1.finished is None
state.add_step(t1, "opened notepad")
state.add_step(t1, "typed text")
assert t1.steps == ["opened notepad", "typed text"]
t1.started = time.time() - 12  # fake a 12s run for duration formatting
state.mark(t1, "done", result="all good")
assert t1.status == "done" and t1.finished is not None and t1.result == "all good"

state.mark(t2, "needs_attention")
t2.needs = "captcha on page"

t3 = state.new_task("x" * 100)  # long text -> truncated to 60 chars in brief
state.current = t3
state.mark(t3, "running")
state.add_step(t3, "step one")
state.add_step(t3, "latest step")

t4 = state.new_task("queued thing")
assert t4.status == "queued"

# --- populated brief ---
brief = state.compose_brief()
assert "Done (1):" in brief, brief
assert t1.id in brief and "!wipe the drive" in brief
assert "[12s]" in brief, brief
assert "Running:" in brief and "latest step" in brief
assert "x" * 60 in brief and "x" * 61 not in brief  # 60-char truncation
assert "Needs you (1):" in brief and "captcha on page" in brief
assert "Queued (1)" in brief, brief  # only t4 still has status "queued"

statuses = {t.id: t.status for t in state.tasks}
assert statuses[t1.id] == "done"
assert statuses[t2.id] == "needs_attention"
assert statuses[t3.id] == "running"
assert statuses[t4.id] == "queued"

# --- last-5 window for done section ---
for i in range(7):
    td = state.new_task(f"done task {i}")
    state.mark(td, "running")
    state.mark(td, "done")
brief = state.compose_brief()
assert "Done (8):" in brief, brief
assert "done task 6" in brief and "done task 2" in brief
assert "done task 1" not in brief  # outside the last-5 window
assert "wipe the drive" not in brief

# --- events.jsonl written, one JSON object per line, each with ts ---
lines = _tmp.read_text(encoding="utf-8").strip().splitlines()
assert len(lines) > 0
events = [json.loads(ln) for ln in lines]
assert all("ts" in e for e in events)
kinds = {e["event"] for e in events}
assert {"task_created", "task_status", "task_step"} <= kinds, kinds
created = [e for e in events if e["event"] == "task_created"]
assert created[0]["origin"] == "badge" and created[0]["text"] == "!wipe the drive"

# --- secrets are redacted from events and audit artifacts ---
state.register_secret("super-secret-123")
state.audit_event(t1, "test", "secret", {"value": "super-secret-123", "password": "other"})
audit_path = state.write_audit(t1)
audit_text = audit_path.read_text(encoding="utf-8")
assert "super-secret-123" not in audit_text and "other" not in audit_text
assert "[REDACTED]" in audit_text
state.forget_secret("super-secret-123")

# --- log_event never raises even on a bad path ---
config.EVENTS_LOG = Path("Z:/definitely/not/a/real/dir/events.jsonl")
state.log_event({"event": "should_not_raise"})
config.EVENTS_LOG = _tmp

_tmp.unlink(missing_ok=True)
audit_path.unlink(missing_ok=True)
config.ARTIFACT_DIR.rmdir()
print("test_state.py: all assertions passed")
