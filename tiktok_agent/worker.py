"""HTTP worker used by n8n.

n8n owns the schedule and every Telegram call; this service owns the state (SQLite), Claude calls,
budget checks, clip handling, video rendering and TikTok posting.

Every endpoint returns the same shape, so the n8n workflow can stay generic:
    {"messages": [<Telegram call>, ...], "next": {"action": "render", "id": 12} | null}
A Telegram call is {"method": "sendMessage", "payload": {...}} or, for media,
{"method": "sendVideo", "field": "video", "file": "<path under data/>", "payload": {...}}.

Run:  uvicorn tiktok_agent.worker:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

import hashlib

from . import audio, content, dialogue, gdrive, scraper, series, video
from .ai_video import AIBudgetExceeded, AIQuotaError, AIVideoSource
from .budget import LABELS, Budget, BudgetNeeded
from .config import load_settings
from .db import DB
from .manual_clips import ManualClipSource
from .publisher import TikTokPublisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("worker")

S = load_settings(os.getenv("AGENT_CONFIG") or None)
TZ = ZoneInfo(S["schedule"]["timezone"])
DATA = S.data_dir.resolve()
DB_ = DB(DATA / "agent.db")
DRIVE = (gdrive.GDrive(DB_, os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"])
         if os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET") else None)
BUDGET = Budget(DB_, S.raw.get("budget"), S["schedule"]["timezone"])
LOCK = threading.RLock()          # guards SQLite + manifests
HEAVY = threading.Lock()          # one render / paid generation at a time
BUSY: set[str] = set()            # avoids running the same Claude job twice on double taps

WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
TG_API = os.getenv("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
CHAT = S.telegram_chat_id
AI_CFG = {**(S.raw.get("ai_video") or {}), "enabled": True, "scenes": "all"}
SCENES = int(S["content"].get("scenes", 4))
CLIP_SEC = int(AI_CFG.get("max_shot_sec", 8))
PLAN_DAYS = int(S["content"].get("plan_days", 30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, status TEXT, data TEXT, feedback TEXT
);
CREATE TABLE IF NOT EXISTS plan_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, day_no INTEGER, idea TEXT,
    status TEXT, content_id INTEGER
);
"""
DB_.conn.executescript(SCHEMA)


def _claude_usage(usage) -> None:
    c = (S.raw.get("budget") or {}).get("services", {}).get("claude", {})
    pin, pout = float(c.get("price_in_per_m", 4)), float(c.get("price_out_per_m", 20))
    inp = (getattr(usage, "input_tokens", 0) or 0) \
        + 1.25 * (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
        + 0.1 * (getattr(usage, "cache_read_input_tokens", 0) or 0)
    out = getattr(usage, "output_tokens", 0) or 0
    with LOCK:
        BUDGET.record("claude", (inp * pin + out * pout) / 1e6, f"in={int(inp)} out={out}")


content.USAGE_HOOK = _claude_usage
app = FastAPI(title="TikTok agent worker")


# ====================================================================== Telegram message builders

def kb(*rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows if row]}


def msg(text: str, buttons: dict | None = None, chat: int | None = None) -> dict:
    p = {"chat_id": chat or CHAT, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = buttons
    return {"method": "sendMessage", "payload": p}


def long_msg(text: str, buttons: dict | None = None) -> list[dict]:
    """Split on line breaks so no message exceeds Telegram's 4096 chars (HTML tags never span lines here)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    out = [msg(c) for c in chunks if c.strip()]
    if buttons and out:
        out[-1]["payload"]["reply_markup"] = buttons
    return out


def media(kind: str, path: Path, caption: str, buttons: dict | None = None) -> dict:
    payload = {"chat_id": str(CHAT), "caption": caption[:1024], "parse_mode": "HTML",
               "reply_markup": json.dumps(buttons or {"inline_keyboard": []}, ensure_ascii=False)}
    if kind == "video":
        payload["supports_streaming"] = "true"
    method = {"video": "sendVideo", "photo": "sendPhoto", "audio": "sendAudio"}[kind]
    return {"method": method, "field": kind,
            "file": str(Path(path).resolve().relative_to(DATA)), "payload": payload}


def clear_buttons(message_id: int | None) -> list[dict]:
    if not message_id:
        return []
    return [{"method": "editMessageReplyMarkup",
             "payload": {"chat_id": CHAT, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}}]


def reply(*messages, next_step: dict | None = None) -> dict:
    flat: list[dict] = []
    for m in messages:
        flat.extend(m if isinstance(m, list) else [m])
    return {"messages": flat, "next": next_step}


esc = html.escape


def budget_msg(req: dict) -> dict:
    s = req["service"]
    text = (f"💰 <b>Yêu cầu duyệt ngân sách #NS{req['id']}</b>\n\n"
            f"Dịch vụ: <b>{esc(LABELS.get(s, s))}</b>\nTháng: {req['month']}\n"
            f"Đã dùng tháng này: ${BUDGET.spent(s):.2f} · hạn mức hiện tại: ${BUDGET.allowance(s):.2f}\n"
            f"Đề xuất duyệt thêm: <b>${req['amount_usd']:.2f}</b>\n\n"
            f"Lý do: {esc(req['reason'] or '')}\n\n"
            "Hệ thống sẽ không tiêu thêm đồng nào cho dịch vụ này tới khi bạn duyệt.")
    return msg(text, kb([("✅ Duyệt", f"ba:{req['id']}"), ("❌ Từ chối", f"bx:{req['id']}")]))


HELP = ("🤖 <b>Trợ lý kênh TikTok</b>\n\n"
        "/kehoach – lập kế hoạch 30 ngày mới\n"
        "/kichban – viết ngay kịch bản tiếp theo\n"
        "/clip – gửi lại prompt clip đang chờ\n"
        "/trangthai – xem tình trạng hệ thống\n"
        "/ngansach – chi phí tháng này\n"
        "/nhanvat – nhân vật cố định & prompt ảnh nhân vật\n"
        "📷 Gửi ảnh + chú thích tên (vd 'Bé Na - con gái 8 tuổi') – đặt nhân vật theo ảnh (gửi 1 lần, dùng mãi)\n"
        "/nhac – thư viện nhạc nền (gửi file mp3 để thêm)\n"
        "/phongcach – xem/sửa mô tả phong cách kênh mẫu\n"
        "/drive – kết nối Google Drive (tự lưu kịch bản đã duyệt)\n"
        "/tiktok – kết nối tài khoản TikTok\n\n"
        "Mỗi sáng bot gửi 1 kịch bản thoại giữa các nhân vật + nhạc nền. Duyệt → hệ thống tự tạo clip AI có thoại "
        "(hoặc gửi prompt để bạn tạo trên Flow) → ghép video → gửi để duyệt → duyệt xong đăng TikTok.")


# ====================================================================== state helpers

def now_ts() -> int:
    return int(time.time())


def get_plan(pid: int) -> dict | None:
    r = DB_.conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"])
    return d


def active_plan() -> dict | None:
    r = DB_.conn.execute("SELECT id FROM plans WHERE status='approved' ORDER BY id DESC LIMIT 1").fetchone()
    return get_plan(r["id"]) if r else None


def latest_plan() -> dict | None:
    r = DB_.conn.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    return get_plan(r["id"]) if r else None


def remaining_days() -> int:
    r = DB_.conn.execute("SELECT COUNT(*) n FROM plan_days d JOIN plans p ON p.id=d.plan_id "
                         "WHERE p.status='approved' AND d.status='planned'").fetchone()
    return int(r["n"])


def owner_notes(limit: int = 8) -> list[str]:
    rows = DB_.conn.execute("SELECT feedback FROM content WHERE feedback IS NOT NULL AND feedback != '' "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [r["feedback"] for r in rows]


def manual() -> ManualClipSource:
    return ManualClipSource(AI_CFG, DB_, DATA, S["schedule"]["timezone"])


def fix_bible(bible: dict) -> dict:
    """Every bible gets the channel's fixed visual style and the owner's photo characters — set once, used always."""
    b = dict(bible or {})
    style = (niche().get("visual_style_en") or "").strip()
    if style:
        b["style_en"] = " ".join(style.split())
    cast = cast_items()
    return series.apply_cast(b, cast) if cast else b


def content_bible(item: dict) -> dict:
    plan = get_plan(item["idea"]["plan_id"]) if item["idea"].get("plan_id") else None
    return fix_bible((plan or {}).get("data", {}).get("bible", {}))


def is_dialogue(script: dict | None) -> bool:
    return any("lines" in s for s in (script or {}).get("scenes", []))


def rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(DATA))


# ====================================================================== plan

def create_plan(feedback: str | None = None, previous: dict | None = None) -> dict:
    if "plan" in BUSY:
        return reply(msg("⏳ Đang lập kế hoạch, vui lòng chờ…"))
    svc = (S.raw.get("budget") or {}).get("services", {})
    with LOCK:
        BUDGET.ensure("claude", float(svc.get("claude", {}).get("est_plan_usd", 0.8)),
                      f"Lập kế hoạch {PLAN_DAYS} ngày (phân tích đối thủ + series bible).",
                      resume={"action": "plan"})
    BUSY.add("plan")
    try:
        refs = [c.lstrip("@") for c in (S.raw.get("reference_channels") or []) if c]
        competitors = list(dict.fromkeys(refs + [c.lstrip("@") for c in (S["competitors"] or []) if c]))
        notes = []
        if S.apify_token and competitors:
            est = float(svc.get("apify", {}).get("est_scrape_usd", 0.5))
            with LOCK:
                BUDGET.ensure("apify", est, "Quét video mới của các kênh đối thủ.", resume={"action": "plan"})
            try:
                vids = scraper.scrape_profiles(S.apify_token, competitors, S["scraper"]["videos_per_profile"])
                with LOCK:
                    DB_.upsert_videos(vids)
                    BUDGET.record("apify", est, f"{len(vids)} video")
                notes.append(f"Đã quét {len(vids)} video của {len(competitors)} kênh (kênh mẫu: "
                             f"{', '.join('@' + r for r in refs) or 'chưa đặt'}).")
            except httpx.HTTPError as e:
                log.warning("Apify lỗi: %s", type(e).__name__)
                notes.append("⚠️ Quét đối thủ lỗi lần này, dùng dữ liệu đã lưu.")
        else:
            notes.append("ℹ️ Chưa có APIFY_TOKEN → lập kế hoạch không có dữ liệu đối thủ mới.")
        with LOCK:
            since = now_ts() - S["scraper"]["lookback_days"] * 86400
            n = S["scraper"]["top_n_for_analysis"]
            ref_top = DB_.top_videos(since, n, authors=refs) if refs else []
            top = DB_.top_videos(since, max(5, n - len(ref_top)), exclude=refs)
            recent = DB_.recent_titles(60)
        if refs and not ref_top:
            notes.append("ℹ️ Chưa có dữ liệu video của kênh mẫu — dùng mô tả phong cách trong config.yaml.")
        cast = cast_items()
        plan = series.make_plan(niche(), top, recent, PLAN_DAYS, feedback=feedback, previous=previous,
                                reference=ref_top, cast=cast)
        data = plan.model_dump()
        data["bible"] = fix_bible(data["bible"])
        if cast:
            notes.append(f"🧑‍🎨 Dùng {len(cast)} nhân vật từ ảnh của bạn.")
        with LOCK:
            cur = DB_.conn.execute("INSERT INTO plans (ts, status, data, feedback) VALUES (?, 'review', ?, ?)",
                                   (now_ts(), json.dumps(data, ensure_ascii=False), feedback))
            pid = cur.lastrowid
            DB_.conn.execute("UPDATE plans SET status='replaced' WHERE status='review' AND id != ?", (pid,))
            for d in data["days"]:
                DB_.conn.execute("INSERT INTO plan_days (plan_id, day_no, idea, status) VALUES (?,?,?, 'planned')",
                                 (pid, d["day"], json.dumps(d, ensure_ascii=False)))
            DB_.conn.commit()
        return reply(msg("\n".join(notes)), plan_messages(pid))
    finally:
        BUSY.discard("plan")


def plan_messages(pid: int) -> list[dict]:
    p = get_plan(pid)
    d = p["data"]
    b = d["bible"]
    lines = [f"🗓 <b>Kế hoạch {len(d['days'])} ngày #KH{pid}</b>", "",
             f"🏡 <b>Gia đình xuyên suốt</b> ({esc(b['era'])}):"]
    lines += [f"• <b>{esc(c['name'])}</b> – {esc(c['role'])}" for c in b["characters"]]
    lines += ["", "📈 <b>Học từ kênh mẫu:</b>"] + [f"• {esc(x)}" for x in d["patterns"][:6]]
    lines += ["", "📅 <b>Lịch nội dung:</b>"]
    for day in d["days"]:
        lines.append(f"<b>{day['day']}.</b> {esc(day['title'])} <i>({esc(day['theme'])})</i> – {esc(day['lesson'])}")
    lines += ["", f"👉 Duyệt để bắt đầu. Muốn sửa: <b>Trả lời (Reply)</b> tin nhắn này và gõ góp ý. (#KH{pid})"]
    return long_msg("\n".join(lines), kb([("✅ Duyệt kế hoạch", f"pa:{pid}"), ("🔄 Lập lại", f"pr:{pid}")]))


def approve_plan(pid: int, message_id: int | None) -> dict:
    with LOCK:
        p = get_plan(pid)
        if not p or p["status"] != "review":
            return reply(clear_buttons(message_id), msg(f"Kế hoạch #KH{pid} không còn chờ duyệt."))
        DB_.conn.execute("UPDATE plans SET status='done' WHERE status='approved'")
        DB_.conn.execute("UPDATE plan_days SET status='skipped' WHERE status='planned' AND plan_id != ?", (pid,))
        DB_.conn.execute("UPDATE plans SET status='approved' WHERE id=?", (pid,))
        DB_.conn.commit()
    return reply(clear_buttons(message_id),
                 msg(f"✅ Đã duyệt kế hoạch #KH{pid}. Mỗi sáng (07:00) bot sẽ gửi 1 kịch bản.\n"
                     "Muốn làm ngay kịch bản đầu tiên: gõ /kichban\n\n"
                     "💡 Bước nên làm 1 lần: gõ /nhanvat để tạo ảnh nhân vật chuẩn, giúp các clip giống nhau."))


# ====================================================================== scripts

def script_messages(cid: int) -> list[dict]:
    item = DB_.get_content(cid)
    sc, idea = item["script"], item["idea"]
    lines = [f"📝 <b>Kịch bản #{cid}</b> · Ngày {idea.get('day', '?')}/{PLAN_DAYS} · <i>{esc(idea.get('theme', ''))}</i>",
             f"<b>{esc(sc['title'])}</b>", "", f"🎣 <b>Hook:</b> {esc(sc['hook'])}", ""]
    for i, s in enumerate(sc["scenes"], 1):
        lines.append(f"🎬 <b>Cảnh {i}</b> – {esc(s['visual_vi'])}")
        if "lines" in s:
            lines += [f"   💬 <b>{esc(ln['speaker'])}</b> <i>({esc(ln.get('emotion_vi', ''))})</i>: “{esc(ln['text'])}”"
                      for ln in s["lines"]] or ["   <i>(không thoại – chỉ hình và âm thanh)</i>"]
        else:
            lines.append(f"🗣 {esc(s['narration'])}")
        lines.append("")
    if sc.get("music_mood"):
        lines.append(f"🎵 <b>Không khí nhạc gợi ý:</b> {esc(sc['music_mood'])}")
    lines += [f"💡 <b>Thông điệp:</b> {esc(sc['lesson'])}", "", f"📣 <b>Caption:</b> {esc(item['caption'])}", ""]
    voice_msg = audio_message(cid)
    if voice_msg:
        lines.append(("🎧 <b>Nghe thử nhạc nền ở tin tiếp theo</b>, rồi bấm duyệt ở đó. " if is_dialogue(sc) else
                      "🎧 <b>Nghe thử giọng đọc + nhạc nền ở tin tiếp theo</b>, rồi bấm duyệt ở đó. ") +
                     "Muốn sửa thoại/nội dung: <b>Trả lời (Reply)</b> tin này và gõ góp ý.")
        return long_msg("\n".join(lines)) + [voice_msg]
    err = (sc.get("audio_error") or "")[:200]
    lines.append(("⚠️ Chưa tạo được bản nghe thử" + (f" ({esc(err)})" if err else "") + ". ") +
                 "👉 Duyệt để làm video. Muốn sửa chi tiết: <b>Trả lời (Reply)</b> tin này và gõ góp ý.")
    return long_msg("\n".join(lines), kb([("✅ Duyệt kịch bản", f"sa:{cid}")],
                                         [("🔄 Viết bản khác", f"sr:{cid}"), ("❌ Bỏ", f"sx:{cid}")]))


# ---------------------------------------------------------------- voice-over + music preview (approved with the script)

def voice_options() -> list[str]:
    v = S.raw.get("voice", {})
    opts = list(v.get("options") or [])
    if v.get("name") and v["name"] not in opts:
        opts.insert(0, v["name"])
    return opts or ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"]


VOICE_LABELS = {"vi-VN-HoaiMyNeural": "Hoài My (nữ)", "vi-VN-NamMinhNeural": "Nam Minh (nam)"}


def _narr_hash(voice: str, rate: str, narrations: list[str]) -> str:
    return hashlib.sha1(json.dumps([voice, rate, narrations], ensure_ascii=False).encode()).hexdigest()[:16]


def prepare_audio(cid: int, voice: str | None = None, music: str | None = None) -> dict:
    """(Re)build the narration + preview for a script. `music` is a path relative to DATA, or "" for none."""
    with LOCK:
        item = DB_.get_content(cid)
    sc = item["script"]
    cur = sc.get("audio") or {}
    voice = voice or cur.get("voice") or voice_options()[0]
    rate = S.raw.get("voice", {}).get("rate", "-8%")
    if music is None:
        if "music" in cur:
            music = cur["music"]
        else:  # library track → path relative to DATA; a fixed track from config may live elsewhere (absolute)
            m = pick_music(cid)
            music = (rel(Path(m)) if Path(m).resolve().is_relative_to(DATA) else str(Path(m).resolve())) if m else ""
    narrations = [s["narration"] for s in sc["scenes"]]
    h = _narr_hash(voice, rate, narrations)
    d = DATA / "audio" / str(cid)
    meta_file = d / "narration.json"
    if cur.get("hash") == h and meta_file.is_file() and (d / "narration.mp3").is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    else:
        meta = audio.narration_track(narrations, voice, rate, d)
    total = sum(audio.scene_durations(meta))
    prev = audio.preview(d / "narration.mp3", str(DATA / music) if music else None, total, S.raw,
                         d / f"preview_{now_ts()}.mp3")
    for old in d.glob("preview_*.mp3"):
        if old != prev:
            old.unlink(missing_ok=True)
    sc["audio"] = {"voice": voice, "rate": rate, "music": music, "hash": h, "preview": rel(prev),
                   "total": round(total, 2)}
    sc.pop("audio_error", None)
    with LOCK:
        DB_.update_content(cid, script=sc)
    return sc["audio"]


def prepare_music(cid: int, music: str | None = None) -> dict:
    """Dialogue scripts: the voices come from the clips, so the owner approves the dialogue text + the music."""
    with LOCK:
        item = DB_.get_content(cid)
    sc = item["script"]
    cur = sc.get("audio") or {}
    if music is None:
        if "music" in cur:
            music = cur["music"]
        else:
            m = pick_music(cid)
            music = (rel(Path(m)) if Path(m).resolve().is_relative_to(DATA) else str(Path(m).resolve())) if m else ""
    total = float(len(sc["scenes"]) * CLIP_SEC)
    d = DATA / "audio" / str(cid)
    d.mkdir(parents=True, exist_ok=True)
    prev = dialogue.music_preview(str(DATA / music) if music else None, total, S.raw, d / f"music_{now_ts()}.mp3")
    for old in d.glob("music_*.mp3"):
        if old != prev:
            old.unlink(missing_ok=True)
    sc["audio"] = {"mode": "dialogue", "music": music, "preview": rel(prev) if prev else "", "total": total}
    sc.pop("audio_error", None)
    with LOCK:
        DB_.update_content(cid, script=sc)
    return sc["audio"]


def _try_prepare_audio(cid: int) -> None:
    try:
        with LOCK:
            dlg = is_dialogue(DB_.get_content(cid)["script"])
        prepare_music(cid) if dlg else prepare_audio(cid)
    except Exception as e:  # edge-tts outage etc. — the script can still be approved; render retries TTS
        log.warning("Không tạo được bản nghe thử #%s: %s", cid, e)
        with LOCK:
            item = DB_.get_content(cid)
            sc = item["script"]
            sc["audio_error"] = str(e)[:300]
            DB_.update_content(cid, script=sc)


def audio_message(cid: int) -> dict | None:
    item = DB_.get_content(cid)
    a = (item["script"] or {}).get("audio") or {}
    if a.get("mode") == "dialogue":
        rows = [[("✅ Duyệt kịch bản & nhạc", f"sa:{cid}")],
                [("🎵 Đổi nhạc", f"am:{cid}"), ("🔄 Viết bản khác", f"sr:{cid}"), ("❌ Bỏ", f"sx:{cid}")]]
        if not a.get("preview") or not (DATA / a["preview"]).is_file():
            return msg(f"🎵 <b>#{cid} – Nhạc nền:</b> không dùng nhạc. Gửi file mp3/m4a vào đây để thêm vào thư viện "
                       "(/nhac), rồi bấm Đổi nhạc.", kb(*rows))
        cap = (f"🎵 <b>#{cid} – Nhạc nền:</b> {esc(Path(a['music']).stem)} ({a['total']:.0f}s)\n\n"
               "Trong video, nhạc sẽ nhỏ hơn và tự hạ xuống khi nhân vật nói. Giọng nhân vật do video AI tạo "
               "cùng clip (khớp khẩu hình).")
        return media("audio", DATA / a["preview"], cap, kb(*rows))
    if not a.get("preview") or not (DATA / a["preview"]).is_file():
        return None
    music = Path(a["music"]).stem if a.get("music") else "không nhạc"
    cap = (f"🎧 <b>#{cid} – Nghe thử</b> ({a['total']:.0f}s)\n🎙 Giọng: {esc(VOICE_LABELS.get(a['voice'], a['voice']))}"
           f"\n🎵 Nhạc: {esc(music)}\n\nĐây chính là âm thanh của video thành phẩm.")
    buttons = kb([("✅ Duyệt kịch bản & âm thanh", f"sa:{cid}")],
                 [("🎵 Đổi nhạc", f"am:{cid}"), ("🎙 Đổi giọng", f"av:{cid}")],
                 [("🔄 Viết bản khác", f"sr:{cid}"), ("❌ Bỏ", f"sx:{cid}")])
    return media("audio", DATA / a["preview"], cap, buttons)


def change_audio(cid: int, what: str) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
    if not item or item["status"] != "script_review":
        return reply(msg(f"Kịch bản #{cid} không còn ở bước duyệt."))
    a = item["script"].get("audio") or {}
    if is_dialogue(item["script"]):
        opts = [rel(t) for t in music_tracks()] + [""]
        if len(opts) == 1:
            return reply(msg("🎵 Thư viện nhạc đang trống. Gửi file mp3/m4a vào đây để thêm nhạc, rồi bấm Đổi nhạc."))
        cur = a.get("music", "")
        prepare_music(cid, opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0])
        return reply(audio_message(cid))
    if what == "music":
        opts = [rel(t) for t in music_tracks()] + [""]
        if len(opts) == 1:
            return reply(msg("🎵 Thư viện nhạc đang trống. Gửi file mp3/m4a vào đây để thêm nhạc, rồi bấm Đổi nhạc."))
        cur = a.get("music", "")
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
        prepare_audio(cid, music=nxt)
    else:
        opts = voice_options()
        cur = a.get("voice") or opts[0]
        prepare_audio(cid, voice=opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0])
    return reply(audio_message(cid) or msg("⚠️ Không tạo được bản nghe thử, thử lại sau."))


def _write(idea: dict, bible: dict, patterns: list[str], feedback=None, previous=None) -> tuple[dict, str]:
    script = series.write_script(niche(), bible, idea, patterns, SCENES, CLIP_SEC, owner_notes(),
                                 feedback=feedback, previous=previous).model_dump()
    script["scenes"] = script["scenes"][:SCENES]
    caption = script["caption"] + "\n\n" + " ".join(f"#{h.lstrip('#')}" for h in script["hashtags"])
    return script, caption


def next_script(scheduled: bool = False) -> dict:
    with LOCK:
        plan = active_plan()
        if not plan:
            lp = latest_plan()
            if lp and lp["status"] == "review":
                return reply(msg(f"⏳ Kế hoạch #KH{lp['id']} đang chờ bạn duyệt, chưa viết kịch bản."))
            if scheduled:
                return reply()  # the plan job will create one
            return reply(msg("Chưa có kế hoạch. Gõ /kehoach để lập kế hoạch 30 ngày."))
        if scheduled:
            today = dt.datetime.now(TZ).date().isoformat()
            if (DB_.kv_get("last_daily") or {}).get("date") == today:
                return reply()
            DB_.kv_set("last_daily", {"date": today})
        row = DB_.conn.execute("SELECT * FROM plan_days WHERE plan_id=? AND status='planned' ORDER BY day_no LIMIT 1",
                               (plan["id"],)).fetchone()
        if not row:
            return reply(msg("🎉 Đã dùng hết kế hoạch. Gõ /kehoach để lập kế hoạch mới."))
        svc = (S.raw.get("budget") or {}).get("services", {})
        BUDGET.ensure("claude", float(svc.get("claude", {}).get("est_script_usd", 0.15)),
                      "Viết kịch bản hằng ngày.", resume={"action": "script"})
        key = f"day{row['id']}"
        if key in BUSY:
            return reply(msg("⏳ Đang viết kịch bản…"))
        BUSY.add(key)
    try:
        idea = {**json.loads(row["idea"]), "plan_id": plan["id"], "plan_day_id": row["id"]}
        script, caption = _write(idea, fix_bible(plan["data"]["bible"]), plan["data"]["patterns"])
        with LOCK:
            cid = DB_.create_content(idea)
            DB_.update_content(cid, script=script, caption=caption, status="script_review")
            DB_.conn.execute("UPDATE plan_days SET status='scripted', content_id=? WHERE id=?", (cid, row["id"]))
            DB_.conn.commit()
        _try_prepare_audio(cid)
        return reply(script_messages(cid))
    finally:
        BUSY.discard(key)


def rewrite_script(cid: int, feedback: str | None, message_id: int | None = None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] not in ("script_review",):
            return reply(clear_buttons(message_id), msg(f"Kịch bản #{cid} không còn ở bước duyệt."))
        svc = (S.raw.get("budget") or {}).get("services", {})
        BUDGET.ensure("claude", float(svc.get("claude", {}).get("est_script_usd", 0.15)),
                      "Viết lại kịch bản theo góp ý.", resume=None)
        if f"c{cid}" in BUSY:
            return reply(msg("⏳ Đang viết lại…"))
        BUSY.add(f"c{cid}")
    try:
        plan = get_plan(item["idea"]["plan_id"])
        script, caption = _write(item["idea"], fix_bible(plan["data"]["bible"]), plan["data"]["patterns"],
                                 feedback=feedback, previous=item["script"])
        with LOCK:
            old_audio = (item["script"] or {}).get("audio") or {}
            script["audio"] = {k: old_audio[k] for k in ("voice", "music") if k in old_audio}  # keep owner's picks
            DB_.update_content(cid, script=script, caption=caption, feedback=feedback or item["feedback"])
        _try_prepare_audio(cid)
        return reply(clear_buttons(message_id), msg("✍️ Bản mới:"), script_messages(cid))
    finally:
        BUSY.discard(f"c{cid}")


# ====================================================================== clips

def prompt_messages(cid: int) -> list[dict]:
    m = manual().load(cid)
    n = len(m["shots"])
    out = [msg(f"🎥 <b>#{cid} – Tạo {n} clip miễn phí</b>\n\n"
               "1) Mở <b>labs.google/flow</b> trên Safari (tài khoản Google miễn phí).\n"
               "2) Chạm vào khung prompt của từng cảnh bên dưới để <b>sao chép</b> → dán vào Flow → chọn "
               "khung <b>dọc 9:16</b>, 8 giây → tạo → tải clip về.\n"
               "3) Quay lại Telegram, <b>Trả lời (Reply)</b> đúng tin nhắn \"Cảnh N\" và gửi clip đó "
               "(<b>giữ nguyên âm thanh</b> — Flow tạo luôn giọng thoại của nhân vật). "
               f"Hoặc gửi cả {n} clip một lượt theo đúng thứ tự cảnh.\n\n"
               "💡 Để nhân vật giống nhau: trong Flow thêm ảnh nhân vật (/nhanvat) vào phần <i>Ingredients</i>; "
               "sau mỗi clip bot gửi khung hình cuối để bạn làm khung bắt đầu cho cảnh sau.")]
    for i, s in enumerate(m["shots"]):
        scene = DB_.get_content(cid)["script"]["scenes"][i]
        out.append(msg(f"🎬 <b>#{cid} · Cảnh {i + 1}/{n}</b> – {esc(scene['visual_vi'])}\n\n<code>{esc(s['prompt'])}</code>"))
    if os.getenv("GEMINI_API_KEY"):
        usd = float((S.raw.get("budget") or {}).get("services", {}).get("gemini_video", {}).get("usd_per_clip", 0.19))
        out.append(msg("Không muốn tạo tay?",
                       kb([(f"🤖 Tạo tự động bằng Gemini (≈ ${usd * n:.2f})", f"pg:{cid}")])))
    return out


def approve_script(cid: int, message_id: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "script_review":
            return reply(clear_buttons(message_id), msg(f"Kịch bản #{cid} không còn chờ duyệt."))
        bible = content_bible(item)
        scenes = item["script"]["scenes"]
        shots = [{"scene": i + 1, "shot": 1, "seconds": CLIP_SEC,
                  "prompt": series.clip_prompt(bible, sc, i, len(scenes)),
                  "file": None, "tg_message_id": None} for i, sc in enumerate(scenes)]
        manual().save(cid, {"content_id": cid, "day": dt.datetime.now(TZ).date().isoformat(),
                            "shots": shots, "unassigned": []})
        sc = item["script"]
        sc["approved_at"] = now_ts()
        DB_.update_content(cid, status="awaiting_clips", script=sc)
    saved = sync_drive()
    if AI_CFG.get("auto_generate"):
        if os.getenv("GEMINI_API_KEY"):
            return reply(clear_buttons(message_id),
                         msg(f"✅ Đã duyệt kịch bản #{cid}.{saved}\n🤖 Hệ thống tự tạo {len(scenes)} clip AI có thoại…"),
                         next_step={"action": "paid", "id": cid})
        return reply(clear_buttons(message_id), msg(
            f"✅ Đã duyệt kịch bản #{cid}.{saved}\nℹ️ Chưa có GEMINI_API_KEY nên chưa tạo clip tự động được — tạo tay "
            "miễn phí trên Google Flow theo hướng dẫn dưới. (Muốn tự động: thêm GEMINI_API_KEY vào .env.)"),
            prompt_messages(cid))
    return reply(clear_buttons(message_id), msg(f"✅ Đã duyệt kịch bản #{cid}.{saved}"), prompt_messages(cid))


# ---------------------------------------------------------------- Google Drive: one doc with every approved script

APPROVED = ("awaiting_clips", "rendering", "video_review", "publishing", "posted", "failed", "video_rejected")


def approved_doc_html() -> str:
    with LOCK:
        rows = DB_.conn.execute(f"SELECT id FROM content WHERE status IN ({','.join('?' * len(APPROVED))}) "
                                "ORDER BY id DESC", APPROVED).fetchall()
        items = [DB_.get_content(r["id"]) for r in rows]
    parts = [f"<h1>{esc(gdrive.DOC_NAME)}</h1>",
             f"<p><i>Tự động cập nhật mỗi khi duyệt kịch bản. Tổng cộng {len(items)} kịch bản, mới nhất ở trên.</i></p>"]
    for it in items:
        sc, idea = it["script"] or {}, it["idea"] or {}
        when = dt.datetime.fromtimestamp(sc.get("approved_at") or it["created_at"], TZ).strftime("%d/%m/%Y %H:%M")
        parts.append(f"<h2>#{it['id']} – {esc(sc.get('title', ''))}</h2>")
        parts.append(f"<p><b>Duyệt:</b> {when} · <b>Ngày</b> {esc(str(idea.get('day', '?')))} · "
                     f"<b>Chủ đề:</b> {esc(idea.get('theme', ''))}</p>")
        if sc.get("hook"):
            parts.append(f"<p><b>Mở đầu:</b> {esc(sc['hook'])}</p>")
        for i, s in enumerate(sc.get("scenes", []), 1):
            parts.append(f"<h3>Cảnh {i}: {esc(s.get('visual_vi', ''))}</h3>")
            if "lines" in s:
                lines = [f"<li><b>{esc(ln['speaker'])}</b> <i>({esc(ln.get('emotion_vi', ''))})</i>: "
                         f"“{esc(ln['text'])}”</li>" for ln in s["lines"]]
                parts.append("<ul>" + ("".join(lines) or "<li><i>(không thoại)</i></li>") + "</ul>")
            elif s.get("narration"):
                parts.append(f"<p>🗣 {esc(s['narration'])}</p>")
        a = sc.get("audio") or {}
        music = Path(a["music"]).stem if a.get("music") else ("không nhạc" if "music" in a else "")
        if music or sc.get("music_mood"):
            parts.append(f"<p><b>Nhạc nền:</b> {esc(music or '—')}"
                         + (f" ({esc(sc['music_mood'])})" if sc.get("music_mood") else "") + "</p>")
        if sc.get("lesson"):
            parts.append(f"<p><b>Thông điệp:</b> {esc(sc['lesson'])}</p>")
        parts.append(f"<p><b>Caption:</b> {esc(it.get('caption') or '')}</p><hr>")
    return "<html><head><meta charset='utf-8'></head><body>" + "".join(parts) + "</body></html>"


def sync_drive() -> str:
    """Upload the approved-scripts doc; returns a short note for the Telegram message (never raises)."""
    if DRIVE is None:
        return ""
    with LOCK:
        connected = DRIVE.connected()
    if not connected:
        return "\n📄 Chưa lưu Google Drive — gõ /drive để kết nối."
    try:
        link = DRIVE.save_doc(approved_doc_html())
        return f"\n📄 Đã lưu vào Google Drive: {esc(link)}" if link else "\n📄 Đã lưu vào Google Drive."
    except Exception as e:
        log.warning("Lưu Google Drive lỗi: %s", e)
        return (f"\n⚠️ Lưu Google Drive lỗi: {esc(str(e)[:200])}. Lần duyệt sau sẽ tự lưu lại toàn bộ "
                "(hoặc /drive → Đồng bộ lại).")


def drive_cmd() -> dict:
    if DRIVE is None:
        return reply(msg("📄 <b>Lưu kịch bản lên Google Drive chưa được bật.</b>\n\nCần điền <code>GOOGLE_CLIENT_ID</code> "
                         "và <code>GOOGLE_CLIENT_SECRET</code> (OAuth client loại <i>TVs and Limited Input devices</i>) "
                         "vào .env rồi chạy lại lệnh cài. Xem hướng dẫn trong TIEN_DO.md / HUONG_DAN.md."))
    if DRIVE.connected():
        link = DRIVE.doc_link()
        return reply(msg("📄 <b>Google Drive: đã kết nối.</b>\n" + (f"File kịch bản: {esc(link)}" if link else
                         "Chưa có file — sẽ tạo ở lần duyệt kịch bản tới (hoặc bấm Đồng bộ ngay)."),
                         kb([("📤 Đồng bộ ngay", "ds:0")], [("🔌 Ngắt kết nối", "dx:0")])))
    d = DRIVE.start_device()
    return reply(msg(f"📄 <b>Kết nối Google Drive</b>\n\n1) Mở {esc(d['url'])} trên Safari\n"
                     f"2) Nhập mã: <code>{esc(d['code'])}</code>\n3) Chọn đúng tài khoản Google → Cho phép\n"
                     f"4) Quay lại đây bấm nút dưới.\n\nMã có hiệu lực {d['minutes']} phút. Ứng dụng chỉ thấy được "
                     "thư mục và file do nó tạo ra, không đọc được các file khác trên Drive của bạn.",
                     kb([("✅ Tôi đã cho phép", "dc:0")])))


def drive_check(mid: int | None) -> dict:
    if DRIVE is None:
        return reply(clear_buttons(mid))
    st = DRIVE.poll_device()
    if st == "ok":
        return reply(clear_buttons(mid), msg("✅ Đã kết nối Google Drive. Từ giờ mỗi kịch bản được duyệt sẽ tự lưu "
                                             "vào file trên Drive." + sync_drive()))
    if st == "pending":
        return reply(msg("⏳ Google chưa xác nhận. Nhập mã trên google.com/device, bấm Cho phép, rồi bấm lại nút.",
                         kb([("✅ Tôi đã cho phép", "dc:0")])))
    return reply(clear_buttons(mid), msg("❌ Mã đã hết hạn hoặc bị từ chối. Gõ /drive để lấy mã mới."))


def _tg_download(file_id: str, dst: Path) -> None:
    token = S.telegram_bot_token
    r = httpx.get(f"{TG_API}/bot{token}/getFile", params={"file_id": file_id}, timeout=60)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram getFile lỗi: {body.get('description', '')} "
                           "(bot chỉ tải được file ≤ 20MB — hãy gửi clip nhẹ hơn)")
    with httpx.stream("GET", f"{TG_API}/file/bot{token}/{body['result']['file_path']}", timeout=300) as resp:
        resp.raise_for_status()
        with dst.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)


# ---------------------------------------------------------------- channel style (editable from Telegram)

def niche() -> dict:
    """Niche from config.yaml, with the style description overridden by /phongcach if the owner set one."""
    n = dict(S["niche"])
    with LOCK:
        custom = DB_.kv_get("style_reference")
    if custom and custom.get("text"):
        n["style_reference"] = custom["text"]
    return n


def style_cmd(text: str) -> dict:
    new = text.partition(" ")[2].strip()
    if new:
        if len(new) < 20:
            return reply(msg("Mô tả phong cách quá ngắn. Hãy viết cụ thể hình ảnh, giọng đọc, nhạc, chữ, độ dài…"))
        with LOCK:
            DB_.kv_set("style_reference", {"text": new[:3000], "ts": now_ts()})
        return reply(msg("✅ Đã lưu phong cách mới. Áp dụng cho kịch bản tiếp theo; gõ /kehoach để lập lại "
                         "kế hoạch theo phong cách này."))
    cur = niche().get("style_reference") or "(chưa có)"
    return reply(msg(f"🎨 <b>Phong cách kênh đang dùng:</b>\n\n{esc(cur)}\n\n"
                     "Muốn sửa: gõ <code>/phongcach</code> + mô tả mới trong cùng một tin nhắn, ví dụ:\n"
                     "<code>/phongcach Clip AI người thật, nắng chiều vàng, giọng nữ trầm ấm, nhạc piano nhẹ, "
                     "câu mở đầu 'Ai còn nhớ…', 20 giây</code>"))


# ---------------------------------------------------------------- characters from the owner's reference photos

CAST_KEY = "character_refs"
IMG_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def cast_items() -> list[dict]:
    with LOCK:
        return (DB_.kv_get(CAST_KEY) or {}).get("items", [])


def _save_cast(items: list[dict]) -> None:
    with LOCK:
        DB_.kv_set(CAST_KEY, {"items": items})


def receive_photo(body: dict) -> dict:
    if int(body.get("file_size") or 0) > 20 * 1024 * 1024:
        return reply(msg("⚠️ Ảnh lớn hơn 20 MB, bot không tải được. Hãy gửi ảnh nhẹ hơn."))
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(str(body.get("mime_type") or ""), ".jpg")
    d = DATA / "characters"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"nv_{now_ts()}_{secrets.token_hex(3)}{ext}"
    _tg_download(str(body["file_id"]), dst)
    caption = str(body.get("caption") or "").strip()[:300]
    return reply(msg("⏳ Đang đọc ảnh nhân vật (khoảng 30 giây)…"),
                 next_step={"action": "photo", "path": rel(dst), "caption": caption})


def describe_photo(path: str, caption: str) -> dict:
    p = (DATA / str(path)).resolve()
    if not p.is_relative_to(DATA) or not p.is_file():
        return reply(msg("⚠️ Không tìm thấy ảnh vừa gửi, hãy gửi lại."))
    est = float(((S.raw.get("budget") or {}).get("services", {}).get("claude", {})).get("est_image_usd", 0.05))
    with LOCK:
        BUDGET.ensure("claude", est, "Đọc ảnh nhân vật để viết mô tả cố định.",
                      resume={"action": "photo", "path": path, "caption": caption})
        plan = active_plan() or latest_plan()
    era = (plan or {}).get("data", {}).get("bible", {}).get("era", "") if plan else ""
    ch = series.describe_character(p.read_bytes(), IMG_TYPES.get(p.suffix.lower(), "image/jpeg"), caption, era)
    items = cast_items()
    same = [x for x in items if x["name"].strip().lower() == ch.name.strip().lower()]
    items = [x for x in items if x not in same]
    cid = max([x["id"] for x in cast_items()] + [0]) + 1
    items.append({"id": cid, **ch.model_dump(), "image": rel(p), "ts": now_ts()})
    _save_cast(items)
    text = (f"🧑‍🎨 <b>Nhân vật từ ảnh: {esc(ch.name)}</b> – {esc(ch.role)}"
            + (" <i>(thay cho ảnh cũ cùng tên)</i>" if same else "") + "\n\n"
            f"<i>{esc(ch.look_en)}</i>\n\nPhong cách ảnh: {esc(ch.style_en)}\n\n"
            "✅ Đã lưu và <b>tự áp dụng cho mọi kịch bản từ giờ</b> (cả kế hoạch đang chạy và các kế hoạch sau) — "
            "chỉ cần gửi ảnh 1 lần. Hình ảnh luôn theo phong cách cố định của kênh (anime điện ảnh).\n"
            "Muốn đổi tên/vai: gửi lại ảnh kèm chú thích, vd <code>Bé Na - con gái út 8 tuổi</code>.")
    rows = [[("🗑 Xoá nhân vật này", f"cx:{cid}")]]
    return reply(msg(text, kb(*rows)))


def apply_cast_to_plan(cid: int, mid: int | None) -> dict:
    item = next((x for x in cast_items() if x["id"] == cid), None)
    with LOCK:
        plan = active_plan() or latest_plan()
        if not item or not plan:
            return reply(clear_buttons(mid), msg("Không áp dụng được (nhân vật đã xoá hoặc chưa có kế hoạch)."))
        data = plan["data"]
        data["bible"] = series.apply_cast(data["bible"], [item])
        DB_.conn.execute("UPDATE plans SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), plan["id"]))
        DB_.conn.commit()
    return reply(clear_buttons(mid), msg(f"✅ Đã đưa <b>{esc(item['name'])}</b> vào kế hoạch #KH{plan['id']}. "
                                         "Áp dụng cho các kịch bản viết từ bây giờ (/nhanvat để xem)."))


def delete_cast(cid: int, mid: int | None) -> dict:
    items = cast_items()
    item = next((x for x in items if x["id"] == cid), None)
    if not item:
        return reply(clear_buttons(mid))
    _save_cast([x for x in items if x["id"] != cid])
    return reply(clear_buttons(mid), msg(f"🗑 Đã xoá nhân vật {esc(item['name'])} khỏi danh sách ảnh mẫu. "
                                         "Kế hoạch đang chạy giữ nguyên; kế hoạch mới sẽ không dùng nhân vật này."))


# ---------------------------------------------------------------- background music library

MUSIC_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".wav")


def music_dir() -> Path:
    d = DATA / str(S.raw.get("video", {}).get("music_dir") or "music")
    d.mkdir(parents=True, exist_ok=True)
    return d


def music_tracks() -> list[Path]:
    return sorted(p for p in music_dir().iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXT)


def pick_music(cid: int) -> str:
    """Fixed track from config if set, else rotate through the library (one track per video)."""
    fixed = str(S.raw.get("video", {}).get("background_music") or "")
    if fixed and Path(fixed).exists():
        return fixed
    tracks = music_tracks()
    return str(tracks[cid % len(tracks)]) if tracks else ""


def _safe_name(name: str, ext: str) -> str:
    stem = Path(name).stem.replace("đ", "d").replace("Đ", "D")
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or f"nhac_{now_ts()}"
    return stem + ext


def receive_audio(body: dict) -> dict:
    size = int(body.get("file_size") or 0)
    if size > 20 * 1024 * 1024:
        return reply(msg("⚠️ File nhạc lớn hơn 20 MB, bot không tải được. Hãy gửi bản mp3 nhẹ hơn."))
    name = str(body.get("file_name") or "")
    ext = Path(name).suffix.lower()
    if ext not in MUSIC_EXT:
        ext = {"audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg",
               "audio/wav": ".wav", "audio/x-wav": ".wav"}.get(str(body.get("mime_type") or ""), ".mp3")
    dst = music_dir() / _safe_name(name or str(body.get("title") or ""), ext)
    _tg_download(str(body["file_id"]), dst)
    try:
        ok = video._duration(dst) > 0
    except Exception:
        ok = False
    if not ok:
        dst.unlink(missing_ok=True)
        return reply(msg("⚠️ Không đọc được file nhạc này. Hãy gửi file mp3 hoặc m4a."))
    return reply(msg(f"🎵 Đã thêm nhạc nền <b>{esc(dst.name)}</b>."), music_list())


def music_list() -> dict:
    tracks = music_tracks()
    if not tracks:
        return msg("🎵 <b>Thư viện nhạc nền đang trống.</b>\n\nGửi file nhạc (mp3/m4a, dưới 20 MB) vào đây để thêm. "
                   "Chỉ dùng nhạc <b>không bản quyền</b> (YouTube Audio Library, Pixabay Music…). "
                   "Mỗi video sẽ tự xoay vòng một bài, nhạc tự nhỏ lại khi có giọng đọc.")
    lines = ["🎵 <b>Thư viện nhạc nền</b> (mỗi video xoay vòng 1 bài):"]
    lines += [f"{i + 1}. {esc(t.name)}" for i, t in enumerate(tracks)]
    lines += ["", "Gửi thêm file nhạc để bổ sung. Bấm nút để xoá bài không dùng."]
    rows = [[(f"🗑 Xoá {i + 1}. {t.stem[:24]}", f"mx:{i}")] for i, t in enumerate(tracks[:20])]
    return msg("\n".join(lines), kb(*rows))


def delete_music(i: int, mid: int | None) -> dict:
    tracks = music_tracks()
    if not 0 <= i < len(tracks):
        return reply(clear_buttons(mid), music_list())
    name = tracks[i].name
    tracks[i].unlink(missing_ok=True)
    return reply(clear_buttons(mid), msg(f"🗑 Đã xoá {esc(name)}."), music_list())


def last_frame(src: Path, dst: Path) -> Path | None:
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.15", "-i", str(src), "-frames:v", "1",
                        "-q:v", "3", str(dst)], capture_output=True)
    return dst if p.returncode == 0 and dst.exists() else None


def receive_clip(body: dict) -> dict:
    ref = body.get("reply_to_text") or ""
    m_id = re.search(r"#(\d+)", ref)
    with LOCK:
        if m_id:
            cid = int(m_id.group(1))
        else:
            r = DB_.conn.execute("SELECT id FROM content WHERE status='awaiting_clips' ORDER BY id DESC LIMIT 1").fetchone()
            if not r:
                return reply(msg("Mình chưa có video nào đang chờ clip. Hãy duyệt một kịch bản trước nhé."))
            cid = r["id"]
        item = DB_.get_content(cid)
        if not item or item["status"] != "awaiting_clips":
            return reply(msg(f"Video #{cid} không ở bước chờ clip."))
    src = manual()
    folder = src.manifest_path(cid).parent
    dst = folder / f"in_{body.get('message_id', now_ts())}.mp4"
    _tg_download(body["file_id"], dst)

    scene = None
    m_sc = re.search(r"Cảnh (\d+)", ref) or re.match(r"\s*(?:cảnh|canh|c)?\s*(\d)\b", body.get("caption") or "", re.I)
    if m_sc:
        scene = int(m_sc.group(1))
    with LOCK:
        m = src.load(cid)
        n = len(m["shots"])
        if scene and 1 <= scene <= n:
            m["shots"][scene - 1]["file"] = str(dst)
            m["shots"][scene - 1]["tg_message_id"] = body.get("message_id")
        else:
            scene = None
            m.setdefault("unassigned", []).append({"message_id": body.get("message_id") or 0, "file": str(dst)})
        empty = [s for s in m["shots"] if not s["file"]]
        complete = len(m.get("unassigned", [])) >= len(empty)
        if complete:  # fill empty scenes with the un-labelled clips in the order they were sent
            for s, u in zip(empty, sorted(m["unassigned"], key=lambda u: u["message_id"])):
                s["file"], s["tg_message_id"] = u["file"], u["message_id"]
            m["unassigned"] = []
            DB_.update_content(cid, status="rendering")
        src.save(cid, m)
        got = sum(1 for s in m["shots"] if s["file"]) + len(m.get("unassigned", []))
    if complete:
        return reply(msg(f"📥 Đã nhận đủ {n}/{n} clip cho #{cid}. ⏳ Đang ghép video (1–3 phút)…"),
                     next_step={"action": "render", "id": cid})
    out = [msg(f"📥 #{cid}: đã nhận {'cảnh ' + str(scene) if scene else 'clip'} ({got}/{n}).")]
    if scene and scene < n:
        frame = last_frame(dst, folder / f"last_{scene}.jpg")
        if frame:
            out.append(media("photo", frame, f"🖼 Khung cuối cảnh {scene} → dùng làm <b>khung bắt đầu</b> "
                                             f"(Frames to Video) cho cảnh {scene + 1} để nối liền mạch."))
    return reply(out)


def reset_clips(cid: int, message_id: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        m = manual().load(cid)
        if not item or not m or item["status"] not in ("video_review", "failed", "awaiting_clips"):
            return reply(clear_buttons(message_id), msg(f"Không thay clip được cho #{cid} lúc này."))
        for s in m["shots"]:
            s["file"], s["tg_message_id"] = None, None
        m["unassigned"] = []
        manual().save(cid, m)
        DB_.update_content(cid, status="awaiting_clips")
    return reply(clear_buttons(message_id), msg(f"🔁 Gửi lại clip cho #{cid}:"), prompt_messages(cid))


# ====================================================================== render / generate

def _render_script(item: dict, prompts: list[str] | None = None) -> dict:
    sc = item["script"]
    if is_dialogue(sc):
        return {"title": sc["title"], "visual_bible": {},
                "scenes": [{"lines": s.get("lines") or [], "visual_query": s["visual_query"], "keep_audio": True,
                            **({"prompt_full": prompts[i]} if prompts else {})} for i, s in enumerate(sc["scenes"])]}
    scenes = [{"narration": s["narration"], "visual_query": s["visual_query"],
               "shots": [prompts[i]] if prompts else [s["action_en"]]} for i, s in enumerate(sc["scenes"])]
    return {"title": sc["title"], "scenes": scenes, "visual_bible": {},
            "voice": (sc.get("audio") or {}).get("voice")}


def approved_narration(item: dict) -> dict | None:
    """Narration meta of the approved preview, if it still matches the script text."""
    a = (item["script"] or {}).get("audio") or {}
    d = DATA / "audio" / str(item["id"])
    f = d / "narration.json"
    if not a.get("hash") or not f.is_file() or not (d / "narration.mp3").is_file():
        return None
    narrs = [s["narration"] for s in item["script"]["scenes"]]
    if _narr_hash(a.get("voice", ""), a.get("rate", ""), narrs) != a["hash"]:
        return None
    meta = json.loads(f.read_text(encoding="utf-8"))
    meta["path"] = str(d / "narration.mp3")
    return meta


def approved_music(item: dict) -> str:
    a = (item["script"] or {}).get("audio") or {}
    if "music" in a:
        return str(DATA / a["music"]) if a["music"] and (DATA / a["music"]).is_file() else ""
    return pick_music(item["id"])


def do_render(cid: int, paid: bool = False) -> dict:
    if not HEAVY.acquire(timeout=1800):
        return reply(msg("⏳ Máy đang bận ghép video khác, thử lại sau ít phút."))
    try:
        with LOCK:
            item = DB_.get_content(cid)
            m = manual().load(cid)
        workdir = DATA / "videos" / f"{cid}_{now_ts()}"
        if paid:
            src = AIVideoSource({**AI_CFG}, DB_, DATA, S["schedule"]["timezone"])
            script = _render_script(item, [s["prompt"] for s in m["shots"]])
        else:
            src = manual()
            script = _render_script(item)
        src.start_run(cid)
        run: dict = {}
        try:
            cfg = {**S.raw, "video": {**S.raw.get("video", {}), "background_music": approved_music(item)}}
            if is_dialogue(item["script"]):
                src.strict = paid  # paid clips must not silently turn into stock footage
                path = dialogue.render_dialogue(script, workdir, cfg, S.pixabay_api_key or "", src,
                                                content_bible(item))
            else:
                path = video.render(script, workdir, cfg, S.pixabay_api_key or "", ai=src,
                                    narration=approved_narration(item))
        except (AIQuotaError, AIBudgetExceeded) as e:
            run = src.finish_run() or {}
            with LOCK:
                BUDGET.record("gemini_video", float(run.get("est_cost_usd", 0)), f"#{cid} (dừng giữa chừng)")
                DB_.update_content(cid, status="awaiting_clips", error=str(e)[:2000])
            return reply(msg(
                f"⚠️ <b>Tạo clip tự động cho #{cid} bị dừng: hết hạn mức / hết credit.</b>\n"
                f"<i>{esc(str(e)[:300])}</i>\n\nBạn chọn:\n"
                "• Nâng hạn mức hoặc đăng ký gói trả phí của Gemini API (Google AI Studio → Billing), rồi bấm "
                "<b>Thử tạo tự động lại</b>.\n• Hoặc tạo tay miễn phí trên Google Flow với các prompt bên dưới.",
                kb([("🔁 Thử tạo tự động lại", f"pg:{cid}")])), prompt_messages(cid))
        finally:
            run = run or src.finish_run() or {}
        with LOCK:
            if paid:
                BUDGET.record("gemini_video", float(run.get("est_cost_usd", 0)), f"#{cid}")
            DB_.update_content(cid, video_path=str(path), status="video_review", error=None)
        fallbacks = run.get("fallbacks") or []
        dur = video._duration(path)
        cap = (f"🎞 <b>Video #{cid}</b> · {esc(item['script']['title'])} · {dur:.0f}s\n\n{esc(item['caption'])}")
        out = [media("video", path, cap, kb([("✅ Duyệt & đăng TikTok", f"va:{cid}")],
                                            [("🔁 Thay clip", f"vc:{cid}"), ("❌ Bỏ", f"vx:{cid}")]))]
        if fallbacks:
            out.append(msg("⚠️ Một số cảnh thiếu clip AI nên dùng video stock: "
                           + ", ".join(str(f["scene"]) for f in fallbacks)))
        return reply(out)
    except Exception as e:
        log.exception("render #%s failed", cid)
        with LOCK:
            DB_.update_content(cid, status="failed", error=str(e)[:2000])
        return reply(msg(f"⚠️ Ghép video #{cid} lỗi: {esc(str(e)[:400])}",
                         kb([("🔁 Thử lại", f"rr:{cid}"), ("🔁 Gửi clip khác", f"vc:{cid}")])))
    finally:
        HEAVY.release()


def request_paid(cid: int, message_id: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "awaiting_clips":
            return reply(clear_buttons(message_id), msg(f"#{cid} không ở bước chờ clip."))
        usd = float((S.raw.get("budget") or {}).get("services", {}).get("gemini_video", {}).get("usd_per_clip", 0.19))
        n = len(item["script"]["scenes"])
        BUDGET.ensure("gemini_video", usd * n, f"Tạo tự động {n} clip AI cho video #{cid} (≈ ${usd:.2f}/clip).",
                      resume={"action": "paid", "id": cid})
        DB_.update_content(cid, status="rendering")
    return reply(clear_buttons(message_id), msg(f"🤖 Đang tạo {n} clip AI cho #{cid} (có thể 5–15 phút)…"),
                 next_step={"action": "generate", "id": cid})


# ====================================================================== publish

def publishers() -> list[TikTokPublisher]:
    if not (S.tiktok_client_key and S.tiktok_client_secret):
        return []
    accounts = (S["tiktok"].get("accounts") or ["default"])
    return [TikTokPublisher(S.tiktok_client_key, S.tiktok_client_secret, S.tiktok_redirect_uri, DB_, a)
            for a in accounts]


def publish(cid: int, message_id: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "video_review":
            return reply(clear_buttons(message_id), msg(f"Video #{cid} không còn chờ duyệt."))
        DB_.update_content(cid, status="publishing")
    pubs = [p for p in publishers() if p.connected()]
    if not pubs:
        with LOCK:
            DB_.update_content(cid, status="video_review")
        return reply(msg("⚠️ Chưa kết nối tài khoản TikTok nào. Gõ /tiktok để kết nối, rồi bấm Duyệt lại.",
                         kb([("✅ Duyệt & đăng lại", f"va:{cid}")])))
    tk = S["tiktok"]
    results, ok = [], 0
    for p in pubs:
        try:
            if tk["post_mode"] == "direct":
                pid = p.direct_post(Path(item["video_path"]), item["caption"], tk["privacy_level"])
            else:
                pid = p.upload_to_inbox(Path(item["video_path"]))
            ok += 1
            results.append(f"✅ {esc(p.account)}: đã gửi (publish_id {esc(pid)})")
        except Exception as e:
            log.exception("publish %s", p.account)
            results.append(f"❌ {esc(p.account)}: {esc(str(e)[:300])}")
    with LOCK:
        DB_.update_content(cid, status="posted" if ok else "video_review")
    note = ("\n\n📲 Chế độ hộp nháp: mở app TikTok → Hộp thư → hoàn tất đăng. Caption để dán:\n"
            f"<code>{esc(item['caption'])}</code>") if tk["post_mode"] != "direct" else ""
    buttons = None if ok else kb([("🔁 Thử đăng lại", f"va:{cid}")])
    return reply(clear_buttons(message_id), msg(f"🚀 <b>Đăng video #{cid}</b>\n" + "\n".join(results) + note, buttons))


# ====================================================================== commands

def status_text() -> str:
    with LOCK:
        lp = latest_plan()
        rows = DB_.conn.execute("SELECT status, COUNT(*) n FROM content GROUP BY status").fetchall()
        waiting = DB_.conn.execute("SELECT id, status FROM content WHERE status IN "
                                   "('script_review','awaiting_clips','rendering','video_review') ORDER BY id").fetchall()
        rem = remaining_days()
    lines = ["📊 <b>Tình trạng</b>", ""]
    lines.append(f"Kế hoạch: #KH{lp['id']} ({lp['status']}), còn {rem} ngày chưa viết" if lp else "Kế hoạch: chưa có")
    names = {"script_review": "chờ duyệt kịch bản", "awaiting_clips": "chờ clip", "rendering": "đang ghép",
             "video_review": "chờ duyệt video"}
    for w in waiting:
        lines.append(f"• #{w['id']}: {names.get(w['status'], w['status'])}")
    lines.append("Thống kê: " + ", ".join(f"{r['status']}={r['n']}" for r in rows) if rows else "Chưa có video.")
    pubs = publishers()
    lines.append("TikTok: " + (", ".join(f"{p.account} {'✅' if p.connected() else '❌'}" for p in pubs)
                               if pubs else "chưa cấu hình TIKTOK_CLIENT_KEY"))
    n_music = len(music_tracks())
    lines.append(f"Nhạc nền: {n_music} bài" + ("" if n_music else " — gửi file mp3 vào đây để thêm (/nhac)"))
    return "\n".join(lines)


def budget_text() -> str:
    with LOCK:
        rep = BUDGET.report()
        month = BUDGET.month()
    lines = [f"💰 <b>Chi phí tháng {month}</b>", ""]
    for r in rep:
        lines.append(f"• {esc(r['label'])}: đã dùng <b>${r['spent']:.2f}</b> / hạn mức ${r['free'] + r['approved']:.2f} "
                     f"(miễn phí ${r['free']:.2f} + đã duyệt ${r['approved']:.2f})")
    lines += ["", "Giọng đọc (edge-tts), ghép video (FFmpeg), clip Google Flow, Telegram, n8n: $0."]
    return "\n".join(lines)


def tiktok_links() -> dict:
    pubs = publishers()
    if not pubs:
        return reply(msg("Chưa có TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET trong file .env (xem HUONG_DAN.md, bước 9)."))
    lines = ["🔗 <b>Kết nối TikTok</b>", "Mở link bằng Safari, đăng nhập <b>đúng tài khoản</b> rồi bấm Cho phép. "
             "Kết nối tài khoản tiếp theo thì đăng xuất TikTok trên Safari trước.", ""]
    states = DB_.kv_get("tiktok_states") or {}
    for p in pubs:
        nonce = secrets.token_urlsafe(12)
        states[nonce] = {"account": p.account, "ts": now_ts()}
        lines.append(f"• {esc(p.account)} {'(đã kết nối ✅)' if p.connected() else ''}\n{esc(p.authorize_url(nonce))}")
    with LOCK:
        DB_.kv_set("tiktok_states", {k: v for k, v in states.items() if v["ts"] > now_ts() - 3600})
    return reply(msg("\n".join(lines)))


def plan_cmd() -> dict:
    cast = cast_items()
    if not cast:
        return reply(msg(
            "🧑‍🎨 <b>Trước khi lập kế hoạch: bạn có muốn chọn nhân vật bằng ảnh không?</b>\n\n"
            "Gửi ảnh từng nhân vật vào đây (mỗi ảnh một tin), phần chú thích ghi <b>tên – vai</b>, ví dụ:\n"
            "<code>Bé Na - con gái út 8 tuổi</code>\n<code>Bà nội - 70 tuổi</code>\n"
            "Bot đọc ảnh và lưu mô tả. Gửi xong thì gõ /kehoach lần nữa.\n\n"
            "Hoặc để AI tự tạo cả gia đình nhân vật:",
            kb([("✨ Để AI tự tạo nhân vật", "pk:0")])))
    names = ", ".join(c["name"] for c in cast)
    return reply(msg(f"⏳ Đang lập kế hoạch 30 ngày với {len(cast)} nhân vật từ ảnh của bạn ({esc(names)}) "
                     "(2–5 phút)…"), next_step={"action": "plan"})


def characters() -> dict:
    with LOCK:
        plan = active_plan() or latest_plan()
    cast = cast_items()
    photos = [media("photo", DATA / c["image"], f"🧑‍🎨 {esc(c['name'])} – {esc(c['role'])} (ảnh mẫu, dùng làm Ingredients "
                    "trong Flow)", kb([("🗑 Xoá", f"cx:{c['id']}")]))
              for c in cast if (DATA / c["image"]).is_file()]
    if not plan:
        tip = msg("Chưa có kế hoạch. Gõ /kehoach — kế hoạch sẽ dùng các nhân vật trong ảnh bạn đã gửi."
                  if cast else "Chưa có kế hoạch. Gõ /kehoach trước.\n\n💡 Gửi ảnh nhân vật (kèm chú thích tên, "
                  "vd <code>Bé Na - con gái út 8 tuổi</code>) để kế hoạch dùng đúng nhân vật bạn muốn.")
        return reply(tip, *photos)
    b = plan["data"]["bible"]
    lines = [f"🏡 <b>Nhân vật cố định</b> ({esc(b['era'])})", ""]
    lines += [f"• <b>{esc(c['name'])}</b> – {esc(c['role'])}\n<i>{esc(c['look_en'])}</i>" for c in b["characters"]]
    lines += ["", "🖼 <b>Tạo ảnh nhân vật chuẩn (làm 1 lần/tháng)</b>: dán prompt dưới vào Google Flow "
              "(chế độ tạo ảnh) hoặc Gemini/Whisk, lưu ảnh đẹp nhất vào Ảnh của iPad. Mỗi lần tạo clip, thêm ảnh này "
              "vào <i>Ingredients</i>.", "", f"<code>{esc(series.character_sheet_prompt(b))}</code>",
              "", "💡 Muốn nhân vật theo ảnh của bạn: gửi ảnh vào đây kèm chú thích tên và vai, "
              "vd <code>Bé Na - con gái út 8 tuổi</code>."]
    if photos:
        lines += ["", "⬇️ Ảnh mẫu bạn đã gửi (lưu lại để thêm vào Ingredients trong Flow):"]
    return reply(msg("\n".join(lines)), *photos)


def resend_prompts() -> dict:
    with LOCK:
        r = DB_.conn.execute("SELECT id FROM content WHERE status='awaiting_clips' ORDER BY id DESC LIMIT 1").fetchone()
    return reply(prompt_messages(r["id"])) if r else reply(msg("Không có video nào đang chờ clip."))


# ====================================================================== HTTP layer

def _auth(token: str) -> None:
    if not WORKER_TOKEN or not secrets.compare_digest(token or "", WORKER_TOKEN):
        raise HTTPException(401, "bad worker token")


# Endpoints are plain `def` on purpose: FastAPI runs them in a thread pool, so a long Claude call or
# render never blocks other requests (and edge-tts can start its own event loop).
BODY = Body(default={})
TOKEN = Header(default="", alias="X-Worker-Token")


def _tg_guard(body: dict) -> dict | None:
    """Return a reply that stops processing, or None if the Telegram update is from the owner."""
    if WEBHOOK_SECRET and not secrets.compare_digest(str(body.get("secret", "")), WEBHOOK_SECRET):
        log.warning("Bỏ qua yêu cầu không đến từ Telegram (sai secret)")
        return reply()
    chat = body.get("chat_id")
    if not CHAT:
        if chat and str(body.get("text", "")).startswith("/"):
            return reply(msg(f"Chat ID của bạn là <code>{chat}</code>.\nĐiền vào TELEGRAM_CHAT_ID trong file .env "
                             "rồi chạy lại lệnh cài (xem HUONG_DAN.md).", chat=chat))
        return reply()
    if str(chat) != str(CHAT):
        log.warning("Bỏ qua tin nhắn từ chat lạ %s", chat)
        return reply()
    return None


def _safe(fn, *args) -> dict:
    try:
        return fn(*args)
    except BudgetNeeded as b:
        return reply(budget_msg(b.request))
    except HTTPException:
        raise
    except Exception as e:
        log.exception("handler error")
        return reply(msg(f"⚠️ Lỗi: {esc(str(e)[:500])}"))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/tg/command")
def tg_command(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    cmd = str(body.get("text", "")).split()[0].split("@")[0].lower()
    table = {"/kehoach": plan_cmd,
             "/kichban": lambda: reply(msg("⏳ Đang viết kịch bản (khoảng 1 phút)…"), next_step={"action": "script"}),
             "/clip": resend_prompts,
             "/trangthai": lambda: reply(msg(status_text())), "/ngansach": lambda: reply(msg(budget_text())),
             "/nhanvat": characters, "/tiktok": tiktok_links, "/nhac": lambda: reply(music_list()),
             "/phongcach": lambda: style_cmd(str(body.get("text", ""))),
             "/drive": drive_cmd}
    return _safe(table.get(cmd, lambda: reply(msg(HELP))))


@app.post("/tg/button")
def tg_button(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    action, _, arg = str(body.get("data", "")).partition(":")
    mid = body.get("message_id")
    try:
        n = int(arg)
    except ValueError:
        return reply()
    handlers = {
        "pa": lambda: approve_plan(n, mid),
        "pr": lambda: _redo_plan(n, None, mid),
        "sa": lambda: approve_script(n, mid),
        "sr": lambda: reply(clear_buttons(mid), msg("⏳ Đang viết bản khác…"),
                            next_step={"action": "rewrite", "id": n}),
        "sx": lambda: _set_status(n, "script_review", "script_rejected", mid,
                                  f"❌ Đã bỏ kịch bản #{n}. Gõ /kichban để viết kịch bản ngày tiếp theo."),
        "pg": lambda: request_paid(n, mid),
        "va": lambda: publish(n, mid),
        "vc": lambda: reset_clips(n, mid),
        "vx": lambda: _set_status(n, "video_review", "video_rejected", mid, f"❌ Đã bỏ video #{n}."),
        "rr": lambda: _retry_render(n, mid),
        "ba": lambda: _budget_decision(n, True, mid),
        "bx": lambda: _budget_decision(n, False, mid),
        "mx": lambda: delete_music(n, mid),
        "ca": lambda: apply_cast_to_plan(n, mid),
        "am": lambda: reply(msg("⏳ Đang đổi nhạc…"), next_step={"action": "audio", "id": n, "what": "music"}),
        "av": lambda: reply(msg("⏳ Đang đổi giọng đọc…"), next_step={"action": "audio", "id": n, "what": "voice"}),
        "dc": lambda: drive_check(mid),
        "ds": lambda: reply(clear_buttons(mid), msg(("📤 Đồng bộ Google Drive:" + sync_drive()) if DRIVE else "")),
        "dx": lambda: (DRIVE.disconnect() if DRIVE else None) or reply(clear_buttons(mid),
                                                                        msg("🔌 Đã ngắt kết nối Google Drive.")),
        "pk": lambda: reply(clear_buttons(mid), msg("⏳ Đang quét đối thủ và lập kế hoạch 30 ngày (2–5 phút)…"),
                            next_step={"action": "plan"}),
        "cx": lambda: delete_cast(n, mid),
    }
    return _safe(handlers.get(action, reply))


def _set_status(cid: int, expect: str, new: str, mid: int | None, text: str) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != expect:
            return reply(clear_buttons(mid), msg(f"#{cid} không còn ở bước này."))
        DB_.update_content(cid, status=new)
    return reply(clear_buttons(mid), msg(text))


def _retry_render(cid: int, mid: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "failed" or not manual().load(cid):
            return reply(clear_buttons(mid), msg(f"#{cid} không thể ghép lại lúc này."))
        DB_.update_content(cid, status="rendering")
    return reply(clear_buttons(mid), msg(f"⏳ Ghép lại video #{cid}…"), next_step={"action": "render", "id": cid})


def _redo_plan(pid: int, feedback: str | None, mid: int | None) -> dict:
    with LOCK:
        p = get_plan(pid)
    if not p or p["status"] != "review":
        return reply(clear_buttons(mid), msg(f"Kế hoạch #KH{pid} không còn chờ duyệt."))
    return reply(clear_buttons(mid), msg("🔄 Đang lập lại kế hoạch (1–3 phút)…"),
                 next_step={"action": "plan_redo", "id": pid, "feedback": feedback})


def _budget_decision(rid: int, approve: bool, mid: int | None) -> dict:
    with LOCK:
        req = BUDGET.decide(rid, approve)
    if not req:
        return reply(clear_buttons(mid))
    if req["status"] == "rejected":
        resume = json.loads(req["resume"]) if req["resume"] else {}
        extra = []
        if resume.get("action") == "paid" and resume.get("id"):
            with LOCK:
                it = DB_.get_content(int(resume["id"]))
            if it and it["status"] == "awaiting_clips":
                extra = [msg("Không tạo tự động. Bạn tạo tay miễn phí trên Google Flow nhé:"),
                         *prompt_messages(int(resume["id"]))]
        return reply(clear_buttons(mid), msg(f"❌ Đã từ chối ngân sách #NS{rid}. Không phát sinh chi phí."), *extra)
    if req["status"] != "approved":
        return reply(clear_buttons(mid))
    resume = json.loads(req["resume"]) if req["resume"] else None
    return reply(clear_buttons(mid), msg(f"✅ Đã duyệt ${req['amount_usd']:.2f} cho {esc(LABELS.get(req['service'], req['service']))} "
                                         f"tháng {req['month']}."), next_step=resume)


@app.post("/tg/text")
def tg_text(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    ref, text = body.get("reply_to_text") or "", str(body.get("text", "")).strip()
    if m := re.search(r"#KH(\d+)", ref):
        return _safe(_redo_plan, int(m.group(1)), text, None)
    if m := re.search(r"Kịch bản #(\d+)", ref):
        return reply(msg("⏳ Đang sửa kịch bản theo góp ý…"),
                     next_step={"action": "rewrite", "id": int(m.group(1)), "feedback": text})
    return reply(msg("Để góp ý, hãy <b>Trả lời (Reply)</b> đúng tin nhắn kịch bản/kế hoạch.\n\n" + HELP))


@app.post("/tg/clip")
def tg_clip(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    return _safe(receive_clip, body)


@app.post("/tg/audio")
def tg_audio(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    return _safe(receive_audio, body)


@app.post("/tg/photo")
def tg_photo(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    return _safe(receive_photo, body)


@app.post("/scheduled/plan")
def scheduled_plan(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    with LOCK:
        lp = latest_plan()
        need = not (lp and lp["status"] == "review") and remaining_days() <= 2
    return _safe(create_plan) if need else reply()


@app.post("/scheduled/daily")
def scheduled_daily(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    return _safe(next_script, True)


@app.post("/continue")
def cont(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    action, n = body.get("action"), body.get("id")
    if action == "render":
        return _safe(do_render, int(n))
    if action == "generate":
        return _safe(do_render, int(n), True)
    if action == "plan":
        return _safe(create_plan)
    if action == "plan_redo":
        p = get_plan(int(n))
        return _safe(create_plan, body.get("feedback") or "Làm một kế hoạch khác hẳn.", p["data"] if p else None)
    if action == "script":
        return _safe(next_script)
    if action == "rewrite":
        return _safe(rewrite_script, int(n), body.get("feedback"))
    if action == "audio":
        return _safe(change_audio, int(n), body.get("what") or "music")
    if action == "photo":
        return _safe(describe_photo, body.get("path") or "", body.get("caption") or "")
    if action == "paid":
        return _safe(request_paid, int(n), None)
    return reply()


@app.get("/files")
def files(path: str, x_worker_token: str = TOKEN):
    _auth(x_worker_token)
    p = (DATA / path).resolve()
    if not p.is_relative_to(DATA) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(code: str = "", state: str = "", error: str = "") -> str:
    def page(t: str) -> str:
        return (f"<meta name=viewport content='width=device-width'><body style='font:18px sans-serif;padding:24px'>"
                f"{t}</body>")
    if error or not code:
        return page(f"❌ TikTok báo lỗi: {esc(error or 'thiếu code')}")
    with LOCK:
        states = DB_.kv_get("tiktok_states") or {}
        st = states.pop(state, None)
        DB_.kv_set("tiktok_states", states)
    if not st:
        return page("❌ Link đã hết hạn. Gõ /tiktok trên Telegram để lấy link mới.")
    pub = next((p for p in publishers() if p.account == st["account"]), None)
    if not pub:
        return page("❌ Không tìm thấy tài khoản trong config.yaml.")
    try:
        pub.exchange_code(code)
    except Exception as e:
        return page(f"❌ Kết nối lỗi: {esc(str(e)[:300])}")
    return page(f"✅ Đã kết nối TikTok cho <b>{esc(st['account'])}</b>. Bạn có thể đóng trang này.")
