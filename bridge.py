"""Telegram phone bridge for Pocket Agent (Track A).

Manual PTB v22 init — main.py owns the event loop, never app.run_polling().
"""
import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import profile_store
import state
from state import TaskRecord

# --- module state ---
_app: Application | None = None
_worker_task: asyncio.Task | None = None
_current_run_task: asyncio.Task | None = None  # wraps the running agent call (for /cancel)
_allowed_chat_id: int = config.ALLOWED_CHAT_ID
_pending_confirms: dict[str, asyncio.Future] = {}  # cid -> Future[bool]
_pending_ask: asyncio.Future | None = None  # Future[str] resolved by next plain text
_pending_secret: tuple[asyncio.Future, str, str, str | None] | None = None
_pending_setup: asyncio.Future | None = None  # Future[str] resolved by the onboarding reply
_status_cards: dict[str, dict] = {}  # task.id -> {msg_id, last, pending, flusher}


@dataclass
class _Secret:
    value: str
    expires: float
    task_id: str | None
    kind: str
    domain: str


_secrets: dict[str, _Secret] = {}


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
    await _retry(_app.bot.send_message, chat_id=_allowed_chat_id,
                 text=state.redact_text(text))


async def notify_task_accepted(task: TaskRecord) -> None:
    """Notify the phone when a task originated outside Telegram (the J badge)."""
    await send_text(
        f"J badge task {task.id} accepted.\n"
        f"Page: {task.source_url or '(not provided)'}\n"
        f"Request: {task.text}"
    )


def _expire_secrets() -> None:
    now = time.monotonic()
    for handle, secret in list(_secrets.items()):
        if secret.expires <= now:
            state.forget_secret(secret.value)
            _secrets.pop(handle, None)


async def request_secret(
    question: str,
    *,
    kind: str = "secret",
    domain: str = "",
    task_id: str | None = None,
) -> str:
    """Request a secret via Telegram and return an opaque, five-minute handle."""
    global _pending_secret
    if _pending_secret is not None:
        raise RuntimeError("another secret request is already pending")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_secret = (fut, kind, domain, task_id)
    where = f" for {domain}" if domain else ""
    await send_text(
        f"🔐 {question}{where}\n"
        "Reply with the value. It will be held in memory for one use and redacted from Jerry's logs."
    )
    try:
        return await asyncio.wait_for(fut, config.CONFIRM_TIMEOUT)
    finally:
        if _pending_secret is not None and _pending_secret[0] is fut:
            _pending_secret = None


async def consume_secret(handle: str, *, task_id: str | None = None) -> str:
    """Consume a secret without exposing it to the model or audit trail."""
    _expire_secrets()
    secret = _secrets.pop(handle, None)
    if secret is None:
        raise ValueError("secret handle is invalid or expired")
    if task_id and secret.task_id and task_id != secret.task_id:
        _secrets[handle] = secret
        raise ValueError("secret handle belongs to a different task")
    state.forget_secret(secret.value)
    return secret.value


def clear_task_secrets(task_id: str | None) -> None:
    for handle, secret in list(_secrets.items()):
        if task_id is None or secret.task_id == task_id:
            state.forget_secret(secret.value)
            _secrets.pop(handle, None)


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
    summary = state.redact_text(summary)
    if state.current is not None:
        state.audit_event(state.current, "approval", "requested", {"summary": summary})
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
    if state.current is not None:
        state.audit_event(state.current, "approval", "answered",
                          {"approved": approved}, ok=approved)
    return approved


async def ask_user(question: str) -> str:
    """Send a question; the next plain text message is the answer. Timeout raises."""
    global _pending_ask
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_ask = fut
    await send_text(f"❓ {question}")
    if state.current is not None:
        state.audit_event(state.current, "user_input", "question", {"question": question})
    try:
        return await asyncio.wait_for(fut, config.CONFIRM_TIMEOUT)
    finally:
        if _pending_ask is fut:
            _pending_ask = None


# --- status card ---
def _render_status(task: TaskRecord) -> str:
    lines = [f"▶️ [{task.origin}] {state.redact_text(task.text)}"]
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
    """Send final result, evidence, generated artifacts, and a detailed audit."""
    if _app is None or _allowed_chat_id == 0:
        return
    lines = [f"[{task.status.upper()}] {state.redact_text(task.text)}"]
    if task.result:
        lines.append(f"Result: {task.result}")
    if task.needs:
        lines.append(f"Needs: {task.needs}")
    meta = f"Steps: {len(task.steps)}"
    if task.started is not None and task.finished is not None:
        meta += f" | Duration: {task.finished - task.started:.0f}s"
    lines.append(meta)
    if task.sources:
        lines.append("Sources:")
        lines.extend(f"- {url}" for url in task.sources[:10])
    await _retry(_app.bot.send_message, chat_id=_allowed_chat_id,
                 text=state.redact_text("\n".join(lines))[:4000])
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

    audit_path = state.write_audit(task)
    files = [p for p in task.artifacts if p != screenshot_path]
    files.append(str(audit_path))
    sent_paths: set[str] = set()
    for raw in files:
        if raw in sent_paths:
            continue
        sent_paths.add(raw)
        try:
            with open(raw, "rb") as f:
                data = f.read()
            await _retry(
                _app.bot.send_document,
                chat_id=_allowed_chat_id,
                document=InputFile(data, filename=Path(raw).name),
                caption=f"Task {task.id}: {Path(raw).name}",
            )
        except OSError as e:
            state.log_event({"event": "artifact_failed", "task": task.id,
                             "path": raw, "error": str(e)})


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


# --- file upload helpers (pure, no Telegram objects — unit-testable) ---
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# \b treats "_" as a word char, which would miss "my_resume.pdf" — use an explicit
# non-alnum/start/end boundary instead so underscore/hyphen/dot separators all count.
_RESUME_HINT_RE = re.compile(r"(?:^|[^a-z0-9])(resum[eé]|cv)(?:$|[^a-z0-9])", re.I)
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # Telegram Bot API can't fetch files bigger than this
_UPLOAD_RECENCY_SECS = 48 * 3600


def _sanitize_filename(name: str, fallback_ext: str = "") -> str:
    """Strip path traversal / unsafe chars / reserved names. Never trust file_name as a path."""
    name = Path((name or "").strip()).name or f"upload{fallback_ext}"  # .name kills traversal
    name = _UNSAFE_CHARS.sub("_", name).strip(" .")
    if not name:
        name = f"upload{fallback_ext}"
    stem, ext = os.path.splitext(name)
    if len(name) > 150:
        name = stem[: 150 - len(ext)] + ext
        stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED_NAMES:
        name = f"_{name}"
    return name


def _dedupe_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        candidate = directory / f"{stem} ({i}){ext}"
        if not candidate.exists():
            return candidate
        i += 1


def _extract_file_meta(msg):
    """Resolve the Telegram attachment object + a suggested filename, or (None, None)."""
    if msg.document:
        return msg.document, msg.document.file_name or "document"
    if msg.photo:
        p = msg.photo[-1]  # largest size
        return p, f"photo_{p.file_unique_id}.jpg"
    if msg.audio:
        return msg.audio, msg.audio.file_name or f"audio_{msg.audio.file_unique_id}.mp3"
    if msg.voice:
        return msg.voice, f"voice_{msg.voice.file_unique_id}.ogg"
    if msg.video:
        return msg.video, msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
    return None, None


def _looks_like_resume(filename: str, caption: str | None) -> bool:
    hay = f"{filename} {caption or ''}"
    return bool(_RESUME_HINT_RE.search(hay)) and Path(filename).suffix.lower() in {".pdf", ".doc", ".docx"}


def _with_upload_context(text: str) -> str:
    """Point 'it'/'that file' at the most recent upload, if one is recent and still exists."""
    extra = profile_store.load()
    path, ts = extra.get("last_upload"), extra.get("last_upload_ts")
    if not path or not ts:
        return text
    try:
        age = time.time() - float(ts)
    except ValueError:
        return text
    if age > _UPLOAD_RECENCY_SECS or not Path(path).is_file():
        return text
    return f"{text}\n(Most recently uploaded file: {path})"


# --- handlers ---
_SETUP_PROMPT = (
    "Quick one-time setup — reply with your name, age, email, and phone, comma-separated "
    "(use \"-\" to skip any), e.g.:\nAlex Kim, 24, alex@example.com, 555-010-4477\n"
    "Or send /skip to do this later with /remember."
)


async def _maybe_start_onboarding(update: Update) -> None:
    global _pending_setup
    if profile_store.load().get("onboarded") == "yes" or _pending_setup is not None:
        return
    _pending_setup = asyncio.get_running_loop().create_future()
    await update.message.reply_text(_SETUP_PROMPT)


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _allowed_chat_id
    chat_id = update.effective_chat.id
    if _allowed_chat_id == 0:
        _allowed_chat_id = chat_id
        _write_env_chat_id(chat_id)
        state.log_event({"event": "owner_captured", "chat_id": chat_id})
        await update.message.reply_text("You're registered as owner")
        await _maybe_start_onboarding(update)
        return
    if chat_id != _allowed_chat_id:
        return
    await update.message.reply_text(
        "Pocket Agent ready. Send a task, "
        "/brief, /status, /cancel.")
    await _maybe_start_onboarding(update)


async def _on_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_setup
    if not _is_allowed(update):
        return
    _pending_setup = asyncio.get_running_loop().create_future()
    await update.message.reply_text(_SETUP_PROMPT)


async def _on_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_setup
    if not _is_allowed(update):
        return
    if _pending_setup is not None and not _pending_setup.done():
        _pending_setup.cancel()
    _pending_setup = None
    profile_store.upsert("onboarded", "yes")
    await update.message.reply_text("Skipped — fill this in anytime with /remember.")


async def _handle_setup_reply(update: Update, text: str) -> None:
    global _pending_setup
    fut, _pending_setup = _pending_setup, None
    if fut is not None and not fut.done():
        fut.set_result(text)

    parts = [p.strip() for p in text.split(",")]
    fields = ("name", "age", "email", "phone")
    parsed: dict[str, str] = {}
    problems: list[str] = []
    for i, field in enumerate(fields):
        val = parts[i] if i < len(parts) else ""
        if val in ("", "-"):
            continue
        if field == "age" and not (val.isdigit() and 0 < int(val) < 130):
            problems.append(f"age '{val}' skipped")
            continue
        if field == "email" and ("@" not in val or "." not in val.split("@")[-1]):
            problems.append(f"email '{val}' skipped")
            continue
        if field == "phone" and len(re.sub(r"\D", "", val)) < 7:
            problems.append(f"phone '{val}' skipped")
            continue
        parsed[field] = val

    for k, v in parsed.items():
        profile_store.upsert(k, v)
    profile_store.upsert("onboarded", "yes")  # one-time, even on a bad/partial reply
    state.log_event({"event": "onboarding_completed", "fields": sorted(parsed)})

    msg = (f"Saved: {', '.join(f'{k}={v}' for k, v in parsed.items())}."
           if parsed else "Didn't save anything usable.")
    if problems:
        msg += " " + " / ".join(problems)
    msg += " Fix anytime with /remember key: value."
    await update.message.reply_text(msg)


async def _on_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    data = {k: v for k, v in profile_store.load().items() if k != "onboarded"}
    if not data:
        await update.message.reply_text("Nothing remembered yet.")
        return
    await update.message.reply_text("\n".join(f"{k}: {v}" for k, v in sorted(data.items())))


async def _on_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    key = (update.message.text or "").partition(" ")[2].strip()
    if not key:
        await update.message.reply_text("Usage: /forget key")
        return
    ok = profile_store.delete(key)
    await update.message.reply_text(f"Forgot '{key}'." if ok else f"Didn't have '{key}'.")


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_ask, _pending_secret
    if not _is_allowed(update):
        return
    text = update.message.text
    if _pending_secret is not None:
        fut, kind, domain, task_id = _pending_secret
        _pending_secret = None
        if not fut.done():
            handle = "secret:" + uuid.uuid4().hex
            state.register_secret(text)
            _secrets[handle] = _Secret(
                value=text,
                expires=time.monotonic() + config.CONFIRM_TIMEOUT,
                task_id=task_id,
                kind=kind,
                domain=domain,
            )
            fut.set_result(handle)
        return
    if _pending_setup is not None:
        await _handle_setup_reply(update, text)
        return
    if _pending_ask is not None and not _pending_ask.done():
        fut, _pending_ask = _pending_ask, None
        fut.set_result(text)
        return
    text = _with_upload_context(text)
    ahead = state.queue.qsize() + (1 if state.current is not None else 0)
    task = state.new_task(text, origin="telegram")
    await update.message.reply_text(f"Queued as task {task.id} ({ahead} ahead of it)")


async def _on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    msg = update.message
    tg_obj, suggested_name = _extract_file_meta(msg)
    if tg_obj is None:
        return
    size = getattr(tg_obj, "file_size", None)
    if size is not None and size > _MAX_UPLOAD_BYTES:
        await update.message.reply_text(
            f"That's {size / 1_048_576:.1f} MB — Telegram bots can only fetch files up to "
            "20 MB. Send a smaller file, or share a direct link instead.")
        return
    file = await _retry(context.bot.get_file, tg_obj.file_id)
    if file is None:
        await update.message.reply_text("Couldn't download that after a few tries — please resend.")
        return
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_filename(suggested_name)
    dest = _dedupe_path(config.UPLOAD_DIR, safe_name)
    ok = await _retry(file.download_to_drive, custom_path=str(dest))
    if ok is None:
        await update.message.reply_text("Download failed partway through — please resend.")
        return

    state.log_event({"event": "upload_saved", "path": str(dest), "size": size})
    profile_store.upsert("last_upload", str(dest))
    profile_store.upsert("last_upload_ts", str(time.time()))
    if _looks_like_resume(safe_name, msg.caption):
        profile_store.upsert("resume_path", str(dest))

    caption = (msg.caption or "").strip()
    if caption:
        text = f"{caption}\n(Uploaded file available at: {dest})"
        ahead = state.queue.qsize() + (1 if state.current is not None else 0)
        task = state.new_task(text, origin="telegram")
        await update.message.reply_text(
            f"Saved {safe_name} and queued as task {task.id} ({ahead} ahead of it).")
    else:
        await update.message.reply_text(
            f"Saved {safe_name}. Say what to do with it whenever you're ready — "
            "I'll remember it as your most recent upload.")


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
    global _pending_ask, _pending_secret, _pending_setup
    if not _is_allowed(update):
        return
    for cid in list(_pending_confirms):
        fut = _pending_confirms.pop(cid, None)
        if fut is not None and not fut.done():
            fut.set_result(False)
    if _pending_ask is not None and not _pending_ask.done():
        _pending_ask.cancel()
    _pending_ask = None
    if _pending_setup is not None and not _pending_setup.done():
        _pending_setup.cancel()
    _pending_setup = None
    if _pending_secret is not None:
        fut = _pending_secret[0]
        if not fut.done():
            fut.cancel()
        _pending_secret = None
    if _current_run_task is not None and not _current_run_task.done():
        if state.current is not None:
            state.mark(state.current, "cancelled")
            clear_task_secrets(state.current.id)
        _current_run_task.cancel()
    await update.message.reply_text("Stopped.")


async def _on_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    task = state.new_task(
        "Scan my email inbox from the last 24 hours using scan_inbox, then give me a short "
        "briefing: summarize what's new and flag only what looks important (things needing a "
        "reply, deadlines, money, real people writing directly). Skip routine newsletters/promos.",
        origin="telegram")
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
        profile_store.upsert(key, value)
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
        await update_status(task, "starting")
        run = asyncio.create_task(_run_agent(task))
        _current_run_task = run
        try:
            # No wall-clock cap here: waiting on the human (ask_user/confirm) must not
            # kill the task. Machine time is bounded inside the agent loop instead
            # (MAX_STEPS x MODEL_CALL_TIMEOUT) and each human wait by CONFIRM_TIMEOUT.
            result = await run
            if result.startswith("INCOMPLETE:"):
                task.needs = result.removeprefix("INCOMPLETE:").strip()
                state.mark(task, "needs_attention", result)
            else:
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
            finally:
                clear_task_secrets(task.id)


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
    app.add_handler(CommandHandler("setup", _on_setup))
    app.add_handler(CommandHandler("skip", _on_skip))
    app.add_handler(CommandHandler("memory", _on_memory))
    app.add_handler(CommandHandler("forget", _on_forget))
    app.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^cfm:"))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO | filters.VOICE)
        & ~filters.COMMAND,
        _on_file))
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
    clear_task_secrets(None)
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
