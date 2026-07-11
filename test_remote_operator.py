"""Focused tests for Telegram secret handles and task audit redaction."""
import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import bridge
import config
import state
import tools_browser


class _Chat:
    id = 4242


class _Message:
    def __init__(self, text):
        self.text = text

    async def reply_text(self, text):
        raise AssertionError("secret replies must be consumed, not queued as tasks")


class _Update:
    effective_chat = _Chat()

    def __init__(self, text):
        self.message = _Message(text)


async def main():
    original_log = config.EVENTS_LOG
    original_artifacts = config.ARTIFACT_DIR
    with tempfile.TemporaryDirectory(prefix="jerry_remote_test_") as tmp:
        root = Path(tmp)
        config.EVENTS_LOG = root / "events.jsonl"
        config.ARTIFACT_DIR = root / "artifacts"
        bridge._allowed_chat_id = 4242
        bridge._secrets.clear()

        task = state.new_task("log in", origin="telegram")
        waiter = asyncio.create_task(bridge.request_secret(
            "Password", kind="password", domain="example.test", task_id=task.id))
        await asyncio.sleep(0)
        raw = "correct horse battery staple"
        await bridge._on_text(_Update(raw), None)
        handle = await waiter

        assert handle.startswith("secret:") and raw not in handle
        state.audit_event(task, "test", "while_secret_live", {"value": raw})
        audit = state.write_audit(task).read_text(encoding="utf-8")
        assert raw not in audit and "[REDACTED]" in audit

        resolved = await bridge.consume_secret(handle, task_id=task.id)
        assert resolved == raw
        try:
            await bridge.consume_secret(handle, task_id=task.id)
            raise AssertionError("a secret handle must be one-use")
        except ValueError:
            pass

        # Edge closed: launch directly and wait for CDP.
        original_cdp = tools_browser._cdp_available
        original_running = tools_browser._edge_running
        original_launch = tools_browser._launch_edge_sync
        original_run = tools_browser.subprocess.run
        original_confirm = tools_browser._confirm_cb
        original_timeout = config.EDGE_START_TIMEOUT
        ready = {"value": False, "launches": 0}
        tools_browser._cdp_available = lambda: ready["value"]
        tools_browser._edge_running = lambda: False
        def launch():
            ready["launches"] += 1
            ready["value"] = True
        tools_browser._launch_edge_sync = launch
        config.EDGE_START_TIMEOUT = 0.2
        await tools_browser._ensure_edge()
        assert ready["launches"] == 1

        # Edge running normally: denial stops before termination/relaunch.
        tools_browser._cdp_available = lambda: False
        tools_browser._edge_running = lambda: True
        async def deny(summary):
            return False
        tools_browser._confirm_cb = deny
        try:
            await tools_browser._ensure_edge()
            raise AssertionError("denied Edge relaunch must stop")
        except RuntimeError as e:
            assert "denied" in str(e)

        # Approval permits termination and a single relaunch.
        ready = {"value": False, "launches": 0, "kills": 0}
        tools_browser._cdp_available = lambda: ready["value"]
        tools_browser._edge_running = lambda: True
        async def allow(summary):
            return True
        tools_browser._confirm_cb = allow
        def fake_run(*args, **kwargs):
            ready["kills"] += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        def approved_launch():
            ready["launches"] += 1
            ready["value"] = True
        tools_browser.subprocess.run = fake_run
        tools_browser._launch_edge_sync = approved_launch
        await tools_browser._ensure_edge()
        assert ready["kills"] == 1 and ready["launches"] == 1

        tools_browser._cdp_available = original_cdp
        tools_browser._edge_running = original_running
        tools_browser._launch_edge_sync = original_launch
        tools_browser.subprocess.run = original_run
        tools_browser._confirm_cb = original_confirm
        config.EDGE_START_TIMEOUT = original_timeout

        expired = "secret:expired"
        bridge._secrets[expired] = bridge._Secret(
            value="123456", expires=time.monotonic() - 1,
            task_id=task.id, kind="otp", domain="example.test")
        try:
            await bridge.consume_secret(expired, task_id=task.id)
            raise AssertionError("expired secrets must fail")
        except ValueError:
            pass

    config.EVENTS_LOG = original_log
    config.ARTIFACT_DIR = original_artifacts
    print("test_remote_operator.py: all assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
