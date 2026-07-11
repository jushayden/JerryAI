"""Telegram phone bridge for Pocket Agent (Track A).

Manual PTB v22 init — main.py owns the event loop, never app.run_polling().
"""
import asyncio
import os
import tempfile
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import state
import voice
from state import TaskRecord

# --- module state ---
_app: Application | None = None
_worker_task: asyncio.Task | None = None
_current_run_task: asyncio.Task | None = None  # wraps the running agent call (for /cancel)
_allowed_chat_id: int = config.ALLOWED_CHAT_ID
_pending_confirms: dict[str, asyncio.Future] = {}  # cid -> Future[bool]
_pending_ask: asyncio.Future | None = None  # Future[str] resolved by next plain text
_status_cards: dict[str, dict] = {}  # task.id -> {msg_id, last, pending, flusher}


async def _default_agent(task: TaskRecord) -> str:
    await asyncio.sleep(2)
    return f"echo: {task.text}"


_agent_fn = _default_agent


def set_agent(fn) -> None:
    """Register the agent coroutine: async (task: TaskRecord) -> str."""
    global _agent_fn
    _agent_fn = fn


# --- outbound helpers ---
async def _retry(fn, *args, **kwargs):
    """Call an async send fn with 3 attempts (1s/2s backoff). Returns None on final failure."""
    for attempt in range(3):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            if attempt == 2:
                state.log_event({"event": "send_failed", "error": str(e)})
                return None
            await asyncio.sleep(1 + attempt)


async def send_text(text: str) -> None:
    """Send plain text to the owner chat."""
    if _app is None or _allowed_chat_id == 0:
        state.log_event({"event": "send_skipped", "reason": "no app or no owner chat"})
        return
    await _retry(_app.bot.send_message, chat_id=_allowed_chat_id, text=text)


# --- confirm / ask ---
async def confirm(summary: str) -> bool:
    """Ask the owner to approve via inline keyboard. Timeout or /cancel -> False."""
    if _app is None or _allowed_chat_id == 0:
        return False
    cid = uuid.uuid4().hex[:8]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_confirms[cid] = fut
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"cfm:{cid}:y"),
        InlineKeyboardButton("Deny", callback_data=f"cfm:{cid}:n"),
    ]])
    msg = await _retry(_app.bot.send_message, chat_id=_allowed_chat_id,
                       text=f"⚠️ Approval needed:\n{summary}", reply_markup=kb)
    if msg is None:
        _pending_confirms.pop(cid, None)
        return False
    state.log_event({"event": "confirm_sent", "cid": cid, "summary": summary[:120]})
    try:
        approved = await asyncio.wait_for(fut, config.CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        _pending_confirms.pop(cid, None)
        try:
            await _app.bot.edit_message_text(
                chat_id=_allowed_chat_id, message_id=msg.message_id,
                text="Expired (no answer)")
        except Exception as e:
            state.log_event({"event": "edit_failed", "error": str(e)})
        state.log_event({"event": "confirm_timeout", "cid": cid})
        return False
    state.log_event({"event": "confirm_answered", "cid": cid, "approved": approved})
    return approved


async def ask_user(question: str) -> str:
    """Send a question; the next plain text message is the answer. Timeout raises."""
    global _pending_ask
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_ask = fut
    await send_text(f"❓ {question}")
    try:
        return await asyncio.wait_for(fut, config.CONFIRM_TIMEOUT)
    finally:
        if _pending_ask is fut:
            _pending_ask = None


# --- status card ---
def _render_status(task: TaskRecord) -> str:
    lines = [f"▶️ {task.text}"]
    lines += [f"→ {s}" for s in task.steps[-6:]]
    return "\n".join(lines)


async def _flush_card(task: TaskRecord, card: dict) -> None:
    try:
        await asyncio.sleep(max(0.0, card["last"] + 1.0 - time.monotonic()))
        text = card.get("pending")
        card["pending"] = None
        if text and card["msg_id"] is not None and _app is not None:
            card["last"] = time.monotonic()
            try:
                await _app.bot.edit_message_text(
                    chat_id=_allowed_chat_id, message_id=card["msg_id"], text=text)
            except Exception as e:
                state.log_event({"event": "edit_failed", "task": task.id, "error": str(e)})
    finally:
        card["flusher"] = None


async def update_status(task: TaskRecord, step: str) -> None:
    """Maintain one status-card message per task, throttled to <=1 edit/s."""
    state.add_step(task, step)
    if _app is None or _allowed_chat_id == 0:
        return
    card = _status_cards.setdefault(
        task.id, {"msg_id": None, "last": 0.0, "pending": None, "flusher": None})
    text = _render_status(task)
    if card["msg_id"] is None:
        try:
            msg = await _app.bot.send_message(chat_id=_allowed_chat_id, text=text)
            card["msg_id"] = msg.message_id
            task.status_msg_id = msg.message_id
            card["last"] = time.monotonic()
        except Exception as e:
            state.log_event({"event": "send_failed", "task": task.id, "error": str(e)})
        return
    now = time.monotonic()
    if now - card["last"] >= 1.0 and card["flusher"] is None:
        card["last"] = now
        try:
            await _app.bot.edit_message_text(
                chat_id=_allowed_chat_id, message_id=card["msg_id"], text=text)
        except Exception as e:
            state.log_event({"event": "edit_failed", "task": task.id, "error": str(e)})
    else:
        card["pending"] = text  # coalesce: keep only the latest
        if card["flusher"] is None:
            card["flusher"] = asyncio.create_task(_flush_card(task, card))


# --- final report ---
async def send_report(task: TaskRecord, screenshot_path: str | None = None) -> None:
    """Send the final report for a task, plus optional screenshot."""
    if _app is None or _allowed_chat_id == 0:
        return
    lines = [f"[{task.status.upper()}] {task.text}"]
    if task.result:
        lines.append(f"Result: {task.result}")
    if task.needs:
        lines.append(f"Needs: {task.needs}")
    meta = f"Steps: {len(task.steps)}"
    if task.started is not None and task.finished is not None:
        meta += f" | Duration: {task.finished - task.started:.0f}s"
    lines.append(meta)
    await _retry(_app.bot.send_message, chat_id=_allowed_chat_id, text="\n".join(lines))
    if screenshot_path:
        try:
            with open(screenshot_path, "rb") as f:
                data = f.read()
            sent = await _retry(_app.bot.send_photo, chat_id=_allowed_chat_id, photo=data)
            if sent is not None:  # delivered to Telegram — no need to keep it on disk
                import os
                os.remove(screenshot_path)
        except OSError as e:
            state.log_event({"event": "screenshot_failed", "error": str(e)})


# --- owner capture ---
def _write_env_chat_id(chat_id: int) -> None:
    """Rewrite (or add) the ALLOWED_CHAT_ID line in .env, preserving other lines."""
    try:
        env = config.PROJECT_ROOT / ".env"
        lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
        out, replaced = [], False
        for ln in lines:
            if ln.strip().startswith("ALLOWED_CHAT_ID="):
                out.append(f"ALLOWED_CHAT_ID={chat_id}")
                replaced = True
            else:
                out.append(ln)
        if not replaced:
            out.append(f"ALLOWED_CHAT_ID={chat_id}")
        env.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:
        state.log_event({"event": "env_write_failed", "error": str(e)})


def _is_allowed(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and _allowed_chat_id != 0 and chat.id == _allowed_chat_id


# --- handlers ---
async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _allowed_chat_id
    chat_id = update.effective_chat.id
    if _allowed_chat_id == 0:
        _allowed_chat_id = chat_id
        _write_env_chat_id(chat_id)
        state.log_event({"event": "owner_captured", "chat_id": chat_id})
        await update.message.reply_text("You're registered as owner")
        return
    if chat_id != _allowed_chat_id:
        return
    await update.message.reply_text(
        "Pocket Agent ready. Send a task (prefix with ! to preauthorize), "
        "/brief, /status, /cancel.")


async def _dispatch_text(text: str, update: Update | None = None) -> None:
    """Route a user message (typed OR transcribed from voice) into the ask/queue pipeline."""
    global _pending_ask
    if _pending_ask is not None and not _pending_ask.done():
        fut, _pending_ask = _pending_ask, None
        fut.set_result(text)
        return
    ahead = state.queue.qsize() + (1 if state.current is not None else 0)
    task = state.new_task(text)
    reply = f"Queued as task {task.id} ({ahead} ahead of it)"
    if update is not None:
        await update.message.reply_text(reply)
    else:
        await send_text(reply)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await _dispatch_text(update.message.text, update)


async def _on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a Telegram voice message locally, then run it like a typed task."""
    if not _is_allowed(update):
        return
    v = update.message.voice
    if v is None:
        return
    await update.message.reply_text("🎧 transcribing…")
    tmp = os.path.join(tempfile.gettempdir(), f"pa_voice_{v.file_unique_id}.ogg")
    try:
        f = await context.bot.get_file(v.file_id)
        await f.download_to_drive(tmp)
        text = await voice.transcribe(tmp)
    except Exception as e:
        await update.message.reply_text(f"Couldn't handle that voice message: {e}")
        return
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if text.startswith("Error"):
        await update.message.reply_text(text)
        return
    if not text.strip():
        await update.message.reply_text("Couldn't make out any speech — try again.")
        return
    await update.message.reply_text(f"🎙️ heard: {text}")
    await _dispatch_text(text, update)


async def _on_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.reply_text(state.compose_brief())


async def _on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    cur = state.current
    if cur is None:
        await update.message.reply_text("Idle")
    else:
        latest = cur.steps[-1] if cur.steps else "starting"
        await update.message.reply_text(f"Running {cur.id}: {cur.text[:60]} → {latest}")


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_ask
    if not _is_allowed(update):
        return
    for cid in list(_pending_confirms):
        fut = _pending_confirms.pop(cid, None)
        if fut is not None and not fut.done():
            fut.set_result(False)
    if _pending_ask is not None and not _pending_ask.done():
        _pending_ask.cancel()
    _pending_ask = None
    if _current_run_task is not None and not _current_run_task.done():
        if state.current is not None:
            state.mark(state.current, "cancelled")
        _current_run_task.cancel()
    await update.message.reply_text("Stopped.")


async def _on_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    task = state.new_task(
        "Scan my email inbox from the last 24 hours using scan_inbox, then give me a short "
        "briefing: summarize what's new and flag only what looks important (things needing a "
        "reply, deadlines, money, real people writing directly). Skip routine newsletters/promos.")
    await update.message.reply_text(f"On it — inbox briefing queued as task {task.id}.")


async def _on_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    raw = (update.message.text or "").partition(" ")[2].strip()
    if ":" not in raw:
        await update.message.reply_text("Usage: /remember key: value  (e.g. /remember work authorization: US citizen)")
        return
    key, _, value = raw.partition(":")
    key, value = key.strip(), value.strip()
    if not key or not value:
        await update.message.reply_text("Usage: /remember key: value")
        return
    try:
        with open(config.PROFILE_EXTRA_PATH, "a", encoding="utf-8") as f:
            f.write(f"{key}: {value}\n")
        state.log_event({"event": "remember", "key": key})
        await update.message.reply_text(f"Got it — I'll remember {key} = {value}.")
    except Exception as e:
        await update.message.reply_text(f"Couldn't save that: {e}")


async def _on_testconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    async def _run():
        ok = await confirm("Test approval — tap one")
        await send_text(f"testconfirm result: {ok}")

    asyncio.create_task(_run())


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or not _is_allowed(update):
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "cfm":
        await q.answer()
        return
    _, cid, flag = parts
    fut = _pending_confirms.pop(cid, None)
    if fut is None or fut.done():
        await q.answer("expired")
        return
    approved = flag == "y"
    fut.set_result(approved)
    await q.answer()
    suffix = "→ Approved ✅" if approved else "→ Denied ❌"
    try:
        await q.edit_message_text(text=f"{q.message.text}\n{suffix}", reply_markup=None)
    except Exception as e:
        state.log_event({"event": "edit_failed", "error": str(e)})


# --- worker ---
async def _run_agent(task: TaskRecord) -> str:
    return await _agent_fn(task)


async def _worker() -> None:
    global _current_run_task
    while True:
        task = await state.queue.get()
        if task.status == "cancelled":
            continue
        state.current = task
        state.mark(task, "running")
        run = asyncio.create_task(_run_agent(task))
        _current_run_task = run
        try:
            # No wall-clock cap here: waiting on the human (ask_user/confirm) must not
            # kill the task. Machine time is bounded inside the agent loop instead
            # (MAX_STEPS x MODEL_CALL_TIMEOUT) and each human wait by CONFIRM_TIMEOUT.
            result = await run
            state.mark(task, "done", result)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the worker itself is being cancelled (shutdown)
            if task.status != "cancelled":
                state.mark(task, "cancelled")
        except Exception as e:
            state.mark(task, "failed", str(e))
        finally:
            _current_run_task = None
            state.current = None
            try:
                await send_report(task, getattr(task, "screenshot", None))
            except Exception as e:
                state.log_event({"event": "report_failed", "task": task.id,
                                 "error": str(e)})


# --- lifecycle ---
async def start_bridge() -> None:
    """Build the PTB app, register handlers, start polling, spawn the worker."""
    global _app, _worker_task
    if not config.BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is empty — create a bot with @BotFather and put "
            "BOT_TOKEN=<token> in .env at the project root.")
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(CommandHandler("brief", _on_brief))
    app.add_handler(CommandHandler("status", _on_status))
    app.add_handler(CommandHandler("cancel", _on_cancel))
    app.add_handler(CommandHandler("inbox", _on_inbox))
    app.add_handler(CommandHandler("remember", _on_remember))
    app.add_handler(CommandHandler("testconfirm", _on_testconfirm))
    app.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^cfm:"))
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    _app = app
    _worker_task = asyncio.create_task(_worker())
    state.log_event({"event": "bridge_started"})


async def stop_bridge() -> None:
    """Graceful shutdown: stop polling, stop app, cancel the worker."""
    global _app, _worker_task, _current_run_task
    if _current_run_task is not None and not _current_run_task.done():
        _current_run_task.cancel()
    if _app is not None:
        try:
            if _app.updater and _app.updater.running:
                await _app.updater.stop()
            if _app.running:
                await _app.stop()
            await _app.shutdown()
        except Exception as e:
            state.log_event({"event": "shutdown_error", "error": str(e)})
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):
            pass
    _app = None
    _worker_task = None
    state.log_event({"event": "bridge_stopped"})


async def _main() -> None:
    await start_bridge()
    print("Bridge running with stub agent, Ctrl+C to stop")
    try:
        await asyncio.Event().wait()
    finally:
        await stop_bridge()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
