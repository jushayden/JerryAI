"""Jerry Pocket Agent — single entry point.

Starts everything the demo needs: mock form server, Ollama check + model
warm-up, Telegram bridge, and the real agent wired into the task worker.
Run: python main.py
"""
import asyncio
import ctypes
import re
import subprocess
import sys

import agent
import bridge
import config
import server
import state
import tools_browser
import tools_browser_use
import tools_email
import tools_fs
import tools_finance
import tools_social
import tools_shopping
import tools_system
import profile_store

_INSTANCE_MUTEX = None


def _acquire_single_instance() -> None:
    """Prevent duplicate Telegram workers and browser controllers on Windows."""
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JerryPocketAgent")
    if not handle:
        raise RuntimeError("could not create the Jerry single-instance lock")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(handle)
        raise RuntimeError("Jerry is already running. Close the existing window before starting it again.")
    _INSTANCE_MUTEX = handle


def _release_single_instance() -> None:
    global _INSTANCE_MUTEX
    if _INSTANCE_MUTEX is not None:
        ctypes.windll.kernel32.ReleaseMutex(_INSTANCE_MUTEX)
        ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
        _INSTANCE_MUTEX = None


_CONVERSATIONAL = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|who are you|what can you do|help)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _requires_action(task: state.TaskRecord) -> bool:
    return bool(task.source_url) or not bool(_CONVERSATIONAL.match(task.text))


_APPLICATION_DIRECT = re.compile(
    r"\b(job application|apply (?:to|for)|fill (?:out )?(?:this |the )?(?:application|form)|"
    r"application form|upload (?:my |the )?resume)\b", re.I)
_APPLICATION_FOLLOWUP = re.compile(
    r"\b(essay questions?|graduation (?:year|date)|grad (?:year|date)|work authori[sz]ation|"
    r"u\.?s\.? citizen|current location|university information|respond to (?:question |number )?\d+)\b",
    re.I)
_EXPLICIT_FILE_REQUEST = re.compile(
    r"\b(save|create|write|export|download)\b.{0,25}\b(file|document|txt|docx|pdf|desktop|folder)\b",
    re.I)


def _application_context(task: state.TaskRecord, recent=None) -> bool:
    text = task.text or ""
    source = (task.source_url or "").lower()
    explicit_file = bool(_EXPLICIT_FILE_REQUEST.search(text))
    if source and re.search(r"/(jobs?|careers?)/|/apply(?:[/?]|$)", source):
        return not explicit_file
    if _APPLICATION_DIRECT.search(text):
        return not explicit_file
    page = tools_browser.page_or_none()
    live_url = page.url.lower() if page is not None else ""
    live_application = bool(re.search(r"/(jobs?|careers?)/|/apply(?:[/?]|$)", live_url))
    if _APPLICATION_FOLLOWUP.search(text) and not explicit_file:
        if live_application:
            return True
        return any(re.search(r"\b(application|apply|resume|form)\b",
                             f"{item.text} {item.result or ''}", re.I)
                   for item in (recent or []))
    return False


def _task_tools(task: state.TaskRecord, *, application_mode: bool = False) -> dict:
    """Expose a small, relevant tool set instead of all 58 tools on every turn."""
    text = task.text.lower()
    selected: dict = {}
    browser_words = (
        "browser", "website", "web ", "page", "site", "tab", "search", "research",
        "scrape", "form", "application", "apply", "resume", "itinerary", "booking",
        "edge", "http://", "https://", "download", "upload",
        "linkedin", "instagram", "job", "career",
    )
    email_words = ("email", "gmail", "inbox", "mail ", "reply to", "draft")
    social_words = ("reddit", "youtube", "social", "subreddit")
    finance_words = (
        "stock", "stocks", "ticker", "market mover", "market moves", "invest",
        "investment", "shares", "earnings", "sec filing", "portfolio research",
    )
    shopping_words = (
        "clothes", "clothing", "outfit", "shirt", "pants", "jeans", "dress",
        "jacket", "shoes", "sneakers", "fashion", "wear", "wardrobe", "apparel",
    )
    system_words = (
        "volume", "mute", "brightness", "lock pc", "lock computer", "music", "media",
        "pause", "play", "shutdown", "shut down", "restart", "sleep pc", "system status",
    )

    is_browser = application_mode or bool(task.source_url) or any(w in text for w in browser_words)
    if is_browser:
        selected.update(tools_browser.TOOLS)
        # Keep the long-mission browser agent, but only offer it for multi-site research.
        # Applications and current-page tasks stay with the deterministic co-driver.
        if (not task.source_url
                and not any(w in text for w in finance_words)
                and any(w in text for w in ("multi-site", "compare", "research", "itinerary"))):
            selected.update(tools_browser_use.TOOLS)
    if any(w in text for w in email_words):
        selected.update(tools_email.TOOLS)
    if any(w in text for w in social_words):
        selected.update(tools_social.TOOLS)
    if any(w in text for w in finance_words):
        selected.update(tools_finance.TOOLS)
    if any(w in text for w in shopping_words):
        selected.update(tools_shopping.TOOLS)
    if any(w in text for w in system_words):
        selected.update(tools_system.TOOLS)

    # Unknown action requests still get core browser and OS coverage. Files/profile tools
    # are registered inside agent.run_task and do not need to be duplicated here.
    if not selected and _requires_action(task):
        selected.update(tools_browser.TOOLS)
        selected.update(tools_system.TOOLS)
    return selected


async def _final_screenshot() -> str | None:
    """Proof-of-work shot: the live browser page if one exists, else the desktop."""
    try:
        if tools_browser.page_or_none() is not None:
            res = await tools_browser.TOOLS["screenshot_page"]["fn"]({})
        elif tools_browser_use.last_screenshot() is not None:
            return tools_browser_use.last_screenshot()
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
    tools_fs.touched.clear()  # disk proof must never leak across unrelated tasks

    async def status_cb(step: str):
        await bridge.update_status(task, step)

    answered_questions: dict[str, str] = {}

    async def ask_cb(question: str):
        task.needs = question
        try:
            qkey = " ".join(question.lower().split())
            if qkey in answered_questions:
                return answered_questions[qkey]
            remembered = profile_store.application_answer(question)
            if remembered:
                answered_questions[qkey] = remembered
                return remembered
            answer = await bridge.ask_user(question)
            if re.search(r"\b(stop|cancel)( this| the)?( task)?\b", answer, re.I):
                raise asyncio.CancelledError
            answered_questions[qkey] = answer
            live_page = tools_browser.page_or_none()
            live_url = (live_page.url.lower() if live_page is not None else "")
            application_context = (
                (task.source_url and any(word in task.text.lower()
                    for word in ("application", "apply", "form", "resume")))
                or bool(re.search(r"/(jobs?|careers?)/|/apply(?:[/?]|$)", live_url))
            )
            if application_context:
                key = profile_store.remember_application_answer(question, answer)
                if key:
                    state.audit_event(task, "profile", "application_answer_saved", {"key": key})
            return answer
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
    application_mode = _application_context(task, recent)
    history = "\n".join(
        f'- [{t.status}] "{t.text}" -> {(t.result or t.needs or "no result")[:200]}'
        for t in recent)

    tools_browser.configure(
        confirm=bridge.confirm,
        task=task,
        secret_resolver=bridge.consume_secret,
    )
    tools_browser_use.configure(confirm=bridge.confirm, task=task, status=status_cb)
    # No preauth bypass any more (the operator build removed `!`); email send/reply/trash
    # and system power actions always gate through the phone, matching the browser gate.
    tools_email.configure(confirm=bridge.confirm)
    tools_finance.configure(task=task)
    tools_shopping.configure(task=task)
    tools_system.configure(confirm=bridge.confirm)
    prompt = task.text
    if task.attachments:
        listing = "\n".join(f"- {p}" for p in task.attachments)
        prompt += (
            "\n(The user attached these files directly to this J-badge task. "
            "Use these exact paths for matching upload fields; do not ask them to attach again:\n"
            f"{listing})"
        )
    if task.source_url:
        prompt += (
            f"\n(The J badge was used on this exact page: {task.source_url}. "
            "Act on that existing tab first.)"
        )
    if application_mode:
        prompt += (
            "\n(This is a browser application task or a follow-up answer for the open "
            "application. Put requested paragraphs and answers directly into the matching "
            "web form fields. Do not create, move, delete, or export local files unless the "
            "user explicitly asks for a file.)"
        )
    result = await agent.run_task(
        prompt,
        status_cb=status_cb,
        ask_user_cb=ask_cb,
        confirm_cb=bridge.confirm,
        request_secret_cb=secret_cb,
        audit_cb=audit_cb,
        extra_tools=_task_tools(task, application_mode=application_mode),
        disabled_tools=({"write_file", "move_file", "delete_file", "open_app", "take_screenshot"}
                        if application_mode else set()),
        history=history,
        require_action=_requires_action(task),
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
    _acquire_single_instance()
    _prune_screenshots()
    form_proc = None
    if config.START_MOCK_SERVER:
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
    server.configure(task_created_cb=bridge.notify_task_accepted)
    await bridge.start_bridge()
    await server.start_server()
    print(f"J-badge endpoint: http://127.0.0.1:{config.LOCAL_PORT}")
    print("Jerry Pocket Agent running — message your bot from the phone. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop_server()
        await bridge.stop_bridge()
        await tools_browser_use.shutdown()
        await tools_browser.shutdown()
        if form_proc is not None:
            form_proc.terminate()
        _release_single_instance()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        print(f"Startup failed: {e}")
