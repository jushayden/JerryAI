"""Telegram bridge.

Everything runs on python-telegram-bot's single asyncio loop. Incoming tasks
are scheduled with application.create_task so handlers return immediately;
gated tools suspend on an asyncio.Future that the Approve/Deny callback
resolves on the same loop.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, platform_status
from gate import card_html
from report_social import split_message
from state import EventLog


class PendingApprovals:
    def __init__(self):
        self._pending: dict[str, asyncio.Future] = {}

    def create(self) -> tuple[str, asyncio.Future]:
        approval_id = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = fut
        return approval_id, fut

    def resolve(self, approval_id: str, approved: bool) -> bool:
        fut = self._pending.pop(approval_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def discard_all(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()


class TelegramNotifier:
    """Notifier + Approver bound to the owner's chat."""

    def __init__(self, bot, chat_id: int, pending: PendingApprovals, log: EventLog):
        self.bot = bot
        self.chat_id = chat_id
        self.pending = pending
        self.log = log

    async def send_text(self, text: str) -> None:
        for chunk in split_message(text):
            try:
                await self.bot.send_message(self.chat_id, chunk, parse_mode="HTML",
                                            disable_web_page_preview=True)
            except Exception:
                # HTML parse failure on odd content — fall back to plain text
                await self.bot.send_message(self.chat_id, chunk)

    async def send_photos(self, paths: list[Path], caption: str = "") -> None:
        paths = [p for p in paths if Path(p).is_file()]
        for i in range(0, len(paths), 10):
            batch = paths[i:i + 10]
            if len(batch) == 1:
                with open(batch[0], "rb") as f:
                    await self.bot.send_photo(self.chat_id, f,
                                              caption=caption[:1024] if i == 0 else None)
                continue
            media, handles = [], []
            try:
                for j, p in enumerate(batch):
                    f = open(p, "rb")
                    handles.append(f)
                    media.append(InputMediaPhoto(
                        f, caption=caption[:1024] if (i == 0 and j == 0) else None))
                await self.bot.send_media_group(self.chat_id, media)
            finally:
                for f in handles:
                    f.close()

    async def request_approval(self, *, platform: str, action: str,
                               payload_text: str, meta: dict, timeout_s: int) -> bool | None:
        approval_id, fut = self.pending.create()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"ap:{approval_id}:1"),
            InlineKeyboardButton("❌ Deny", callback_data=f"ap:{approval_id}:0"),
        ]])
        msg = await self.bot.send_message(
            self.chat_id, card_html(action, platform, payload_text),
            reply_markup=keyboard, parse_mode="HTML",
        )
        try:
            verdict = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self.pending.discard(approval_id)
            await self._edit_card(msg, "⏰ Timed out — denied")
            return None
        except asyncio.CancelledError:
            self.pending.discard(approval_id)
            await self._edit_card(msg, "🚫 Task cancelled")
            raise
        await self._edit_card(msg, "✅ Approved" if verdict else "❌ Denied")
        return verdict

    async def _edit_card(self, msg, verdict_line: str) -> None:
        try:
            await msg.edit_text(msg.text_html + f"\n\n<b>{verdict_line}</b>",
                                parse_mode="HTML", reply_markup=None)
        except Exception:
            pass  # cosmetic only


def run_bridge(cfg: Config, build_deps) -> None:
    pending = PendingApprovals()
    app_state: dict = {"current_task": None, "agent": None, "make_ctx": None,
                       "notifier": None}
    owner_filter = filters.User(user_id=cfg.telegram_owner_id)

    async def post_init(application: Application) -> None:
        notifier = TelegramNotifier(
            application.bot, cfg.telegram_owner_id, pending,
            EventLog(cfg.state_dir / "events.jsonl"),
        )
        agent, make_ctx = build_deps(cfg, notifier)
        app_state.update(agent=agent, make_ctx=make_ctx, notifier=notifier)
        try:
            await application.bot.send_message(
                cfg.telegram_owner_id,
                "🤖 JerryAI online. Send me a task, /status, or /cancel.")
        except Exception:
            # Owner hasn't opened a chat with the bot yet (send /start first) — don't
            # let a startup notification failure crash the whole bridge.
            pass

    def _is_owner(update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id == cfg.telegram_owner_id)

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update) or not update.message or not update.message.text:
            return  # silently ignore strangers
        task = app_state["current_task"]
        if task is not None and not task.done():
            await update.message.reply_text("⏳ Busy with the current task — /cancel to abort.")
            return
        text = update.message.text.strip()
        ctx, cleaned = app_state["make_ctx"](text)
        note = " (writes pre-authorized)" if ctx.pre_authorized else ""
        await update.message.reply_text(f"🤖 On it…{note}")
        app_state["current_task"] = context.application.create_task(
            _run_and_report(cleaned, ctx))

    async def _run_and_report(task_text: str, ctx) -> None:
        notifier: TelegramNotifier = app_state["notifier"]
        try:
            answer = await app_state["agent"].run_task(task_text, ctx)
            await notifier.send_text(html.escape(answer))
        except asyncio.CancelledError:
            await notifier.send_text("🚫 Task cancelled.")
        except Exception as e:
            await notifier.send_text(
                f"💥 Task failed: {html.escape(f'{type(e).__name__}: {e}')}")
        finally:
            app_state["current_task"] = None

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()  # ALWAYS answer first — stops the client spinner
        if not _is_owner(update):
            return
        try:
            _, approval_id, flag = query.data.split(":")
        except ValueError:
            return
        if not pending.resolve(approval_id, flag == "1"):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await context.bot.send_message(cfg.telegram_owner_id,
                                           "That approval was already resolved or expired.")

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update):
            return
        await update.message.reply_text(
            "🤖 JerryAI ready.\n"
            "• Send any task in plain language.\n"
            "• Prefix with ! to pre-authorize posts/deletes for that task.\n"
            "• /status — platform readiness\n"
            "• /cancel — abort the current task"
        )

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update):
            return
        lines = [f"Model: {cfg.ollama_model}"]
        for platform, (ready, reason) in platform_status(cfg).items():
            lines.append(f"{'✅' if ready else '⚪'} {platform}: {reason}")
        busy = app_state["current_task"] is not None and not app_state["current_task"].done()
        lines.append(f"Task running: {'yes' if busy else 'no'}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update):
            return
        task = app_state["current_task"]
        if task is not None and not task.done():
            pending.discard_all()
            task.cancel()
            await update.message.reply_text("🚫 Cancelling…")
        else:
            await update.message.reply_text("Nothing is running.")

    application = (ApplicationBuilder()
                   .token(cfg.telegram_bot_token)
                   .post_init(post_init)
                   .build())
    application.add_handler(CommandHandler("start", cmd_start, filters=owner_filter))
    application.add_handler(CommandHandler("status", cmd_status, filters=owner_filter))
    application.add_handler(CommandHandler("cancel", cmd_cancel, filters=owner_filter))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & owner_filter, on_message))
    application.add_handler(CallbackQueryHandler(on_callback, pattern=r"^ap:"))
    application.run_polling(allowed_updates=["message", "callback_query"])
