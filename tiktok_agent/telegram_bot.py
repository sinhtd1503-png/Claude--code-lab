"""Telegram bot: sends each generated video for review, publishes only on approval.

Commands: /run (chạy pipeline ngay) · /status · /chatid
Buttons:  ✅ Duyệt & đăng · ✏️ Sửa (gửi góp ý) · 🔄 Làm lại · ❌ Bỏ
Free AI clips (ai_video.provider = manual_flow): the bot sends one Google Flow prompt per shot; reply to a
prompt with the generated video and the agent renders automatically once every shot has arrived.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram.error import TimedOut
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
        self.app = (Application.builder().token(self.s.telegram_bot_token)
                    .connect_timeout(30).read_timeout(120).write_timeout(120)
                    .media_write_timeout(300).pool_timeout(60).build())
        self._busy = asyncio.Lock()

        # Without TELEGRAM_CHAT_ID the bot answers only /chatid — nobody else can trigger it.
        only_me = filters.Chat(chat_id=self.chat_id or [])
        self.app.add_handler(CommandHandler("chatid", self.cmd_chatid))
        self.app.add_handler(CommandHandler("run", self.cmd_run, filters=only_me))
        self.app.add_handler(CommandHandler("status", self.cmd_status, filters=only_me))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, self.on_text))
        self.app.add_handler(MessageHandler(
            only_me & (filters.VIDEO | filters.Document.VIDEO | filters.ANIMATION), self.on_clip))

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
        msg = None
        for attempt in range(1, 4):  # uploads from some networks occasionally time out
            try:
                with open(item["video_path"], "rb") as fh:
                    msg = await self.app.bot.send_video(self.chat_id, fh, caption=f"#{cid} – {sc['title']}"[:1000],
                                                        supports_streaming=True)
                break
            except TimedOut:
                if attempt == 3:
                    raise
                log.warning("send_video timed out (attempt %d), retrying…", attempt)
                await asyncio.sleep(5 * attempt)
        await self.app.bot.send_message(self.chat_id, text[:4096], parse_mode="HTML",
                                        reply_markup=_keyboard(cid))
        self.p.db.update_content(cid, telegram_message_id=msg.message_id)

    async def dispatch(self, cid: int) -> None:
        """Send whatever the content needs next: Flow prompts, or the finished video for review."""
        if self.p.db.get_content(cid)["status"] == "awaiting_clips":
            await self.send_clip_requests(cid)
        else:
            await self.send_for_review(cid)

    async def send_clip_requests(self, cid: int) -> None:
        m = self.p.manual.load(cid)
        title = self.p.db.get_content(cid)["script"]["title"]
        n = len(m["shots"])
        intro = (f"🎨 <b>#{cid} – {html.escape(title)}</b>\n"
                 f"Cần {n} clip AI miễn phí từ Google Flow (labs.google/flow):\n"
                 "1. Chọn khung <b>9:16</b>, thời lượng <b>8 giây</b>, model rẻ nhất (vd Gemini Omni Flash)\n"
                 "2. Copy prompt bên dưới → tạo video → tải về\n"
                 f"3. Gửi lại cho bot: <b>chọn cả {n} video gửi một lần theo đúng thứ tự shot 1→{n}</b>, "
                 "hoặc reply từng tin nhắn prompt bằng video tương ứng\n"
                 "Đủ clip là bot tự ghép với giọng đọc + phụ đề và gửi bạn duyệt. Chỗ nào không có clip sẽ dùng Pixabay.")
        await self.app.bot.send_message(self.chat_id, intro, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏩ Dựng luôn (thiếu clip dùng Pixabay)", callback_data=f"render:{cid}")]]))
        for i, shot in enumerate(m["shots"]):
            msg = await self.app.bot.send_message(
                self.chat_id,
                f"🎬 #{cid} · Shot {i + 1}/{n} (cảnh {shot['scene']}, dùng {shot['seconds']}s đầu)\n"
                f"<pre>{html.escape(shot['prompt'])}</pre>", parse_mode="HTML")
            shot["tg_message_id"] = msg.message_id
        self.p.manual.save(cid, m)

    async def _render_and_send(self, cid: int) -> None:
        await self.app.bot.send_message(self.chat_id, f"🎞 Đang dựng video #{cid}…")
        try:
            async with self._busy:  # renders share the AI source state; run one at a time
                await asyncio.to_thread(self.p.render_content, cid)
        except Exception as e:
            await self.app.bot.send_message(self.chat_id, f"⚠️ Lỗi dựng #{cid}: {e}"[:4000])
            return
        await self.send_for_review(cid)

    async def on_clip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        if not self.p.manual:
            await msg.reply_text("Chế độ nhận clip đang tắt (ai_video.provider khác manual_flow).")
            return
        target = None
        if msg.reply_to_message:
            target = self.p.manual.find_by_message(msg.reply_to_message.message_id)
        if target is None:  # not a reply → fill the first missing shot of the newest waiting video
            for row in self.p.db.conn.execute(
                    "SELECT id FROM content WHERE status='awaiting_clips' ORDER BY id DESC").fetchall():
                m = self.p.manual.load(row["id"])
                if m and self.p.manual.missing(m):
                    target = (row["id"], self.p.manual.missing(m)[0])
                    break
        if target is None:
            await msg.reply_text("Không có video nào đang chờ clip. Hãy reply đúng tin nhắn prompt.")
            return
        cid, idx = target
        media = msg.video or msg.document or msg.animation
        if media.file_size and media.file_size > 20 * 1024 * 1024:
            await msg.reply_text("File >20MB, bot Telegram không tải được. Hãy gửi dạng video (không phải file) "
                                 "để Telegram tự nén, hoặc tải bản 720p.")
            return
        tmp = Path(tempfile.mkdtemp()) / "clip.mp4"
        await (await media.get_file()).download_to_drive(tmp)
        m = self.p.manual.attach(cid, idx, tmp)
        left = self.p.manual.missing(m)
        await msg.reply_text(f"✅ Đã nhận shot {idx + 1}/{len(m['shots'])} cho #{cid}. "
                             + (f"Còn thiếu {len(left)} shot." if left else "Đủ rồi, đang dựng video…"))
        if not left:
            await self._render_and_send(cid)

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
                await self.dispatch(cid)

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

        if action == "render":
            await q.edit_message_reply_markup(None)
            if item["status"] == "awaiting_clips":
                await self._render_and_send(cid)
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
        await self.dispatch(cid)

    def run(self) -> None:
        if not self.chat_id:
            log.warning("TELEGRAM_CHAT_ID chưa đặt: gửi /chatid cho bot để lấy, rồi điền vào .env")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
