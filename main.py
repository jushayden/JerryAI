"""Tora AI — single entry point.

Starts the agent: Ollama check + model
warm-up, Telegram bridge, and the real agent wired into the task worker.
Run: python main.py
"""
import asyncio
import subprocess
import sys

import agent
import bridge
import config
import server
import state
import tools_browser
import tools_email
import tools_fs


async def _final_screenshot() -> str | None:
    """Proof-of-work shot: the live browser page if one exists, else the desktop."""
    try:
        if tools_browser.page_or_none() is not None:
            res = await tools_browser.TOOLS["screenshot_page"]["fn"]({})
        else:
            res = await tools_fs.TOOLS["take_screenshot"]["fn"]({})
        if "screenshot saved: " in res:
            return res.split("screenshot saved: ", 1)[1].strip()
    except Exception as e:
        state.log_event({"event": "final_screenshot_failed", "error": str(e)})
    return None


async def _run_real_agent(task: state.TaskRecord) -> str:
    """Bridge worker entry: run the model loop with phone-wired callbacks."""
    _prune_screenshots()  # previous task's shots are already delivered or stale

    async def status_cb(step: str):
        await bridge.update_status(task, step)

    async def ask_cb(question: str):
        task.needs = question
        try:
            return await bridge.ask_user(question)
        finally:
            task.needs = None

    async def secret_cb(question: str, *, kind: str, domain: str):
        task.needs = f"{kind} required for {domain or 'the current page'}"
        try:
            return await bridge.request_secret(
                question, kind=kind, domain=domain, task_id=task.id)
        finally:
            task.needs = None

    async def audit_cb(category: str, action: str, details=None, *, ok=None):
        state.audit_event(task, category, action, details, ok=ok)

    recent = [t for t in state.tasks
              if t.id != task.id and t.status in ("done", "failed", "needs_attention", "cancelled")][-5:]
    history = "\n".join(
        f'- [{t.status}] "{t.text}" -> {(t.result or t.needs or "no result")[:200]}'
        for t in recent)

    tools_browser.configure(
        confirm=bridge.confirm,
        task=task,
        secret_resolver=bridge.consume_secret,
    )
    prompt = task.text
    if task.source_url:
        prompt += (
            f"\n(The T badge was used on this exact page: {task.source_url}. "
            "Act on that existing tab first.)"
        )
    result = await agent.run_task(
        prompt,
        status_cb=status_cb,
        ask_user_cb=ask_cb,
        confirm_cb=bridge.confirm,
        request_secret_cb=secret_cb,
        audit_cb=audit_cb,
        extra_tools={**tools_browser.TOOLS, **tools_email.TOOLS},
        history=history,
    )
    proof = _verify_touched()
    if proof:
        result = f"{result}\n\nVerified on disk:\n{proof}"
    task.screenshot = await _final_screenshot()
    return result


def _verify_touched() -> str:
    """Deterministic outcome check for file work — screenshots show the screen, not the disk."""
    lines = []
    for p in tools_fs.touched[-4:]:
        try:
            if not p.exists():
                lines.append(f"- {p} — no longer exists (deleted)")
            elif p.is_dir():
                names = [e.name for e in list(p.iterdir())[:5]]
                lines.append(f"- {p} exists, contains: {', '.join(names) or '(empty)'}")
            else:
                lines.append(f"- {p} exists ({p.stat().st_size} bytes)")
        except Exception:
            pass
    return "\n".join(lines)


def _prune_screenshots() -> None:
    """Drop stale mid-task screenshots so the folder doesn't grow forever."""
    try:
        if config.SCREENSHOT_DIR.exists():
            for f in config.SCREENSHOT_DIR.glob("*.png"):
                f.unlink(missing_ok=True)
    except Exception:
        pass


async def main() -> None:
    if not config.BOT_TOKEN or config.BOT_TOKEN == "123456:ABC-your-token-from-BotFather":
        raise RuntimeError("Set your own BOT_TOKEN in .env before starting. See QUICKSTART.md.")
    config.ensure_local_token()
    _prune_screenshots()
    form_proc = None
    try:
        if config.MOCK_FORM_ENABLED:
            form_proc = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(config.MOCK_FORM_PORT),
                 "--bind", "127.0.0.1", "--directory", str(config.MOCK_FORM_DIR)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Mock form: http://127.0.0.1:{config.MOCK_FORM_PORT}/index.html")
        ok, msg = await agent.ollama_ready()
        print(msg)
        if ok:
            print(f"Warming up {config.MODEL}...")
            try:
                await agent.warm_up()
                print("Model ready")
            except Exception as error:
                print(f"Warm-up failed: {error}. Check Ollama before sending tasks.")
        else:
            print("Tasks need Ollama and the configured model. You can still pair Telegram.")
        bridge.set_agent(_run_real_agent)
        server.configure(task_created_cb=bridge.notify_task_accepted)
        await bridge.start_bridge()
        await server.start_server()
        print(f"Tora extension endpoint: http://127.0.0.1:{config.LOCAL_PORT}")
        print("Tora AI running. Message your bot; Ctrl+C to stop.")
        if config.ALLOWED_CHAT_ID == 0:
            print("Send /start to see your chat ID, set ALLOWED_CHAT_ID in .env, then restart.")
        await asyncio.Event().wait()
    finally:
        # Cleanup also runs if model/bot/server startup fails partway through.
        for cleanup in (server.stop_server, bridge.stop_bridge, tools_browser.shutdown):
            try:
                await cleanup()
            except Exception as error:
                state.log_event({"event": "cleanup_failed", "error": str(error)})
        if form_proc is not None:
            form_proc.terminate()
            try:
                await asyncio.to_thread(form_proc.wait, timeout=5)
            except subprocess.TimeoutExpired:
                form_proc.kill()
                await asyncio.to_thread(form_proc.wait, timeout=5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
