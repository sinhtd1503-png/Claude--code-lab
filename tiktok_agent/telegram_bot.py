"""Telegram bot: sends each generated video for review, publishes only on approval.

Commands: /run (chạy pipeline ngay) · /status · /chatid
Buttons:  ✅ Duyệt & đăng · ✏️ Sửa (gửi góp ý) · 🔄 Làm lại · ❌ Bỏ
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .pipeline import Pipeline

log = logging.getLogger(__name__)


def _keyboard(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Duyệt & đăng", callback_data=f"approve:{cid}"),
         InlineKeyboardButton("✏️ Sửa", callback_data=f"edit:{cid}")],
        [InlineKeyboardButton("🔄 Làm lại", callback_data=f"regen:{cid}"),
         InlineKeyboardButton("❌ Bỏ", callback_data=f"reject:{cid}")],
    ])


class ReviewBot:
    def __init__(self, pipeline: Pipeline):
        self.p = pipeline
        self.s = pipeline.s
        self.chat_id = self.s.telegram_chat_id
        self.app = Application.builder().token(self.s.telegram_bot_token).build()
        self._busy = asyncio.Lock()

        # Without TELEGRAM_CHAT_ID the bot answers only /chatid — nobody else can trigger it.
        only_me = filters.Chat(chat_id=self.chat_id or [])
        self.app.add_handler(CommandHandler("chatid", self.cmd_chatid))
        self.app.add_handler(CommandHandler("run", self.cmd_run, filters=only_me))
        self.app.add_handler(CommandHandler("status", self.cmd_status, filters=only_me))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, self.on_text))

        sch = self.s["schedule"]
        h, m = map(int, sch["daily_time"].split(":"))
        self.app.job_queue.run_daily(self.job_daily, dt.time(h, m, tzinfo=ZoneInfo(sch["timezone"])))

    # ---------- sending ----------
    async def send_for_review(self, cid: int) -> None:
        item = self.p.db.get_content(cid)
        sc = item["script"]
        narration = "\n".join(f"{i + 1}. {s['narration']}" for i, s in enumerate(sc["scenes"]))
        text = (f"🎬 <b>#{cid} – {html.escape(sc['title'])}</b>\n\n"
                f"<b>Hook:</b> {html.escape(sc['hook'])}\n\n"
                f"<b>Kịch bản:</b>\n{html.escape(narration)}\n\n"
                f"<b>Caption:</b>\n{html.escape(item['caption'])}")
        with open(item["video_path"], "rb") as fh:
            msg = await self.app.bot.send_video(self.chat_id, fh, caption=f"#{cid} – {sc['title']}"[:1000],
                                                supports_streaming=True, read_timeout=120, write_timeout=120)
        await self.app.bot.send_message(self.chat_id, text[:4096], parse_mode="HTML",
                                        reply_markup=_keyboard(cid))
        self.p.db.update_content(cid, telegram_message_id=msg.message_id)

    async def _run_daily(self) -> None:
        if self._busy.locked():
            await self.app.bot.send_message(self.chat_id, "⏳ Pipeline đang chạy, bỏ qua lần này.")
            return
        async with self._busy:
            await self.app.bot.send_message(self.chat_id, "🔎 Đang quét đối thủ & tạo video…")
            try:
                ids = await asyncio.to_thread(self.p.daily)
            except Exception as e:
                log.exception("daily failed")
                await self.app.bot.send_message(self.chat_id, f"⚠️ Lỗi pipeline: {e}"[:4000])
                return
            if not ids:
                await self.app.bot.send_message(self.chat_id, "⚠️ Không tạo được video nào, xem log.")
            for cid in ids:
                await self.send_for_review(cid)

    # ---------- handlers ----------
    async def job_daily(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_daily()

    async def cmd_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"Chat ID của bạn: {update.effective_chat.id}")

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        asyncio.create_task(self._run_daily())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.p.db.conn.execute(
            "SELECT id, status, json_extract(script,'$.title') t FROM content ORDER BY id DESC LIMIT 10"
        ).fetchall()
        lines = [f"#{r['id']} [{r['status']}] {r['t'] or ''}" for r in rows] or ["Chưa có nội dung."]
        await update.message.reply_text("\n".join(lines))

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q.message.chat.id != self.chat_id:
            await q.answer("Không có quyền.", show_alert=True)
            return
        await q.answer()
        action, cid = q.data.split(":")
        cid = int(cid)
        item = self.p.db.get_content(cid)
        if not item:
            return
        if item["status"] in ("posted", "rejected"):
            await q.message.reply_text(f"#{cid} đã ở trạng thái {item['status']}.")
            return

        if action == "approve":
            await q.edit_message_reply_markup(None)
            self.p.db.update_content(cid, status="approved")
            try:
                pid = await asyncio.to_thread(self.p.publish, cid)
            except Exception as e:
                log.exception("publish failed")
                self.p.db.update_content(cid, status="pending_review", error=str(e)[:2000])
                await q.message.reply_text(f"⚠️ Đăng thất bại: {e}"[:4000], reply_markup=_keyboard(cid))
                return
            mode = self.s["tiktok"]["post_mode"]
            note = ("Video đã vào hộp thư/nháp TikTok — mở app TikTok để hoàn tất đăng. "
                    "Caption để dán:\n\n" + item["caption"]) if mode == "inbox" else "Đã đăng lên TikTok."
            await q.message.reply_text(f"✅ #{cid}: {note}\n(publish_id: {pid})")

        elif action == "reject":
            self.p.db.update_content(cid, status="rejected")
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(f"❌ Đã bỏ #{cid}.")

        elif action == "edit":
            context.bot_data["awaiting_feedback"] = cid
            await q.message.reply_text(f"✏️ Gửi góp ý cho #{cid} (vd: 'hook mạnh hơn, kể về bố thay vì mẹ').")

        elif action == "regen":
            await q.edit_message_reply_markup(None)
            await self._regenerate(cid, None)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cid = context.bot_data.pop("awaiting_feedback", None)
        if cid is None:
            await update.message.reply_text("Dùng /run để tạo video, /status để xem trạng thái.")
            return
        await self._regenerate(cid, update.message.text)

    async def _regenerate(self, cid: int, feedback: str | None) -> None:
        await self.app.bot.send_message(self.chat_id, f"🔄 Đang làm lại #{cid}…")
        try:
            await asyncio.to_thread(self.p.regenerate, cid, feedback)
        except Exception as e:
            await self.app.bot.send_message(self.chat_id, f"⚠️ Lỗi khi làm lại #{cid}: {e}"[:4000])
            return
        await self.send_for_review(cid)

    def run(self) -> None:
        if not self.chat_id:
            log.warning("TELEGRAM_CHAT_ID chưa đặt: gửi /chatid cho bot để lấy, rồi điền vào .env")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
