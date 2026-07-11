"""Telegram phone bridge for Pocket Agent (Track A).

Manual PTB v22 init — main.py owns the event loop, never app.run_polling().
"""
import asyncio
import os
import re
import tempfile
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
import schedule
import state
import voice
from state import TaskRecord

# --- module state ---
_app: Application | None = None
_worker_task: asyncio.Task | None = None
_scheduler_task: asyncio.Task | None = None
_current_run_task: asyncio.Task | None = None  # wraps the running agent call (for /cancel)
_allowed_chat_id: int = config.ALLOWED_CHAT_ID
_pending_confirms: dict[str, asyncio.Future] = {}  # cid -> Future[bool]
_pending_ask: asyncio.Future | None = None  # Future[str] resolved by next plain text
_pending_secret: tuple[asyncio.Future, str, str, str | None] | None = None
_pending_setup: asyncio.Future | None = None  # Future[str] resolved by the onboarding reply
_sched_wizard: dict | None = None  # in-progress /schedule setup for the owner
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
_SPIN = "🌑🌒🌓🌔🌕🌖🌗🌘"  # phases advance every ticker beat -> a live loading animation

_FRIENDLY_STEPS = {
    "browser_goto": "🌐 opening", "read_page": "📖 reading the page",
    "extract_form_fields": "🔎 scanning the form", "find_elements": "🔎 finding controls",
    "fill_field": "✏️ filling", "choose_option": "✏️ choosing", "select_option": "✏️ choosing",
    "fill_secret_field": "🔐 entering secret", "upload_file": "📎 attaching",
    "click_element": "🖱️ clicking", "download_element": "⬇️ downloading",
    "screenshot_page": "📸 screenshot", "scrape_page": "🗂️ extracting data",
    "visual_inspect": "👁️ looking at the page", "visual_click": "👁️ clicking visually",
    "web_agent": "🧭 starting a web mission:", "scan_inbox": "📬 reading your inbox",
    "send_email": "✉️ sending email", "reply_email": "✉️ replying",
    "create_draft": "📝 drafting email", "trash_email": "🗑️ trashing email",
    "reddit_search": "🔎 searching Reddit", "reddit_feed": "🔎 browsing Reddit",
    "youtube_search": "🔎 searching YouTube", "list_uploads": "📁 checking your files",
    "read_profile": "👤 reading your profile", "remember_fact": "🧠 remembering that",
    "write_file": "💾 writing", "read_file": "📖 reading", "list_dir": "📁 listing",
    "create_folder": "📁 creating folder", "delete_file": "🗑️ deleting",
    "move_file": "📦 moving", "open_app": "🚀 opening app", "take_screenshot": "📸 screenshot",
    "pick_file": "📁 finding a file", "set_volume": "🔊 volume", "mute": "🔇 muting",
    "set_brightness": "💡 brightness", "media_control": "⏯️ media", "lock_pc": "🔒 locking",
    "power_action": "⏻ power action", "system_status": "🎛️ checking system",
    "ask_user": "💬 asking you", "request_confirmation": "💬 asking your approval",
    "request_secret": "🔐 asking you for a secret",
    "list_tabs": "🗂️ checking tabs", "switch_tab": "🗂️ switching tab",
    "new_tab": "🗂️ new tab", "close_tab": "🗂️ closing tab", "scroll_page": "↕️ scrolling",
}


def _friendly_step(s: str) -> str:
    if s.startswith("web_agent step"):
        return f"🧭 {s[10:].strip()}"
    name, sep, rest = s.partition("(")
    label = _FRIENDLY_STEPS.get(name.strip())
    if label is None:
        return s[:90]
    hint = rest[:-1] if rest.endswith(")") else rest
    hint = hint.strip().strip("{}")[:60]
    return f"{label} {hint}".strip()


def _render_status(task: TaskRecord, frame: int = 0) -> str:
    spin = _SPIN[frame % len(_SPIN)]
    lines = [f"{spin} On it — {state.redact_text(task.text)[:200]}"]
    lines += [f"→ {_friendly_step(s)}" for s in task.steps[-6:]]
    return "\n".join(lines)


async def _ticker(task: TaskRecord, card: dict) -> None:
    """Animate the card while the task runs: spin the loader + native typing cue."""
    try:
        while task.status in ("queued", "running") and _app is not None:
            await asyncio.sleep(2.5)
            if task.status not in ("queued", "running") or card["msg_id"] is None:
                break
            card["spin"] = (card.get("spin", 0) + 1) % len(_SPIN)
            if card["flusher"] is not None:  # a real step edit is already queued
                continue
            try:
                await _app.bot.send_chat_action(chat_id=_allowed_chat_id, action="typing")
                await _app.bot.edit_message_text(
                    chat_id=_allowed_chat_id, message_id=card["msg_id"],
                    text=_render_status(task, card["spin"]))
                card["last"] = time.monotonic()
            except Exception:
                pass  # rate limit / not-modified: skip this beat
    finally:
        card["ticker"] = None


async def _finish_status_card(task: TaskRecord) -> None:
    """Stop the animation and stamp the card with the final verdict."""
    card = _status_cards.pop(task.id, None)
    if card is None:
        return
    for key in ("ticker", "flusher"):
        t = card.get(key)
        if t is not None:
            t.cancel()
    if card.get("msg_id") is not None and _app is not None:
        icon = {"done": "✅", "failed": "❌", "cancelled": "🛑"}.get(task.status, "⚠️")
        lines = [f"{icon} {state.redact_text(task.text)[:200]}"]
        lines += [f"→ {_friendly_step(s)}" for s in task.steps[-4:]]
        try:
            await _app.bot.edit_message_text(
                chat_id=_allowed_chat_id, message_id=card["msg_id"], text="\n".join(lines))
        except Exception:
            pass


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
        task.id, {"msg_id": None, "last": 0.0, "pending": None, "flusher": None,
                  "ticker": None, "spin": 0})
    text = _render_status(task, card.get("spin", 0))
    if card["msg_id"] is None:
        try:
            msg = await _app.bot.send_message(chat_id=_allowed_chat_id, text=text)
            card["msg_id"] = msg.message_id
            task.status_msg_id = msg.message_id
            card["last"] = time.monotonic()
            card["ticker"] = asyncio.create_task(_ticker(task, card))
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
    """Send final result, evidence, and any generated artifacts (downloads).
    The audit trail is written to disk but NOT pushed to the phone."""
    if _app is None or _allowed_chat_id == 0:
        return
    # Clean, human report: just the answer. Only tag when something needs attention —
    # no "[DONE]" echo, no step/duration meta.
    if task.status == "done":
        lines = [task.result or "Done."]
    else:
        tag = {"failed": "⚠️ That didn't work",
               "needs_attention": "⚠️ I need you",
               "cancelled": "Stopped."}.get(task.status, task.status)
        lines = [f"{tag} {task.needs or task.result or ''}".strip()]
    if task.sources:
        lines.append("\nSources: " + " · ".join(task.sources[:5]))
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

    state.write_audit(task)  # kept as a local record; not sent to the phone
    files = [p for p in task.artifacts if p != screenshot_path]
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


async def _dispatch_text(text: str, update: Update | None = None) -> None:
    """Route a user message (typed OR transcribed from voice) into the ask/queue pipeline."""
    global _pending_ask
    if _pending_ask is not None and not _pending_ask.done():
        fut, _pending_ask = _pending_ask, None
        fut.set_result(text)
        return
    text = _with_upload_context(text)
    ahead = state.queue.qsize() + (1 if state.current is not None else 0)
    task = state.new_task(text, origin="telegram")
    reply = f"Queued as task {task.id} ({ahead} ahead of it)"
    if update is not None:
        await update.message.reply_text(reply)
    else:
        await send_text(reply)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_secret
    if not _is_allowed(update):
        return
    text = update.message.text
    # Typed-only flows first: secrets, onboarding, and the /schedule wizard must never
    # be fed from a voice transcription.
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
    if _sched_wizard is not None:
        await _wizard_step(text)
        return
    await _dispatch_text(text, update)


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
    global _pending_ask, _pending_secret, _pending_setup, _sched_wizard
    if not _is_allowed(update):
        return
    _sched_wizard = None  # abort any in-progress /schedule setup
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
        "Scan my email inbox from the last 24 hours using scan_inbox, then text me a quick, "
        "human briefing. Lead with anything that actually needs me (a reply, a deadline, money, "
        "a real person writing directly, a security alert). Then one line on the rest. Keep it "
        "short and natural — a few bullets max, no preamble, no markdown headers.",
        origin="telegram")
    await update.message.reply_text(f"On it — inbox briefing queued as task {task.id}.")


async def _on_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    task = state.new_task(
        "Read my profile (read_profile) and find the interests: list. For each interest, "
        "search DuckDuckGo's HTML site (https://html.duckduckgo.com/html/?q=...) using "
        "browser_goto and read the results with read_page, then open the top article or two "
        "and read those too. Give me a short news briefing grouped by interest — what's new, "
        "one or two lines each, with a source link. Skip anything older than a few days.")
    await update.message.reply_text(f"On it — news briefing queued as task {task.id}.")


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


# --- scheduler ---
_WHEN_HELP = (
    "When should it run? Examples:\n"
    "• daily 08:00\n"
    "• weekdays 09:30\n"
    "• weekly mon 07:00\n"
    "• every 2h   /   every 30m\n"
    "• once 2026-07-12 14:00"
)


def _enqueue_scheduled(text: str, job_id: str) -> None:
    """Enqueue a due scheduled job as a normal task (high-impact actions gate as usual)."""
    task = state.new_task(text)
    if _app is not None and _allowed_chat_id != 0:
        asyncio.create_task(send_text(f"⏰ Scheduled {job_id} started: {task.text[:60]}"))


async def _on_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _sched_wizard
    if not _is_allowed(update):
        return
    _sched_wizard = {"step": "text", "text": None, "spec": None}
    await update.message.reply_text(
        "New scheduled task. What should I do each time? Send the task text.\n"
        "(/cancel to abort)")


async def _wizard_step(text: str) -> None:
    """Advance the /schedule wizard with the owner's latest message."""
    global _sched_wizard
    w = _sched_wizard
    if w is None:
        return
    if w["step"] == "text":
        if not text.strip():
            await send_text("Send the task text — what should I do each time?")
            return
        w["text"], w["step"] = text.strip(), "spec"
        await send_text(_WHEN_HELP)
    elif w["step"] == "spec":
        try:
            job = schedule.add_job(w["text"], " ".join(text.split()))
        except ValueError as e:
            await send_text(f"{e}\n\n{_WHEN_HELP}")
            return
        _sched_wizard = None
        await send_text(
            f"Scheduled ✓ [{job['id']}] \"{job['text'][:60]}\"\n"
            f"{schedule.describe(job)} · high-impact actions still ask on your phone\n"
            f"Next run: {schedule.fmt_next(job)}\n"
            f"/schedules to view · /unschedule {job['id']} to remove")


async def _on_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    js = schedule.jobs()
    if not js:
        await update.message.reply_text("No scheduled tasks. /schedule to add one.")
        return
    lines = ["Scheduled tasks:"]
    for j in js:
        lines.append(f"[{j['id']}] {j['text'][:50]}\n   {schedule.describe(j)} · "
                     f"next {schedule.fmt_next(j)}")
    await update.message.reply_text("\n".join(lines))


async def _on_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    jid = (update.message.text or "").partition(" ")[2].strip()
    if not jid:
        await update.message.reply_text("Usage: /unschedule <id>  (see /schedules)")
        return
    if schedule.remove_job(jid):
        await update.message.reply_text(f"Removed {jid}.")
    else:
        await update.message.reply_text(f"No scheduled task with id {jid}.")


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
        await update_status(task, "thinking about the best way to do this…")
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
                await _finish_status_card(task)
                await send_report(task, getattr(task, "screenshot", None))
            except Exception as e:
                state.log_event({"event": "report_failed", "task": task.id,
                                 "error": str(e)})
            finally:
                clear_task_secrets(task.id)


# --- lifecycle ---
async def start_bridge() -> None:
    """Build the PTB app, register handlers, start polling, spawn the worker + scheduler."""
    global _app, _worker_task, _scheduler_task
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
    app.add_handler(CommandHandler("news", _on_news))
    app.add_handler(CommandHandler("remember", _on_remember))
    app.add_handler(CommandHandler("schedule", _on_schedule))
    app.add_handler(CommandHandler("schedules", _on_schedules))
    app.add_handler(CommandHandler("unschedule", _on_unschedule))
    app.add_handler(CommandHandler("testconfirm", _on_testconfirm))
    app.add_handler(CommandHandler("setup", _on_setup))
    app.add_handler(CommandHandler("skip", _on_skip))
    app.add_handler(CommandHandler("memory", _on_memory))
    app.add_handler(CommandHandler("forget", _on_forget))
    app.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^cfm:"))
    # Voice notes are transcribed and run as tasks; other media are saved as uploads.
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO)
        & ~filters.COMMAND,
        _on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    _app = app
    _worker_task = asyncio.create_task(_worker())
    schedule.load()
    _scheduler_task = asyncio.create_task(
        schedule.run(_enqueue_scheduled,
                     owner_ready=lambda: _allowed_chat_id != 0,
                     log=state.log_event))
    state.log_event({"event": "bridge_started"})


async def stop_bridge() -> None:
    """Graceful shutdown: stop polling, stop app, cancel the worker + scheduler."""
    global _app, _worker_task, _scheduler_task, _current_run_task
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
    for t in (_worker_task, _scheduler_task):
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    _app = None
    _worker_task = None
    _scheduler_task = None
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
