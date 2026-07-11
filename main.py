"""Pocket Agent — single entry point.

Starts everything the demo needs: mock form server, Ollama check + model
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

    recent = [t for t in state.tasks
              if t.id != task.id and t.status in ("done", "failed", "needs_attention", "cancelled")][-5:]
    history = "\n".join(
        f'- [{t.status}] "{t.text}" -> {(t.result or t.needs or "no result")[:200]}'
        for t in recent)

    tools_browser.configure(confirm=bridge.confirm, preauth=task.preauthorized)
    tools_email.configure(confirm=bridge.confirm, preauth=task.preauthorized)
    result = await agent.run_task(
        task.text,
        preauthorized=task.preauthorized,
        status_cb=status_cb,
        ask_user_cb=ask_cb,
        confirm_cb=bridge.confirm,
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
    _prune_screenshots()
    form_proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(config.MOCK_FORM_PORT),
         "--directory", str(config.MOCK_FORM_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"mock form: http://localhost:{config.MOCK_FORM_PORT}/index.html")

    ok, msg = await agent.ollama_ready()
    print(msg)
    if ok:
        print(f"warming up {config.MODEL} (first load can take a minute)...")
        await agent.warm_up()
        print("model ready")
    else:
        print("WARNING: tasks will fail until Ollama is running and the model is pulled")

    bridge.set_agent(_run_real_agent)
    await bridge.start_bridge()
    await server.start_server()
    print(f"J-badge endpoint: http://127.0.0.1:{config.LOCAL_PORT}")
    print("Pocket Agent running — message your bot from the phone. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop_server()
        await bridge.stop_bridge()
        await tools_browser.shutdown()
        form_proc.terminate()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
