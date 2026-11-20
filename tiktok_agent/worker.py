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

from . import audio, content, dialogue, fonts, gdrive, scraper, series, video
from .ai_video import AIAuthError, AIBudgetExceeded, AIPolicyError, AIQuotaError, AIVideoSource
from .budget import LABELS, Budget, BudgetNeeded
from .cast import Cast, bible_characters, deaccent, parse_edit, parse_voice
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
CAST = Cast(DATA, S.raw.get("cast"))
CAST.load()  # creates data/nhan_vat.yaml from config.yaml on first run
LOCK = threading.RLock()          # guards SQLite + manifests
HEAVY = threading.Lock()          # one render / paid generation at a time
BUSY: set[str] = set()            # avoids running the same Claude job twice on double taps

WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
TG_API = os.getenv("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
CHAT = S.telegram_chat_id
AI_CFG = {**(S.raw.get("ai_video") or {}), "enabled": True, "scenes": "all"}
SCENES = int(S["content"].get("scenes", 3))  # default; /socanh overrides (see scenes_count)
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


TG_LIMIT = 4096


def visible_len(text: str) -> int:
    """Length Telegram counts for an HTML message (tags removed, entities decoded)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def msg(text: str, buttons: dict | None = None, chat: int | None = None) -> dict:
    p = {"chat_id": chat or CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if visible_len(text) > TG_LIMIT or len(text) > 3 * TG_LIMIT:
        # never cut HTML (a cut tag makes Telegram refuse the whole message): send it as plain text instead
        p["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))[:TG_LIMIT - 1]
        p.pop("parse_mode")
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


def safe_caption(caption: str, limit: int = 1024) -> str:
    """Telegram captions max 1024 chars; cutting HTML mid-tag makes Telegram refuse the whole message."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", caption))
    if len(plain) <= limit:
        return caption
    return esc(plain[:limit - 30].rstrip() + "…")


def media(kind: str, path: Path, caption: str, buttons: dict | None = None) -> dict:
    payload = {"chat_id": str(CHAT), "caption": safe_caption(caption), "parse_mode": "HTML",
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
            + (f"Lần này cần tối thiểu: <b>${req['need_usd']:.2f}</b>\n" if req.get("need_usd") else "")
            + f"\nLý do: {esc(req['reason'] or '')}\n\n"
            "Chọn mức duyệt thêm cho tháng này (chỉ là hạn mức tối đa, dùng bao nhiêu tính bấy nhiêu). "
            "Hệ thống sẽ không tiêu thêm đồng nào cho dịch vụ này tới khi bạn duyệt.")
    opts = BUDGET.options(req)
    rows = [[(f"✅ Duyệt ${o:g}", f"bo:{req['id']}_{int(round(o * 100))}") for o in opts[i:i + 2]]
            for i in range(0, len(opts), 2)]
    return msg(text, kb(*rows, [("❌ Từ chối", f"bx:{req['id']}")]))


HELP = ("🤖 <b>Trợ lý kênh TikTok</b>\n\n"
        "/kehoach – lập kế hoạch 30 ngày mới\n"
        "/kichban – viết ngay kịch bản tiếp theo\n"
        "/clip – gửi lại prompt clip đang chờ (/clip 2 = riêng cảnh 2)\n"
        "/tansuat – tần suất ra video (vd /tansuat 2 = 2 ngày 1 video, áp dụng từ ngày mai)\n"
        "/socanh – số cảnh mỗi video (vd /socanh 4 ≈ 32 giây)\n"
        "/doithu, /kenhmau – xem/sửa kênh đối thủ và kênh mẫu (vd /doithu them @kenh), rồi lập kế hoạch mới\n"
        "/font – xem/đổi font chữ đầu video, phụ đề, câu kết (có ảnh minh hoạ)\n"
        "/trangthai – xem tình trạng hệ thống\n"
        "/ngansach – chi phí tháng này\n"
        "/nhanvat – 6 nhân vật cố định (ảnh mẫu, ngoại hình, cách nói)\n"
        "/giong – nghe/chọn giọng từng nhân vật; /giong Ông pitch -10 rate -5 – nhập cao độ/tốc độ\n"
        "/themnv Tên - vai – thêm nhân vật mới (vd /themnv Cô Lan - cô giáo)\n"
        "📷 Gửi ảnh + chú thích tên nhân vật (vd 'Ông', 'Bé Na') – ảnh mẫu (gửi 1 lần, dùng mãi)\n"
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
    return series.set_cast(b, bible_characters(cast)) if cast else b


def content_bible(item: dict) -> dict:
    plan = get_plan(item["idea"]["plan_id"]) if item["idea"].get("plan_id") else None
    return fix_bible((plan or {}).get("data", {}).get("bible", {}))


def is_dialogue(script: dict | None) -> bool:
    return any("lines" in s for s in (script or {}).get("scenes", []))


def rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(DATA))


# ====================================================================== plan

# ---------------------------------------------------------------- competitor / reference channels (Telegram)

CHANNELS_KEY = "channels"
USER_RX = re.compile(r"^[A-Za-z0-9._]{2,24}$")


def channels() -> tuple[list[str], list[str]]:
    """(reference channels, competitors): the owner's Telegram list if set, else config.yaml."""
    with LOCK:
        kv = DB_.kv_get(CHANNELS_KEY) or {}
    refs = kv.get("reference") if "reference" in kv else S.raw.get("reference_channels") or []
    comps = kv.get("competitors") if "competitors" in kv else S["competitors"] or []
    clean = lambda xs: list(dict.fromkeys(str(c).lstrip("@").strip() for c in xs if str(c).strip()))
    return clean(refs), clean(comps)


def parse_channel(tok: str) -> str | None:
    """'@abc', 'abc', 'tiktok.com/@abc/video/1', or a short vt.tiktok.com link (resolved) → 'abc'."""
    t = tok.strip().strip("<>,;")
    if re.match(r"https?://(vt|vm)\.tiktok\.com/", t) or re.match(r"https?://(www\.)?tiktok\.com/t/", t):
        try:
            t = str(httpx.get(t, follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).url)
        except httpx.HTTPError:
            return None
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", t)
    name = m.group(1) if m else t.lstrip("@")
    return name if USER_RX.match(name) else None


def channels_text() -> str:
    refs, comps = channels()
    return ("📺 <b>Kênh mẫu</b> (học cách trình bày, ưu tiên nhất): "
            + (", ".join(f"@{esc(c)}" for c in refs) or "<i>chưa có</i>")
            + "\n🔎 <b>Kênh đối thủ</b> (học chủ đề, hook): " + (", ".join(f"@{esc(c)}" for c in comps) or "<i>chưa có</i>"))


CHANNEL_HELP = ("Cách sửa (dán @tên, link kênh hoặc link video TikTok, nhiều kênh cách nhau dấu cách):\n"
                "<code>/doithu them @kenh1 @kenh2</code> · <code>/doithu xoa @kenh1</code> · "
                "<code>/doithu dat @a @b</code> (thay cả danh sách)\n"
                "<code>/kenhmau them @kenh</code> · <code>/kenhmau xoa @kenh</code> · <code>/kenhmau dat @kenh</code>")


def channels_cmd(text: str, which: str) -> dict:
    """/doithu (which=competitors) and /kenhmau (which=reference): show / them / xoa / dat."""
    args = text.split()[1:]
    if not args:
        return reply(msg(f"{channels_text()}\n\n{CHANNEL_HELP}", kb([("🔄 Lập kế hoạch mới theo danh sách này", "pn:0")])))
    op = deaccent(args[0])
    names, bad = [], []
    for tok in args[1:]:
        n = parse_channel(tok)
        (names if n else bad).append(n or tok)
    if op not in ("them", "add", "xoa", "del", "dat", "set") or (not names and op != "dat"):
        return reply(msg(("⚠️ Không đọc được: " + esc(", ".join(bad)) + "\n\n" if bad else "") + CHANNEL_HELP))
    refs, comps = channels()
    cur = refs if which == "reference" else comps
    if op in ("them", "add"):
        new = cur + [x for x in names if x not in cur]
    elif op in ("xoa", "del"):
        new = [x for x in cur if x.lower() not in {n.lower() for n in names}]
    else:
        new = names
    with LOCK:
        kv = DB_.kv_get(CHANNELS_KEY) or {}
        kv.update({"reference": refs, "competitors": comps})
        kv[which] = new
        kv["changed_at"] = now_ts()
        DB_.kv_set(CHANNELS_KEY, kv)
    warn = ("\n⚠️ Không đọc được: " + esc(", ".join(bad))) if bad else ""
    return reply(msg(f"✅ Đã cập nhật.{warn}\n\n{channels_text()}\n\n"
                     "Kế hoạch đang chạy vẫn giữ nguyên. Bấm nút dưới để quét các kênh này và lập <b>kế hoạch mới</b>; "
                     "duyệt xong, mọi kịch bản sau đó viết theo kế hoạch mới.",
                     kb([("🔄 Lập kế hoạch mới theo danh sách này", "pn:0")])))


def replan_for_channels(mid: int | None) -> dict:
    refs, comps = channels()
    fb = ("Danh sách kênh vừa được chủ kênh cập nhật. Kênh mẫu: " + (", ".join("@" + c for c in refs) or "không có")
          + ". Kênh đối thủ: " + (", ".join("@" + c for c in comps) or "không có")
          + ". Hãy rút pattern MỚI từ dữ liệu các kênh này (hook, chủ đề, nhịp, caption) và lập kế hoạch theo đó; "
            "giữ nguyên gia đình nhân vật cố định.")
    return reply(clear_buttons(mid), msg("⏳ Đang quét các kênh và lập kế hoạch mới (2–5 phút)…"),
                 next_step={"action": "plan_channels", "feedback": fb})


def create_plan(feedback: str | None = None, previous: dict | None = None) -> dict:
    if "plan" in BUSY:
        return reply(msg("⏳ Đang lập kế hoạch, vui lòng chờ…"))
    svc = (S.raw.get("budget") or {}).get("services", {})
    with LOCK:
        BUDGET.ensure("claude", float(svc.get("claude", {}).get("est_plan_usd", 0.8)),
                      f"Lập kế hoạch {PLAN_DAYS} ngày (phân tích đối thủ + series bible).",
                      resume={"action": "plan_channels", "feedback": feedback} if feedback and not previous
                      else {"action": "plan"})
    BUSY.add("plan")
    try:
        refs, comps = channels()
        competitors = list(dict.fromkeys(refs + comps))
        notes = []
        if S.apify_token and competitors:
            est = float(svc.get("apify", {}).get("est_scrape_usd", 0.5))
            with LOCK:
                BUDGET.ensure("apify", est, "Quét video mới của các kênh đối thủ.", resume={"action": "plan_channels", "feedback": feedback} if feedback and not previous
                      else {"action": "plan"})
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
        cast = bible_characters(cast_items())
        plan = series.make_plan(niche(), top, recent, PLAN_DAYS, feedback=feedback, previous=previous,
                                reference=ref_top, cast=cast)
        data = plan.model_dump()
        data["bible"] = fix_bible(data["bible"])
        if cast:
            notes.append(f"👨‍👩‍👧‍👦 Gia đình cố định: {', '.join(c['name'] for c in cast)} (/nhanvat để xem/sửa).")
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
                 msg(f"✅ Đã duyệt kế hoạch #KH{pid}. Bot gửi kịch bản lúc 07:00 theo tần suất "
                     f"{freq_label(freq_on(dt.datetime.now(TZ).date())['days'])} (/tansuat để đổi).\n"
                     "Muốn làm ngay kịch bản đầu tiên: gõ /kichban\n\n"
                     "💡 Bước nên làm 1 lần: gõ /nhanvat — gửi ảnh mẫu cho 6 nhân vật để mọi clip giống nhau, "
                     "và /giong để nghe/chọn giọng từng người."))


# ====================================================================== scripts

def thumb_text(sc: dict) -> str:
    return (sc.get("thumb_text") or sc.get("title") or "").strip()


def closing_line(sc: dict) -> str:
    return (sc.get("closing_line") or sc.get("lesson") or "").strip()


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
    lines += [f"💡 <b>Thông điệp:</b> {esc(sc['lesson'])}",
              f"🖼 <b>Chữ trên ảnh bìa:</b> {esc(thumb_text(sc))}",
              f"🔚 <b>Câu kết (hiện ở clip cuối):</b> {esc(closing_line(sc))}", "",
              f"📣 <b>Caption:</b> {esc(item['caption'])}", ""]
    if is_dialogue(sc):  # order: script → background music → auto or manual clips
        lines.append("👉 Duyệt kịch bản → duyệt <b>nhạc nền</b> → duyệt <b>chữ</b> (ảnh minh hoạ font) → chọn tạo clip tự "
                     "động hay bằng tay. "
                     "Muốn sửa thoại/nội dung: <b>Trả lời (Reply)</b> tin này và gõ góp ý.")
        return long_msg("\n".join(lines), kb([("✅ Duyệt kịch bản", f"sa:{cid}")],
                                             [("🔄 Viết bản khác", f"sr:{cid}"), ("❌ Bỏ", f"sx:{cid}")]))
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
                         d / f"preview_{now_ts()}.mp3", spans=[(x["start"], x["end"]) for x in meta["scenes"]])
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
    mpath = (str(DATA / music) if music and not Path(music).is_absolute() else music) or None
    fixed = voice_mode() == "fixed"
    if fixed:  # the approved preview IS the final soundtrack: fixed character voices + music
        normalize_speakers(sc, content_bible(item))
        chars = {c["name"]: c for c in content_bible(item).get("characters", [])}
        tracks = dialogue.voice_tracks(sc["scenes"], chars, S.raw, float(CLIP_SEC), d / "voices")
        prev = dialogue.dialogue_preview(tracks, mpath, total, S.raw, d / f"music_{now_ts()}.mp3")
    else:
        prev = dialogue.music_preview(mpath, total, S.raw, d / f"music_{now_ts()}.mp3")
    for old in d.glob("music_*.mp3"):
        if old != prev:
            old.unlink(missing_ok=True)
    sc["audio"] = {"mode": "dialogue", "voices": "fixed" if fixed else "clip", "music": music,
                   "preview": rel(prev) if prev else "", "total": total}
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
        rows = [[("✅ Duyệt nhạc nền", f"ma:{cid}"), ("🎵 Đổi nhạc", f"am:{cid}")]]
        if not a.get("preview") or not (DATA / a["preview"]).is_file():
            return msg(f"🎵 <b>#{cid} – Nhạc nền: thư viện đang trống.</b>\nGửi file nhạc mp3/m4a (không bản quyền) vào "
                       "đây, rồi bấm <b>🎵 Đổi nhạc</b> để nghe thử. Video cần nhạc nền xuyên suốt từ đầu đến cuối.",
                       kb([("🎵 Đổi nhạc", f"am:{cid}")], [("⏭ Tạm bỏ qua (không nhạc)", f"ma:{cid}")]))
        music = esc(Path(a["music"]).stem) if a.get("music") else "không nhạc"
        if a.get("voices") == "fixed":
            cap = (f"🎧 <b>#{cid} – Nghe thử thoại + nhạc</b> ({a['total']:.0f}s)\n🎵 Nhạc: {music}\n\n"
                   "Đây chính là âm thanh của video thành phẩm: mỗi nhân vật luôn đúng giọng cố định của mình ở mọi "
                   "clip (/giong để nghe/đổi giọng từng người).")
        else:
            cap = (f"🎵 <b>#{cid} – Nhạc nền:</b> {music} ({a['total']:.0f}s)\n\n"
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
    if not item or item["status"] not in ("script_review", "music_review"):
        return reply(msg(f"Kịch bản #{cid} không còn ở bước duyệt."))
    a = item["script"].get("audio") or {}
    if is_dialogue(item["script"]):
        opts = [rel(t) for t in music_tracks()]  # every video has music: no "no music" option
        if not opts:
            return reply(msg("🎵 Thư viện nhạc đang trống. Gửi file mp3/m4a vào đây để thêm nhạc, rồi bấm Đổi nhạc."))
        cur = a.get("music", "")
        prepare_music(cid, opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0])
        return reply(audio_message(cid))
    if what == "music":
        opts = [rel(t) for t in music_tracks()]
        if not opts:
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
    n = scenes_count()
    script = series.write_script(niche(), bible, idea, patterns, n, CLIP_SEC, owner_notes(),
                                 feedback=feedback, previous=previous).model_dump()
    script["scenes"] = script["scenes"][:n]
    link_scenes(script)
    normalize_speakers(script, bible)
    caption = script["caption"] + "\n\n" + " ".join(f"#{h.lstrip('#')}" for h in script["hashtags"])
    return script, caption


def scenes_count() -> int:
    with LOCK:
        kv = DB_.kv_get("scene_count") or {}
    return max(2, min(6, int(kv.get("n") or SCENES)))


def scenes_cmd(text: str) -> dict:
    arg = text.partition(" ")[2].strip()
    m = re.search(r"\d+", arg)
    if not m:
        n = scenes_count()
        return reply(msg(f"🎬 <b>Số cảnh mỗi video:</b> {n} cảnh × {CLIP_SEC} giây ≈ {n * CLIP_SEC} giây.\n\n"
                         "Đổi: gõ <code>/socanh N</code> (2–6) hoặc bấm nút. Áp dụng cho kịch bản viết tiếp theo "
                         "(kịch bản, thoại, prompt và số clip tự điều chỉnh).",
                         kb([(f"{k} cảnh (~{k * CLIP_SEC}s)", f"sn:{k}") for k in (3, 4, 5)])))
    return set_scenes(int(m.group()))


def set_scenes(n: int, mid: int | None = None) -> dict:
    if not 2 <= n <= 6:
        return reply(msg("Số cảnh phải từ 2 đến 6 (mỗi cảnh là 1 clip 8 giây)."))
    with LOCK:
        DB_.kv_set("scene_count", {"n": n, "ts": now_ts()})
    usd = float((S.raw.get("budget") or {}).get("services", {}).get("gemini_video", {}).get("usd_per_clip", 0.19))
    return reply(clear_buttons(mid), msg(
        f"✅ Mỗi video giờ gồm <b>{n} cảnh</b> (≈ {n * CLIP_SEC} giây). Áp dụng từ kịch bản viết tiếp theo; kịch bản "
        f"đang làm giữ nguyên.\nTạo tay: {n} clip trên Flow mỗi video. Tự động (Gemini): ≈ ${usd * n:.2f}/video."))


def link_scenes(script: dict) -> dict:
    """Make every scene start where the previous one ended (the writer is asked to; this enforces it)."""
    prev = None
    for sc in script.get("scenes", []):
        if prev is not None and prev.get("end_en"):
            sc["start_en"] = prev["end_en"]
        prev = sc
    return script


def normalize_speakers(script: dict, bible: dict) -> dict:
    """Map every speaker / on-screen name to the exact cast name, and make sure speakers are listed in the scene."""
    names = {c["name"].strip().lower(): c["name"] for c in bible.get("characters", [])}

    def fix(n: str) -> str:
        if n.strip().lower() in names:
            return names[n.strip().lower()]
        c = CAST.find(n)
        return c["name"] if c and c["name"].strip().lower() in names else n

    for sc in script.get("scenes", []):
        on = [fix(n) for n in sc.get("characters") or []]
        for ln in sc.get("lines") or []:
            ln["speaker"] = fix(ln.get("speaker", ""))
            if ln["speaker"] not in on:
                on.append(ln["speaker"])
        if "characters" in sc or on:
            sc["characters"] = list(dict.fromkeys(on))
    return script


# ---------------------------------------------------------------- posting frequency (/tansuat)

FREQ_KEY = "frequency"


def freq_periods() -> list[dict]:
    """[{from: 'YYYY-MM-DD', days: N}] oldest first; the first period comes from config (default every day)."""
    base = max(1, int(S["content"].get("every_days", 1) or 1))
    with LOCK:
        saved = (DB_.kv_get(FREQ_KEY) or {}).get("periods") or []
    return [{"from": "2000-01-01", "days": base}] + saved


def freq_on(day: dt.date) -> dict:
    return [p for p in freq_periods() if dt.date.fromisoformat(p["from"]) <= day][-1]


def is_due(day: dt.date) -> bool:
    p = freq_on(day)
    return (day - dt.date.fromisoformat(p["from"])).days % int(p["days"]) == 0


def next_due_days(start: dt.date, count: int = 5) -> list[dt.date]:
    out, d = [], start
    while len(out) < count and (d - start).days < 400:
        if is_due(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def freq_label(n: int) -> str:
    return "mỗi ngày 1 video" if n == 1 else f"{n} ngày 1 video"


def set_frequency(n: int, mid: int | None = None) -> dict:
    if not 1 <= n <= 30:
        return reply(msg("Tần suất phải từ 1 đến 30 ngày. Ví dụ <code>/tansuat 2</code> = 2 ngày ra 1 video."))
    tomorrow = dt.datetime.now(TZ).date() + dt.timedelta(days=1)
    with LOCK:
        saved = [p for p in (DB_.kv_get(FREQ_KEY) or {}).get("periods") or [] if p["from"] < tomorrow.isoformat()]
        saved.append({"from": tomorrow.isoformat(), "days": n, "set_at": now_ts()})
        DB_.kv_set(FREQ_KEY, {"periods": saved[-20:]})
    dates = ", ".join(d.strftime("%d/%m") for d in next_due_days(tomorrow))
    return reply(clear_buttons(mid), msg(
        f"✅ Đã đặt tần suất: <b>{freq_label(n)}</b>, áp dụng từ ngày mai ({tomorrow.strftime('%d/%m')}). "
        f"Hôm nay giữ như cũ.\n📅 Các ngày bot gửi kịch bản (07:00): {dates}\n\n"
        "Kế hoạch 30 ý tưởng vẫn dùng lần lượt, chỉ giãn ra theo tần suất mới. Cần video gấp: gõ /kichban bất kỳ lúc nào."))


def frequency_cmd(text: str) -> dict:
    arg = text.partition(" ")[2].strip()
    if arg:
        m = re.search(r"\d+", arg)
        return set_frequency(int(m.group())) if m else reply(msg("Gõ số ngày, ví dụ <code>/tansuat 2</code>."))
    today = dt.datetime.now(TZ).date()
    cur = freq_on(today)["days"]
    tomorrow = today + dt.timedelta(days=1)
    nxt = freq_on(tomorrow)["days"]
    dates = ", ".join(d.strftime("%d/%m") for d in next_due_days(today))
    lines = [f"🗓 <b>Tần suất ra video:</b> {freq_label(cur)}"
             + (f" (từ ngày mai: {freq_label(nxt)})" if nxt != cur else ""),
             f"📅 Các ngày gửi kịch bản sắp tới (07:00): {dates}", "",
             "Đổi tần suất: gõ <code>/tansuat N</code> (N = số ngày ra 1 video, 1–30) hoặc bấm nút. "
             "Áp dụng từ ngày mai."]
    return reply(msg("\n".join(lines), kb([("Mỗi ngày", "tf:1"), ("2 ngày/lần", "tf:2"), ("3 ngày/lần", "tf:3")],
                                          [("4 ngày/lần", "tf:4"), ("Mỗi tuần", "tf:7")])))


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
            if not is_due(dt.datetime.now(TZ).date()):
                return reply()  # not a video day under the current frequency (/tansuat)
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

CLIP_MODES = ("extend", "independent")


def clip_mode(cid: int) -> str | None:
    """How the owner makes the clips by hand: "extend" (ONE video built with Flow's Extend), "independent" (one
    8-second clip per scene, joined by the bot) or None (not chosen yet). Narration videos are always independent."""
    m = manual().load(cid) or {}
    if m.get("mode") in CLIP_MODES:
        return m["mode"]
    item = DB_.get_content(cid)
    return None if item and is_dialogue(item["script"]) else "independent"


def manual_choice_msg(cid: int, head: str = "") -> dict:
    n = len(manual().load(cid)["shots"])
    return msg((head or f"✋ <b>#{cid} – Tạo tay trên Google Flow: chọn cách làm</b>") + "\n"
               f"• <b>🎞 Kéo dài (Extend):</b> bot gửi prompt đầy đủ cho đoạn 1 và {n - 1} prompt kéo dài. Bạn làm "
               "tất cả trong Flow thành <b>1 video hoàn chỉnh</b> rồi gửi lại <b>đúng 1 video</b> — bot không cắt, "
               "không ghép, chỉ thêm nhạc nền, phụ đề, ảnh bìa và câu kết. Hình và giọng liền mạch nhất.\n"
               f"• <b>🧩 Clip độc lập:</b> bot gửi {n} prompt riêng (mỗi cảnh 1 clip 8 giây); bạn gửi lại {n} clip, "
               "bot ghép theo thứ tự.",
               kb([("🎞 Kéo dài – gửi 1 video", f"ce:{cid}")], [("🧩 Clip độc lập – gửi từng clip", f"cm:{cid}")]))


def cast_photo_messages(cid: int, item: dict, bible: dict) -> list[dict]:
    out, used = [], []
    for sc in item["script"]["scenes"]:
        for c in series.scene_cast(bible, sc):
            if c not in used:
                used.append(c)
    for c in used:
        img = CAST.image_path(c)
        if img:
            out.append(media("photo", img, f"🧩 Ảnh mẫu <b>{esc(c['name'])}</b> — thêm vào Ingredients khi cảnh có "
                                           f"{esc(c['name'])}"))
    missing = [c["name"] for c in used if not CAST.image_path(c)]
    if missing:
        out.append(msg(f"⚠️ Chưa có ảnh mẫu cho: {esc(', '.join(missing))}. Gõ /nhanvat để lấy prompt tạo ảnh chân "
                       "dung và gửi lại cho bot — từ lần sau các clip sẽ giống mặt hơn."))
    return out


VOICE_NOTE_KEEP = ("\n🎙 Âm thanh: <b>giữ nguyên</b> như video bạn gửi (giọng, âm lượng, tốc độ); bot chỉ thêm nhạc "
                   "nhỏ phía dưới và phụ đề.")


def prompt_messages(cid: int) -> list[dict]:
    mode = clip_mode(cid)
    if mode is None:
        return [manual_choice_msg(cid)]
    if mode == "extend":
        return extend_messages(cid)
    m = manual().load(cid)
    n = len(m["shots"])
    out = [msg(f"🎥 <b>#{cid} – {n} clip độc lập</b> — bot gửi lần lượt {n} tin <b>Cảnh 1…{n}</b> bên dưới "
               f"(mỗi tin cách nhau ~1 giây). Thiếu cảnh nào: gõ <code>/clip 2</code> (số cảnh) hoặc <code>/clip</code> "
               "để gửi lại tất cả.\n\n"
               "1) Mở <b>labs.google/flow</b> trên Safari (tài khoản Google miễn phí).\n"
               "2) Chạm vào khung prompt của từng cảnh bên dưới để <b>sao chép</b> → dán vào Flow → chọn "
               "khung <b>dọc 9:16</b>, 8 giây → tạo → tải clip về.\n"
               "3) Quay lại Telegram, <b>Trả lời (Reply)</b> đúng tin nhắn \"Cảnh N\" và gửi clip đó "
               "(<b>giữ nguyên âm thanh</b> — Flow tạo luôn giọng thoại của nhân vật). "
               f"Hoặc gửi cả {n} clip một lượt theo đúng thứ tự cảnh.\n\n"
               "🧩 <b>Nhân vật giống nhau:</b> lưu các ảnh mẫu bên dưới; ở MỌI cảnh, trong Flow bấm <b>+ Ingredients</b> "
               "và thêm đúng các ảnh ghi dưới cảnh đó (cùng ảnh cho cả video).\n"
               "🔗 <b>Liền mạch:</b> tạo <b>theo thứ tự</b> Cảnh 1 → 2 → 3…, gửi clip cảnh trước cho bot rồi mới tạo cảnh "
               "sau. Bot gửi lại <b>khung hình cuối</b> của cảnh trước → trong Flow chọn <i>Frames to Video</i>, đặt ảnh đó "
               "làm <b>khung bắt đầu</b>, rồi dán prompt cảnh sau."
               + ("\n🎙 Giọng: khi ghép, bot thay bằng <b>giọng cố định</b> của từng nhân vật (/giong)."
                  if voice_mode() == "fixed" else
                  "\n🎙 Âm thanh: giữ nguyên clip bạn gửi; chỉ câu nào lệch giọng <b>rõ</b> so với các câu khác của cùng "
                  "nhân vật trong video này mới được chỉnh nhẹ cao độ (/giong).")
)]
    item = DB_.get_content(cid)
    bible = content_bible(item)
    out += cast_photo_messages(cid, item, bible)
    for i, s in enumerate(m["shots"]):
        scene = item["script"]["scenes"][i]
        out.extend(scene_prompt_msg(cid, i, n, scene, s["prompt"], ingredients_note(bible, scene, i)))
    rows = [[("🎞 Đổi sang kéo dài – gửi 1 video", f"ce:{cid}")]] if clip_mode_choosable(item) else []
    if os.getenv("GEMINI_API_KEY"):
        usd = float((S.raw.get("budget") or {}).get("services", {}).get("gemini_video", {}).get("usd_per_clip", 0.19))
        rows.append([(f"🤖 Tạo tự động bằng Gemini (≈ ${usd * n:.2f})", f"pg:{cid}")])
    if rows:
        out.append(msg("Muốn đổi cách làm?", kb(*rows)))
    return out


def clip_mode_choosable(item: dict) -> bool:
    return bool(item) and is_dialogue(item["script"])


def extend_messages(cid: int) -> list[dict]:
    """Extend mode: the full prompt of part 1, then one short Extend prompt per following part, then ONE message to
    reply to with the finished video."""
    m = manual().load(cid)
    n = len(m["shots"])
    item = DB_.get_content(cid)
    bible = content_bible(item)
    scenes = item["script"]["scenes"]
    out = [msg(f"🎞 <b>#{cid} – 1 video kéo dài ({n} đoạn)</b>\n\n"
               "1) Mở <b>labs.google/flow</b> → tạo dự án mới, khung <b>dọc 9:16</b>.\n"
               "2) <b>Đoạn 1:</b> bấm <b>+ Ingredients</b>, thêm ảnh mẫu các nhân vật bên dưới, dán prompt "
               "<b>Đoạn 1</b> → tạo.\n"
               f"3) <b>Đoạn 2…{n}:</b> trên clip vừa tạo bấm <b>Extend</b> (kéo dài / Add to scene) và dán lần lượt "
               "prompt kéo dài tương ứng. Đoạn nào chưa ưng thì tạo lại đoạn đó trước khi kéo tiếp.\n"
               f"4) Xong cả {n} đoạn: tải về <b>toàn bộ video</b> (Scene builder → Download), rồi <b>Trả lời</b> tin "
               "<b>📥 Gửi video kéo dài</b> ở cuối bằng <b>đúng 1 video</b> đó.\n\n"
               "Bot <b>không cắt, không ghép</b> video này: giữ nguyên hình, giọng và tiếng; chỉ thêm nhạc nền đã duyệt, "
               "phụ đề, chữ ảnh bìa và câu kết."
               + VOICE_NOTE_KEEP)]
    out += cast_photo_messages(cid, item, bible)
    out.extend(scene_prompt_msg(cid, 0, n, scenes[0], m["shots"][0]["prompt"], ingredients_note(bible, scenes[0], 0),
                                label="Đoạn 1 (prompt đầy đủ)"))
    for i in range(1, n):
        out.extend(extend_msg(cid, i, n, item["script"], bible))
    out.append(msg(f"📥 <b>#{cid} · Gửi video kéo dài</b> — Trả lời tin này bằng <b>1 video hoàn chỉnh</b> (đủ {n} đoạn).",
                   kb([("🧩 Đổi sang clip độc lập", f"cm:{cid}")])))
    return out


def extend_msg(cid: int, i: int, n: int, script: dict, bible: dict) -> list[dict]:
    """Part i+1 (i ≥ 1) of an Extend video: the short prompt to paste after pressing Extend."""
    if i == 0:
        return []
    p = series.extend_prompt(bible, script["scenes"][i], i, n, script)
    return [msg(f"🔁 <b>#{cid} · Đoạn {i + 1}/{n} – kéo dài:</b> bấm <b>Extend</b> trên đoạn {i} rồi dán:"
                f"\n\n<code>{code_esc(p)}</code>")]


def ingredients_note(bible: dict, scene: dict, i: int = 0) -> str:
    names = [c["name"] for c in series.scene_cast(bible, scene)]
    note = f"\n🧩 Ingredients: {esc(', '.join(names))}" if names else ""
    if i > 0:
        note += f"\n🔗 Khung bắt đầu: khung cuối của cảnh {i} (bot gửi sau khi nhận clip cảnh {i})"
    return note


PROMPT_CHUNK = 3500


def split_text(text: str, limit: int = PROMPT_CHUNK) -> list[str]:
    """Split RAW text into pieces <= limit chars: at line breaks first, then at sentence ends, hard cut last."""
    pieces: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit:
            cut = max(line.rfind(". ", 0, limit), line.rfind("; ", 0, limit), line.rfind(", ", 0, limit))
            cut = cut + 1 if cut > limit // 3 else (line.rfind(" ", 0, limit) if line.rfind(" ", 0, limit) > 0 else limit)
            pieces.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        pieces.append(line)
    chunks, cur = [], ""
    for p in pieces:
        add = p if not cur else cur + "\n" + p
        if len(add) > limit and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = add
    if cur.strip():
        chunks.append(cur)
    return chunks or [""]


def code_esc(text: str) -> str:
    return html.escape(text, quote=False)  # quotes need no escaping inside <code> (and &quot; wastes 5 chars)


def scene_prompt_msg(cid: int, i: int, n: int, scene: dict, prompt: str, note: str = "", label: str = "") -> list[dict]:
    """One scene prompt; split into several messages (each its own complete <code> block) if it is long."""
    head = f"🎬 <b>#{cid} · {label}</b>" if label else f"🎬 <b>#{cid} · Cảnh {i + 1}/{n}</b>"
    parts = split_text(prompt)
    out = []
    for k, part in enumerate(parts):
        label = f" · phần {k + 1}/{len(parts)}" if len(parts) > 1 else ""
        top = (f"{head}{label} – {esc(scene['visual_vi'])}{note}" if k == 0 else f"{head}{label}")
        if k == 0 and len(parts) > 1:
            top += f"\n⚠️ Prompt dài: chép lần lượt {len(parts)} phần, dán nối tiếp vào cùng một ô prompt trong Flow."
        out.append(msg(f"{top}\n\n<code>{code_esc(part)}</code>"))
    out[-1]["payload"]["reply_markup"] = kb([("🛡 Flow báo vi phạm chính sách → viết lại", f"fp:{cid * 100 + i}")])
    return out


def fix_prompt(n: int) -> dict:
    """Owner says Flow refused scene i of video cid: rewrite that prompt to be policy-safe and resend it."""
    cid, i = divmod(n, 100)
    with LOCK:
        item = DB_.get_content(cid)
        m = manual().load(cid)
        if not item or not m or i >= len(m["shots"]) or item["status"] not in ("awaiting_clips", "failed"):
            return reply(msg(f"#{cid} không còn ở bước tạo clip."))
        est = float(((S.raw.get("budget") or {}).get("services", {}).get("claude", {})).get("est_fix_usd", 0.03))
        BUDGET.ensure("claude", est, "Viết lại prompt clip cho đúng chính sách Google.", resume={"action": "fixprompt", "id": n})
    out = series.safer_prompt(m["shots"][i]["prompt"])
    with LOCK:
        m = manual().load(cid)
        m["shots"][i]["prompt"] = out.prompt_en
        manual().save(cid, m)
    note = (ingredients_note(content_bible(item), item["script"]["scenes"][i], i)
            + f"\n🛡 <i>Đã viết lại an toàn hơn: {esc(out.changes_vi)}</i>")
    return reply(scene_prompt_msg(cid, i, len(m["shots"]), item["script"]["scenes"][i], out.prompt_en, note))


def approve_script(cid: int, message_id: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "script_review":
            return reply(clear_buttons(message_id), msg(f"Kịch bản #{cid} không còn chờ duyệt."))
        bible = content_bible(item)
        scenes = item["script"]["scenes"]
        shots = [{"scene": i + 1, "shot": 1, "seconds": CLIP_SEC,
                  "prompt": series.clip_prompt(bible, sc, i, len(scenes), item["script"]),
                  "file": None, "tg_message_id": None} for i, sc in enumerate(scenes)]
        manual().save(cid, {"content_id": cid, "day": dt.datetime.now(TZ).date().isoformat(),
                            "shots": shots, "unassigned": []})
        sc = item["script"]
        sc["approved_at"] = now_ts()
        dlg = is_dialogue(sc)
        DB_.update_content(cid, status="music_review" if dlg else "awaiting_clips", script=sc)
    saved = sync_drive()
    if dlg:  # next: approve the background music, then choose auto / manual clips
        a = sc.get("audio") or {}
        if not a.get("preview") or not (DATA / a["preview"]).is_file():
            try:
                prepare_music(cid)
            except Exception as e:
                log.warning("Không tạo được nhạc nghe thử #%s: %s", cid, e)
        return reply(clear_buttons(message_id),
                     msg(f"✅ Đã duyệt kịch bản #{cid}.{saved}\n🎵 <b>Bước 2/4 – duyệt nhạc nền</b> (một bài chạy liền "
                         "từ đầu đến hết video, nhỏ dưới giọng nhân vật). Nghe thử ở tin dưới."),
                     audio_message(cid) or msg("⚠️ Chưa tạo được nhạc nghe thử.",
                                               kb([("🎵 Đổi nhạc", f"am:{cid}")], [("⏭ Bỏ qua", f"ma:{cid}")])))
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


def approve_music(cid: int, mid: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "music_review":
            return reply(clear_buttons(mid), msg(f"#{cid} không ở bước duyệt nhạc."))
        review = bool(S.raw.get("video", {}).get("font_review", True))
        DB_.update_content(cid, status="font_review" if review else "awaiting_clips")
    a = (item["script"] or {}).get("audio") or {}
    name = esc(Path(a["music"]).stem) if a.get("music") else "chưa có nhạc"
    done = msg(f"✅ Đã duyệt nhạc nền #{cid}: <b>{name}</b>.")
    if review:
        return reply(clear_buttons(mid), done, font_review_msg(cid))
    return reply(clear_buttons(mid), done, clip_mode_msg(cid))


# ---------------------------------------------------------------- fonts (cover / subtitles / closing line)

FONT_KEY = "fonts"
ROLE_ARGS = {"dau": "cover", "bia": "cover", "phude": "subtitle", "pd": "subtitle", "ket": "ending", "cuoi": "ending"}


def current_fonts() -> dict:
    with LOCK:
        kv = DB_.kv_get(FONT_KEY) or {}
    base = (S.raw.get("video", {}) or {}).get("fonts") or {}
    return {r: kv.get(r) or base.get(r) or fonts.DEFAULTS[r] for r in fonts.ROLES}


def set_font(role: str, key: str) -> None:
    with LOCK:
        kv = DB_.kv_get(FONT_KEY) or {}
        kv[role] = key
        DB_.kv_set(FONT_KEY, kv)


def font_texts(cid: int | None) -> tuple[dict, Path | None]:
    """Sample texts (from the script when there is one) and a background (a character photo) for the preview."""
    if not cid:
        return dict(fonts.SAMPLE), None
    item = DB_.get_content(cid)
    sc = item["script"]
    first = next((ln["text"] for s in sc.get("scenes", []) for ln in s.get("lines") or []), fonts.SAMPLE["subtitle"])
    bg = None
    bible = content_bible(item)
    for c in series.scene_cast(bible, (sc.get("scenes") or [{}])[0]):
        bg = bg or CAST.image_path(c)
    return {"cover": thumb_text(sc), "subtitle": first, "ending": closing_line(sc)}, bg


def font_preview_file(cid: int | None) -> Path:
    texts, bg = font_texts(cid)
    d = DATA / "fonts_preview"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{cid or 0}_*.jpg"):
        old.unlink(missing_ok=True)
    return fonts.preview({"fonts": current_fonts()}, texts, bg, d / "work", d / f"{cid or 0}_{now_ts()}.jpg")


def font_caption(cid: int | None) -> str:
    cur = current_fonts()
    head = (f"🔤 <b>Bước 3/4 – #{cid}: duyệt chữ</b> (ảnh minh hoạ dựng đúng như video thật)\n" if cid
            else "🔤 <b>Font chữ đang dùng</b> (ảnh minh hoạ)\n")
    return head + "\n".join(f"• {fonts.ROLE_LABEL[r]}: <b>{esc(fonts.CATALOG[cur[r]]['label'])}</b>" for r in fonts.ROLES)


def font_review_msg(cid: int) -> dict:
    rows = [[("✅ Duyệt chữ", f"fa:{cid}")],
            [("🔤 Font đầu", f"ff:{cid * 10}"), ("🔤 Font phụ đề", f"ff:{cid * 10 + 1}"),
             ("🔤 Font kết", f"ff:{cid * 10 + 2}")]]
    try:
        return media("photo", font_preview_file(cid), font_caption(cid) + "\n\nBấm 🔤 để thử font khác (chọn xong "
                     "áp dụng cho các video sau).", kb(*rows))
    except Exception as e:
        log.warning("font preview #%s: %s", cid, e)
        return msg(font_caption(cid) + f"\n⚠️ Chưa tạo được ảnh minh hoạ ({esc(str(e)[:150])}).", kb(*rows))


def cycle_font(n: int, mid: int | None) -> dict:
    cid, r = divmod(n, 10)
    if r >= len(fonts.ROLES):
        return reply()
    role = fonts.ROLES[r]
    set_font(role, fonts.next_key({"fonts": current_fonts()}, role))
    if cid:
        with LOCK:
            item = DB_.get_content(cid)
        if not item or item["status"] != "font_review":
            return reply(clear_buttons(mid), msg(f"#{cid} không còn ở bước duyệt chữ (font mới vẫn được lưu)."))
        return reply(clear_buttons(mid), font_review_msg(cid))
    return reply(clear_buttons(mid), font_cmd(""))


def approve_fonts(cid: int, mid: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        if not item or item["status"] != "font_review":
            return reply(clear_buttons(mid), msg(f"#{cid} không ở bước duyệt chữ."))
        DB_.update_content(cid, status="awaiting_clips")
    return reply(clear_buttons(mid), msg(f"✅ Đã duyệt chữ cho #{cid}."), clip_mode_msg(cid))


def font_cmd(text: str) -> dict:
    """/font → preview + buttons; /font dau mali | /font phude bvp | /font ket prata → set directly."""
    args = text.split()[1:]
    if len(args) >= 2:
        role = ROLE_ARGS.get(deaccent(args[0]).replace(" ", ""))
        key = args[1].lower()
        if role and key in fonts.available():
            set_font(role, key)
        else:
            return reply(msg("Cú pháp: <code>/font dau|phude|ket TEN</code>. Tên font: "
                             + ", ".join(f"<code>{k}</code> ({esc(fonts.CATALOG[k]['label'])})" for k in fonts.available())))
    with LOCK:
        r = DB_.conn.execute("SELECT id FROM content WHERE script IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    cid = r["id"] if r else None
    rows = [[("🔤 Font đầu", "fg:0"), ("🔤 Font phụ đề", "fg:1"), ("🔤 Font kết", "fg:2")]]
    try:
        texts, bg = font_texts(cid)
        d = DATA / "fonts_preview"
        d.mkdir(parents=True, exist_ok=True)
        f = fonts.preview({"fonts": current_fonts()}, texts, bg, d / "work", d / f"cmd_{now_ts()}.jpg")
        return reply(media("photo", f, font_caption(None) + "\n\nBấm 🔤 để đổi; hoặc gõ <code>/font ket pacifico</code>. "
                           "Áp dụng cho các video ghép từ giờ.", kb(*rows)))
    except Exception as e:
        return reply(msg(font_caption(None) + f"\n⚠️ Chưa tạo được ảnh minh hoạ ({esc(str(e)[:150])}).", kb(*rows)))


def use_newest_music(cid: int, mid: int | None) -> dict:
    tracks = sorted(music_tracks(), key=lambda t: t.stat().st_mtime)
    with LOCK:
        item = DB_.get_content(cid)
    if not tracks or not item or item["status"] != "music_review":
        return reply(clear_buttons(mid))
    prepare_music(cid, rel(tracks[-1]))
    return reply(clear_buttons(mid), audio_message(cid))


def clip_mode_msg(cid: int) -> dict:
    n = len(manual().load(cid)["shots"])
    usd = float((S.raw.get("budget") or {}).get("services", {}).get("gemini_video", {}).get("usd_per_clip", 0.19))
    rows = []
    if os.getenv("GEMINI_API_KEY"):
        rows.append([(f"🤖 Tự động bằng Gemini (≈ ${usd * n:.2f})", f"pg:{cid}")])
    rows.append([("🎞 Tay – Flow kéo dài, gửi 1 video", f"ce:{cid}")])
    rows.append([("🧩 Tay – Flow clip độc lập, gửi từng clip", f"cm:{cid}")])
    note = "" if os.getenv("GEMINI_API_KEY") else "\n<i>(Tự động cần GEMINI_API_KEY trong .env.)</i>"
    return msg(f"🎬 <b>Bước 4/4 – #{cid}: tạo video {n} đoạn bằng cách nào?</b>\n"
               "• <b>Tự động:</b> hệ thống tạo clip bằng Gemini (trả phí, luôn hỏi duyệt ngân sách trước).\n"
               f"• <b>🎞 Kéo dài (miễn phí):</b> bot gửi prompt đoạn 1 + {n - 1} prompt kéo dài; bạn làm trên Flow thành "
               "1 video hoàn chỉnh và gửi lại đúng 1 video (bot không ghép).\n"
               f"• <b>🧩 Clip độc lập (miễn phí):</b> bot gửi {n} prompt riêng; bạn gửi lại {n} clip 8 giây, bot ghép."
               f"{note}", kb(*rows))


def choose_manual(cid: int, mid: int | None, mode: str = "independent") -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        m = manual().load(cid)
        if not item or not m or item["status"] != "awaiting_clips":
            return reply(clear_buttons(mid), msg(f"#{cid} không ở bước tạo clip."))
        if m.get("mode") != mode:  # switching method: clips received for the other method no longer apply
            for sh in m["shots"]:
                sh["file"], sh["tg_message_id"] = None, None
            m["unassigned"], m["full"] = [], None
        m["mode"] = mode
        manual().save(cid, m)
    return reply(clear_buttons(mid), prompt_messages(cid))


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
        if sc.get("drive_video"):
            parts.append(f"<p><b>Video:</b> <a href='{esc(sc['drive_video'])}'>{esc(sc['drive_video'])}</a></p>")
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


def upload_video_drive(cid: int, path: Path) -> str:
    """Put a freshly rendered video in Drive (TikTok Agent/Video); returns a short note (never raises)."""
    if DRIVE is None:
        return ""
    with LOCK:
        connected = DRIVE.connected()
        item = DB_.get_content(cid)
    if not connected:
        return "\n📁 Chưa lưu video lên Drive (/drive để kết nối)."
    title = re.sub(r'[\\/:*?"<>|]+', " ", (item["script"] or {}).get("title", ""))[:60].strip()
    name = f"#{cid} {title} {dt.datetime.now(TZ).strftime('%Y-%m-%d %H%M')}.mp4"
    try:
        link = DRIVE.upload_video(Path(path), name)
    except Exception as e:
        log.warning("Tải video lên Drive lỗi: %s", e)
        return f"\n⚠️ Chưa tải được video lên Drive: {esc(str(e)[:150])}"
    with LOCK:
        item = DB_.get_content(cid)
        sc = item["script"]
        sc["drive_video"] = link
        DB_.update_content(cid, script=sc)
    sync_drive()  # the script document gets the video link too
    return f"\n📁 Đã lưu video lên Drive (thư mục Video): {esc(link)}" if link else "\n📁 Đã lưu video lên Drive."


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


# ---------------------------------------------------------------- the 6 fixed characters (look + photo + voice)

OLD_CAST_KEY = "character_refs"
CORE_KEYS = ("ong", "ba", "bo", "me", "be_trai", "be_gai")
PENDING_SLOT = "pending_photo_slot"
IMG_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PENDING_PHOTO = "pending_photo"


def cast_items() -> list[dict]:
    return CAST.load()


def _migrate_cast() -> None:
    """Photos sent before the fixed cast existed: attach them to the matching character once."""
    with LOCK:
        old = DB_.kv_get(OLD_CAST_KEY) or {}
    if not old.get("items") or old.get("migrated"):
        return
    for it in old["items"]:
        c = CAST.find(it.get("name", "")) or CAST.find(it.get("role", ""))
        if c and not c.get("image") and it.get("look_en"):
            CAST.update(c["key"], look_en=it["look_en"], image=it.get("image", ""))
    with LOCK:
        DB_.kv_set(OLD_CAST_KEY, {**old, "migrated": True})


def _cast_index(key: str) -> int:
    return next((i for i, c in enumerate(cast_items()) if c["key"] == key), -1)


def card_text(c: dict) -> str:
    tts = c.get("tts") or {}
    return (f"🧑 <b>{esc(c['name'])}</b> – {esc(c['role'])} [NV:{esc(c['key'])}]\n\n"
            f"👀 <b>Ngoại hình:</b> <i>{esc(c['look_en'])}</i>\n"
            f"🗣 <b>Giọng (clip AI):</b> <i>{esc(c['voice_en'])}</i>\n"
            f"🎙 <b>Giọng ghép cố định:</b> {esc(VOICE_LABELS.get(tts.get('voice'), tts.get('voice', '')))} "
            f"· cao độ {esc(tts.get('pitch', '+0Hz'))} · tốc độ {esc(tts.get('rate', '+0%'))}\n"
            f"💬 <b>Cách nói:</b> {esc(c['speech_vi'])}")


def card_buttons(i: int, c: dict) -> dict:
    rows = [[("🔊 Nghe giọng", f"gp:{i}"), ("🎙 Đổi giọng", f"gv:{i}"), ("✏️ Nhập giọng", f"ge:{i}")],
            [("📷 Thay ảnh" if c.get("image") else "📷 Thêm ảnh", f"cp:{i}")]]
    if c.get("image"):
        rows[1].append(("🗑 Xoá ảnh mẫu", f"cx:{i}"))
    if c["key"] not in CORE_KEYS:
        rows.append([("❌ Xoá nhân vật này", f"cd:{i}")])
    return kb(*rows)


def card_msg(i: int, c: dict, bible: dict, extra: str = "") -> dict | list[dict]:
    img = CAST.image_path(c)
    if img:
        text = card_text(c) + extra
        if len(html.unescape(re.sub(r"<[^>]+>", "", text))) <= 1000:
            return media("photo", img, text, card_buttons(i, c))
        return [media("photo", img, f"🧑 <b>{esc(c['name'])}</b> – ảnh mẫu [NV:{esc(c['key'])}]"),
                msg(text, card_buttons(i, c))]
    tip = ("\n\n⚠️ <b>Chưa có ảnh mẫu</b> → clip dễ lệch mặt. Tạo ảnh: dán prompt dưới vào Google Flow (chế độ "
           f"<i>ảnh</i>) hoặc Gemini, lưu ảnh đẹp nhất, rồi gửi vào đây với chú thích <code>{esc(c['name'])}</code>:\n"
           f"<code>{esc(series.portrait_prompt(bible, c))}</code>")
    return msg(card_text(c) + extra + tip, card_buttons(i, c))


def receive_photo(body: dict) -> dict:
    if int(body.get("file_size") or 0) > 20 * 1024 * 1024:
        return reply(msg("⚠️ Ảnh lớn hơn 20 MB, bot không tải được. Hãy gửi ảnh nhẹ hơn."))
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(str(body.get("mime_type") or ""), ".jpg")
    d = DATA / "characters"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"nv_{now_ts()}_{secrets.token_hex(3)}{ext}"
    _tg_download(str(body["file_id"]), dst)
    caption = str(body.get("caption") or "").strip()[:300]
    tag = re.search(r"\[NV:([\w-]+)\]", str(body.get("reply_to_text") or ""))
    c = CAST.get(tag.group(1)) if tag else None
    c = c or CAST.find(caption)
    if not c:  # "📷 Thay ảnh" pressed in the last 15 minutes
        with LOCK:
            slot = DB_.kv_get(PENDING_SLOT) or {}
            if slot.get("key") and now_ts() - int(slot.get("ts", 0)) < 900:
                c = CAST.get(slot["key"])
                DB_.kv_set(PENDING_SLOT, {})
    if not c:
        with LOCK:
            DB_.kv_set(PENDING_PHOTO, {"path": rel(dst), "caption": caption, "ts": now_ts()})
        items = cast_items()
        rows = [[(items[k]["name"], f"cs:{k}") for k in range(j, min(j + 3, len(items)))] for j in range(0, len(items), 3)]
        new_name = _split_name(caption)[0]
        rows.append([(f"➕ Nhân vật mới: {new_name}" if new_name else "➕ Nhân vật mới", "cn:0")])
        return reply(msg("🧑‍🎨 Ảnh này là của nhân vật nào? Hoặc thêm thành <b>nhân vật mới</b>"
                         + ("" if new_name else " (gửi lại ảnh kèm chú thích <code>Tên - vai</code>, vd "
                            "<code>Cô Lan - cô giáo chủ nhiệm</code>)") + ".", kb(*rows)))
    return reply(msg(f"⏳ Đang đọc ảnh mẫu của <b>{esc(c['name'])}</b> (khoảng 30 giây)…"),
                 next_step={"action": "photo", "path": rel(dst), "caption": caption, "key": c["key"]})


def _split_name(caption: str) -> tuple[str, str]:
    """'Cô Lan - cô giáo chủ nhiệm' → ('Cô Lan', 'cô giáo chủ nhiệm')"""
    parts = re.split(r"\s*[-–:]\s*", (caption or "").strip(), maxsplit=1)
    name = parts[0].strip()[:40]
    role = parts[1].strip()[:200] if len(parts) > 1 else ""
    return name, role


def ask_photo_for(i: int, mid: int | None) -> dict:
    items = cast_items()
    if not 0 <= i < len(items):
        return reply()
    with LOCK:
        DB_.kv_set(PENDING_SLOT, {"key": items[i]["key"], "ts": now_ts()})
    return reply(msg(f"📷 Gửi ảnh mới cho <b>{esc(items[i]['name'])}</b> ngay bây giờ (trong 15 phút, không cần chú "
                     "thích). Bot sẽ thay ảnh mẫu và mô tả lại ngoại hình theo ảnh."))


def new_from_photo(mid: int | None) -> dict:
    with LOCK:
        pend = DB_.kv_get(PENDING_PHOTO) or {}
    name, _ = _split_name(pend.get("caption", ""))
    if not pend.get("path"):
        return reply(clear_buttons(mid), msg("Không còn ảnh chờ. Gửi lại ảnh kèm chú thích <code>Tên - vai</code>."))
    if not name:
        return reply(clear_buttons(mid), msg("Để thêm nhân vật mới, gửi lại ảnh kèm chú thích <code>Tên - vai</code>, "
                                             "vd <code>Cô Lan - cô giáo chủ nhiệm</code>."))
    with LOCK:
        DB_.kv_set(PENDING_PHOTO, {})
    return reply(clear_buttons(mid), msg(f"⏳ Đang tạo nhân vật mới <b>{esc(name)}</b> từ ảnh…"),
                 next_step={"action": "photo", "path": pend["path"], "caption": pend.get("caption", ""), "key": "new"})


def add_cast_cmd(text: str) -> dict:
    arg = text.partition(" ")[2].strip()
    name, role = _split_name(arg)
    if not name:
        return reply(msg("➕ <b>Thêm nhân vật mới</b>\n\n• Có ảnh: gửi ảnh vào đây, chú thích <code>Tên - vai</code>, vd "
                         "<code>Cô Lan - cô giáo chủ nhiệm, hiền</code> → bấm <b>➕ Nhân vật mới</b>.\n"
                         "• Chưa có ảnh: gõ <code>/themnv Cô Lan - cô giáo chủ nhiệm, hiền</code> → bot viết mô tả "
                         "ngoại hình + giọng, kèm prompt để bạn tạo ảnh rồi gửi lại."))
    if CAST.find(name) and norm_name(CAST.find(name)["name"]) == norm_name(name):
        return reply(msg(f"Đã có nhân vật tên {esc(name)}. Gõ /nhanvat để sửa."))
    return reply(msg(f"⏳ Đang tạo nhân vật <b>{esc(name)}</b>…"),
                 next_step={"action": "newcast", "name": name, "role": role})


def norm_name(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _new_cast_item(name: str, role: str, ch) -> dict:
    fb = (S.raw.get("voice") or {}).get("fallback") or {}
    vt = ch.voice_type if ch.voice_type in fb else "female"
    return {"name": name, "role": role or ch.role, "aliases": [], "look_en": ch.look_en, "voice_en": ch.voice_en,
            "speech_vi": "", "voice_type": vt, "tts": dict(fb.get(vt) or {})}


def create_cast(name: str, role: str) -> dict:
    est = float(((S.raw.get("budget") or {}).get("services", {}).get("claude", {})).get("est_fix_usd", 0.03))
    with LOCK:
        BUDGET.ensure("claude", est, "Viết mô tả cố định cho nhân vật mới.",
                      resume={"action": "newcast", "name": name, "role": role})
    b = fix_bible({})
    ch = series.invent_character(name, role, b.get("era") or "Việt Nam thập niên 1990", b.get("style_en", ""))
    c = CAST.add(_new_cast_item(name, role, ch))
    return reply(msg(f"✅ Đã thêm <b>{esc(c['name'])}</b>. Kịch bản từ giờ có thể dùng nhân vật này (kế hoạch mới "
                     "/kehoach sẽ đưa vào lịch)."), card_msg(_cast_index(c["key"]), c, b))


def delete_cast_char(i: int, mid: int | None, confirm: bool) -> dict:
    items = cast_items()
    if not 0 <= i < len(items) or items[i]["key"] in CORE_KEYS:
        return reply(clear_buttons(mid), msg("Không xoá được (6 nhân vật chính luôn giữ lại; chỉ sửa được)."))
    c = items[i]
    if not confirm:
        return reply(msg(f"Xoá hẳn nhân vật <b>{esc(c['name'])}</b>? Kịch bản đã viết vẫn giữ nguyên.",
                         kb([("❌ Xoá", f"cy:{i}"), ("Giữ lại", "nop:0")])))
    CAST.remove(c["key"])
    return reply(clear_buttons(mid), msg(f"🗑 Đã xoá nhân vật {esc(c['name'])}."))


def choose_photo_slot(i: int, mid: int | None) -> dict:
    with LOCK:
        pend = DB_.kv_get(PENDING_PHOTO) or {}
    items = cast_items()
    if not pend.get("path") or not 0 <= i < len(items):
        return reply(clear_buttons(mid), msg("Không còn ảnh chờ gán. Gửi lại ảnh kèm chú thích tên nhân vật."))
    with LOCK:
        DB_.kv_set(PENDING_PHOTO, {})
    return reply(clear_buttons(mid), msg(f"⏳ Đang đọc ảnh mẫu của <b>{esc(items[i]['name'])}</b>…"),
                 next_step={"action": "photo", "path": pend["path"], "caption": pend.get("caption", ""),
                            "key": items[i]["key"]})


def describe_photo(path: str, caption: str, key: str = "") -> dict:
    p = (DATA / str(path)).resolve()
    if not p.is_relative_to(DATA) or not p.is_file():
        return reply(msg("⚠️ Không tìm thấy ảnh vừa gửi, hãy gửi lại."))
    new = key == "new"
    c = None if new else (CAST.get(key) or CAST.find(caption))
    if not c and not new:
        return reply(msg("⚠️ Không rõ ảnh của nhân vật nào. Gửi lại ảnh kèm chú thích tên, vd <code>Ông</code>."))
    est = float(((S.raw.get("budget") or {}).get("services", {}).get("claude", {})).get("est_image_usd", 0.05))
    with LOCK:
        BUDGET.ensure("claude", est, "Đọc ảnh nhân vật để viết mô tả cố định.",
                      resume={"action": "photo", "path": path, "caption": caption, "key": key if new else c["key"]})
    era = fix_bible({}).get("era") or "Việt Nam thập niên 1990"
    if new:
        name, role = _split_name(caption)
        ch = series.describe_character(p.read_bytes(), IMG_TYPES.get(p.suffix.lower(), "image/jpeg"), caption, era,
                                       who=f"{name} – {role or 'nhân vật mới'}")
        c = CAST.add({**_new_cast_item(name, role, ch), "image": rel(p)})
        return reply(card_msg(_cast_index(c["key"]), c, fix_bible({}),
                              "\n\n✅ Đã thêm nhân vật mới. Kịch bản từ giờ có thể dùng nhân vật này."))
    ch = series.describe_character(p.read_bytes(), IMG_TYPES.get(p.suffix.lower(), "image/jpeg"), caption, era,
                                   who=f"{c['name']} – {c['role']}")
    had = bool(c.get("image"))
    c = CAST.update(c["key"], look_en=ch.look_en, image=rel(p))
    note = ("\n\n✅ Đã lưu ảnh mẫu" + (" (thay ảnh cũ)" if had else "") + ". Từ giờ <b>mọi kịch bản và prompt clip</b> "
            "dùng đúng mô tả này; ảnh được gửi kèm khi tạo clip tự động và gửi để bạn thêm vào <i>Ingredients</i> khi "
            "tạo tay trên Flow. Sai chỗ nào: <b>Trả lời</b> tin này, gõ vd <code>Ngoại hình: ...</code>")
    return reply(card_msg(_cast_index(c["key"]), c, fix_bible({}), note))


def edit_cast(key: str, text: str) -> dict:
    c = CAST.get(key)
    if not c:
        return reply(msg("Không tìm thấy nhân vật này (có thể đã đổi trong data/nhan_vat.yaml). Gõ /nhanvat."))
    fields = parse_edit(text)
    if not fields and parse_voice(text, c["tts"]):
        return apply_voice(key, text)
    if not fields:
        return reply(msg("Để sửa, trả lời thẻ nhân vật với từng dòng dạng:\n<code>Tên: Bé Na\nVai: em út, hồn nhiên\n"
                         "Ngoại hình: little girl, short bob hair, red shirt…\nGiọng: sweet little girl voice…\n"
                         "Cách nói: xưng 'con', gọi anh là 'anh Tí'</code>\n"
                         "(Ngoại hình và Giọng nên viết tiếng Anh. Hoặc gửi ảnh mẫu để bot tự mô tả.)"))
    c = CAST.update(key, **fields)
    return reply(msg("✅ Đã cập nhật. Áp dụng cho kịch bản và prompt từ giờ."), card_msg(_cast_index(key), c, fix_bible({})))


def delete_cast_image(i: int, mid: int | None) -> dict:
    items = cast_items()
    if not 0 <= i < len(items):
        return reply(clear_buttons(mid))
    CAST.update(items[i]["key"], image="")
    return reply(clear_buttons(mid), msg(f"🗑 Đã bỏ ảnh mẫu của {esc(items[i]['name'])} (mô tả ngoại hình giữ nguyên). "
                                         "Gửi ảnh mới kèm chú thích tên để thay."))


# ---------------------------------------------------------------- fixed voices (/giong)

def voice_mode() -> str:
    with LOCK:
        kv = DB_.kv_get("dialogue_voice") or {}
    saved = kv.get("mode") if kv.get("v") == 3 else None  # choices saved before 20/11 are ignored
    m = saved or (S.raw.get("voice") or {}).get("dialogue_voice") or "match"
    return m if m in ("fixed", "clip", "match") else "match"


def voice_presets() -> list[dict]:
    fb = (S.raw.get("voice") or {}).get("fallback") or {}
    out = [dict(v) for v in fb.values() if isinstance(v, dict) and v.get("voice")]
    return out or [{"voice": "vi-VN-HoaiMyNeural", "pitch": "+0Hz", "rate": "+0%"}]


def voice_cmd() -> dict:
    mode = voice_mode()
    lines = ["🎙 <b>Giọng thoại khi ghép video</b>", "",
             {"match": "✅ <b>Khớp giọng nhân vật</b> (đang dùng, khuyên dùng): giữ giọng thật trong clip, nhưng bot đo "
                       "cao độ + âm lượng từng câu và kéo về <b>giọng chuẩn</b> của mỗi nhân vật — cùng một người thì "
                       "cùng một giọng ở mọi clip (tốc độ nói giữ nguyên).",
              "clip": "✅ <b>Giữ nguyên âm thanh clip</b> (đang dùng): y như từng clip Flow/Gemini, không chỉnh — giọng "
                      "một nhân vật có thể khác nhau giữa các clip.",
              "fixed": "✅ <b>Giọng cố định (máy)</b> (đang dùng): bot THAY tiếng trong clip bằng giọng máy riêng của "
                       "từng nhân vật (giống hệt nhau, nhưng kém tự nhiên)."}[mode], "",
             "Giọng từng nhân vật (bấm để nghe; dưới bản nghe có nút ✏️ để <b>nhập cao độ/tốc độ</b>). Nhập nhanh: "
             "<code>/giong Ông pitch -10 rate -5</code> hoặc <code>/giong Bé Na cao hơn</code>:"]
    lines += [f"• <b>{esc(c['name'])}</b>: {esc(VOICE_LABELS.get(c['tts']['voice'], c['tts']['voice']))} "
              f"{esc(c['tts']['pitch'])} {esc(c['tts']['rate'])}" for c in cast_items()]
    refs = voice_refs()
    if refs:
        lines += ["", "🎯 <b>Giọng chuẩn đã học</b> (từ video đầu tiên): " +
                  ", ".join(f"{esc(k)} {v['f0']:.0f} Hz" for k, v in refs.items())]
    rows = [[(("🔘 " if mode == "match" else "") + "Khớp giọng nhân vật", "gm:3")],
            [(("🔘 " if mode == "clip" else "") + "Giữ nguyên clip", "gm:2"),
             (("🔘 " if mode == "fixed" else "") + "Giọng cố định (máy)", "gm:1")]]
    if refs:
        rows.append([("♻️ Học lại giọng chuẩn ở video tới", "gr:0")])
    items = cast_items()
    rows += [[(f"🔊 {items[k]['name']}", f"gp:{k}") for k in range(j, min(j + 3, len(items)))]
             for j in range(0, len(items), 3)]
    return reply(msg("\n".join(lines), kb(*rows)))


VOICE_EDIT = "pending_voice_edit"
VOICE_HELP = ("Gõ yêu cầu, ví dụ:\n"
              "• <code>pitch -10 rate +5</code> (hoặc <code>cao độ -10 tốc độ 5</code>, hoặc chỉ <code>-10 5</code>)\n"
              "• <code>trầm hơn</code>, <code>cao hơn nhiều</code>, <code>nhanh hơn</code>, <code>chậm hơn</code> "
              "(mỗi lần 5, \"nhiều\" = 10)\n"
              "• <code>giọng nam</code> / <code>giọng nữ</code> (đổi giọng gốc)\n"
              "Cao độ (pitch) tính bằng Hz: âm = trầm, dương = cao (−100…+100). Tốc độ (rate) tính bằng %: "
              "âm = chậm, dương = nhanh (−50…+100).")


def ask_voice_edit(i: int) -> dict:
    items = cast_items()
    if not 0 <= i < len(items):
        return reply()
    c = items[i]
    with LOCK:
        DB_.kv_set(VOICE_EDIT, {"key": c["key"], "ts": now_ts()})
    t = c["tts"]
    return reply(msg(f"✏️ <b>Chỉnh giọng {esc(c['name'])}</b> [GIONG:{esc(c['key'])}]\n"
                     f"Hiện tại: {esc(VOICE_LABELS.get(t['voice'], t['voice']))} · cao độ <b>{esc(t['pitch'])}</b> · "
                     f"tốc độ <b>{esc(t['rate'])}</b>\n\n{VOICE_HELP}\n\nGõ ngay tin tiếp theo (trong 10 phút), hoặc "
                     "<b>Trả lời</b> tin này."))


def apply_voice(key: str, text: str) -> dict:
    c = CAST.get(key)
    if not c:
        return reply(msg("Không tìm thấy nhân vật này. Gõ /giong."))
    got = parse_voice(text, c["tts"])
    if not got:
        return reply(msg(f"🤔 Chưa hiểu yêu cầu giọng cho {esc(c['name'])}.\n\n{VOICE_HELP}"))
    new, notes = got
    CAST.update(key, tts=new)
    head = msg(f"✅ <b>{esc(c['name'])}</b>: " + (esc("; ".join(notes)) if notes else "giữ nguyên")
               + ". Áp dụng cho bản nghe thử và video ghép từ giờ. Nghe thử bên dưới; muốn chỉnh tiếp cứ gõ tiếp "
               "(vd <code>trầm hơn</code>).")
    with LOCK:
        DB_.kv_set(VOICE_EDIT, {"key": key, "ts": now_ts()})  # keep fine-tuning without pressing again
    return reply(head, *voice_sample(_cast_index(key))["messages"])


def voice_cmd_text(text: str) -> dict:
    """/giong → menu; /giong Ông pitch -10 rate +5 → set directly."""
    arg = text.partition(" ")[2].strip()
    if not arg:
        return voice_cmd()
    m = re.search(r"(?:pitch|rate|cao|tốc|toc|trầm|tram|nhanh|chậm|cham|thấp|thap|giọng|giong|[+-]?\d)", arg, re.I)
    name = arg[:m.start()].strip(" :,-") if m else arg
    c = CAST.find(name) if name else None
    if not c:
        return reply(msg("Ghi tên nhân vật trước yêu cầu, ví dụ <code>/giong Ông pitch -10 rate -5</code> hoặc "
                         "<code>/giong Bé Na cao hơn</code>."))
    if not m:
        return ask_voice_edit(_cast_index(c["key"]))
    return apply_voice(c["key"], arg[m.start():])


def pending_voice_text(text: str) -> dict | None:
    with LOCK:
        pv = DB_.kv_get(VOICE_EDIT) or {}
    if pv.get("key") and now_ts() - int(pv.get("ts", 0)) < 600 and not text.startswith("/"):
        c = CAST.get(pv["key"])
        if c and parse_voice(text, c["tts"]):
            return apply_voice(pv["key"], text)
    return None


VOICE_REFS = "voice_refs"


def voice_refs() -> dict:
    with LOCK:
        return (DB_.kv_get(VOICE_REFS) or {}).get("refs") or {}


def reset_voice_refs(mid: int | None) -> dict:
    with LOCK:
        DB_.kv_set(VOICE_REFS, {})
    return reply(clear_buttons(mid), msg("♻️ Đã xoá giọng chuẩn. Video ghép tiếp theo sẽ học lại giọng chuẩn cho từng "
                                         "nhân vật (từ chính video đó)."))


def set_voice_mode(n: int, mid: int | None) -> dict:
    with LOCK:
        DB_.kv_set("dialogue_voice", {"mode": {1: "fixed", 2: "clip", 3: "match"}.get(n, "match"), "v": 3})
    return reply(clear_buttons(mid), *voice_cmd()["messages"])


def voice_sample(i: int) -> dict:
    items = cast_items()
    if not 0 <= i < len(items):
        return reply()
    c = items[i]
    d = DATA / "audio" / "samples"
    d.mkdir(parents=True, exist_ok=True)
    tts = c["tts"]
    f = d / f"{c['key']}_{hashlib.sha1(json.dumps(tts, sort_keys=True).encode()).hexdigest()[:8]}.mp3"
    if not f.is_file():
        text = SAMPLE_LINES.get(c.get("voice_type"), "Ngày xưa ấy mà, cả nhà mình vui lắm.")
        dialogue.tts_line(text, tts, f)
    return reply(media("audio", f, f"🔊 Giọng cố định của <b>{esc(c['name'])}</b> [GIONG:{esc(c['key'])}]\n"
                                   f"{esc(VOICE_LABELS.get(tts['voice'], tts['voice']))} · cao độ {esc(tts['pitch'])} · "
                                   f"tốc độ {esc(tts['rate'])}\nMuốn chỉnh: <b>Trả lời</b> tin này, vd "
                                   "<code>pitch -10 rate +5</code> hoặc <code>trầm hơn</code>",
                       kb([("✏️ Nhập giọng", f"ge:{i}"), ("🎙 Giọng gợi ý khác", f"gv:{i}")])))


SAMPLE_LINES = {"old_male": "Ngày xưa ấy mà, ông cũng thả diều ngoài đê suốt buổi chiều đấy cháu ạ.",
                "old_female": "Cháu ăn từ từ thôi, bà để phần cháu cả rồi.",
                "male": "Con trai bố hôm nay giỏi thế, mai bố dẫn đi chợ phiên nhé.",
                "female": "Con rửa tay đi rồi vào ăn cơm, cả nhà đợi đấy.",
                "boy": "Ơ hay quá! Na ơi ra đây xem anh làm cái này này!",
                "girl": "Thật á? Bà ơi, sao ngày xưa bà lại biết làm cái đấy ạ?"}


def cycle_voice(i: int, mid: int | None) -> dict:
    items = cast_items()
    if not 0 <= i < len(items):
        return reply()
    presets = voice_presets()
    cur = items[i]["tts"]
    idx = next((k for k, p in enumerate(presets) if p == cur), -1)
    CAST.update(items[i]["key"], tts=presets[(idx + 1) % len(presets)])
    return reply(clear_buttons(mid), *voice_sample(i)["messages"])


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
    with LOCK:
        w = DB_.conn.execute("SELECT id FROM content WHERE status='music_review' ORDER BY id DESC LIMIT 1").fetchone()
    if w:  # a video is waiting for its music: let the owner hear it with the new track right away
        return reply(msg(f"🎵 Đã thêm nhạc nền <b>{esc(dst.name)}</b>. Video #{w['id']} đang chờ duyệt nhạc:",
                         kb([(f"🎧 Nghe thử #{w['id']} với bài này", f"mu:{w['id']}")])))
    return reply(msg(f"🎵 Đã thêm nhạc nền <b>{esc(dst.name)}</b>."), music_list())


def music_list() -> dict:
    tracks = music_tracks()
    if not tracks:
        return msg("🎵 <b>Thư viện nhạc nền đang trống</b> (thư mục data/music trên VPS chưa có file nào).\n\n"
                   "Nếu bạn đã gửi file mà bot <b>không trả lời \"🎵 Đã thêm nhạc nền\"</b>: hãy chạy lệnh cập nhật VPS "
                   "rồi gửi lại file.\n\nGửi file nhạc (mp3/m4a, dưới 20 MB) vào đây để thêm. "
                   "Chỉ dùng nhạc <b>không bản quyền</b> (YouTube Audio Library, Pixabay Music…). "
                   "Mỗi video sẽ tự xoay vòng một bài, nhạc tự nhỏ lại khi nhân vật nói.")
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

    with LOCK:
        m = src.load(cid)
        extend = m.get("mode") == "extend" or (not m.get("mode") and "video kéo dài" in ref)
        if extend:  # ONE finished Extend video: nothing to collect or join
            m["mode"], m["full"] = "extend", str(dst)
            src.save(cid, m)
            DB_.update_content(cid, status="rendering")
    if extend:
        return reply(msg(f"📥 Đã nhận video kéo dài cho #{cid}. ⏳ Đang thêm nhạc nền, phụ đề, ảnh bìa, câu kết "
                         "(không cắt, không ghép)…"), next_step={"action": "render", "id": cid})

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
            out.append(media("photo", frame, f"🔗 <b>Khung cuối cảnh {scene}</b> → lưu ảnh này, trong Flow chọn "
                                             f"<i>Frames to Video</i> và đặt làm <b>khung bắt đầu</b> cho cảnh "
                                             f"{scene + 1} (cùng ảnh nhân vật ở Ingredients), rồi dán prompt cảnh "
                                             f"{scene + 1}. Hai cảnh sẽ nối liền."))
    return reply(out)


def reset_clips(cid: int, message_id: int | None) -> dict:
    with LOCK:
        item = DB_.get_content(cid)
        m = manual().load(cid)
        if not item or not m or item["status"] not in ("video_review", "failed", "awaiting_clips"):
            return reply(clear_buttons(message_id), msg(f"Không thay clip được cho #{cid} lúc này."))
        for s in m["shots"]:
            s["file"], s["tg_message_id"] = None, None
        m["unassigned"], m["full"] = [], None
        manual().save(cid, m)
        DB_.update_content(cid, status="awaiting_clips")
    return reply(clear_buttons(message_id), msg(f"🔁 Gửi lại clip cho #{cid}:"), prompt_messages(cid))


# ====================================================================== render / generate

def _render_script(item: dict, prompts: list[str] | None = None) -> dict:
    sc = item["script"]
    if is_dialogue(sc):
        bible = content_bible(item)
        normalize_speakers(sc, bible)

        def refs(scene: dict) -> list[str]:  # characters' reference photos, sent with the prompt (max 3)
            return [str(p) for p in (CAST.image_path(c) for c in series.scene_cast(bible, scene)) if p][:3]

        return {"title": sc["title"], "visual_bible": {}, "thumb_text": thumb_text(sc), "closing_line": closing_line(sc),
                "scenes": [{"lines": s.get("lines") or [], "visual_query": s["visual_query"], "keep_audio": True,
                            **({"prompt_full": prompts[i], "ref_images": refs(s)} if prompts else {})}
                           for i, s in enumerate(sc["scenes"])]}
    scenes = [{"narration": s["narration"], "visual_query": s["visual_query"],
               "shots": [prompts[i]] if prompts else [s["action_en"]]} for i, s in enumerate(sc["scenes"])]
    return {"title": sc["title"], "scenes": scenes, "visual_bible": {}, "thumb_text": thumb_text(sc),
            "closing_line": closing_line(sc), "voice": (sc.get("audio") or {}).get("voice")}


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
    """The track approved with the script; if none (or it was deleted) a library track — every video has music.
    Returns "" only when the library is empty (the render then warns on Telegram)."""
    a = (item["script"] or {}).get("audio") or {}
    m = a.get("music") or ""
    if m and (DATA / m).is_file():
        return str(DATA / m)
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
            if m and m.get("mode") == "extend":
                if not m.get("full") or not Path(m["full"]).is_file():
                    raise RuntimeError("chưa nhận được video kéo dài")
                script["single_clip"] = m["full"]
        src.start_run(cid)
        run: dict = {}
        try:
            cfg = {**S.raw, "video": {**S.raw.get("video", {}), "background_music": approved_music(item),
                                      "fonts": current_fonts()}}
            if is_dialogue(item["script"]):
                src.strict = paid  # paid clips must not silently turn into stock footage
                path = dialogue.render_dialogue(script, workdir, cfg, S.pixabay_api_key or "", src,
                                                content_bible(item), voice_mode=voice_mode(),
                                                voice_dir=DATA / "audio" / str(cid) / "voices", voice_refs=voice_refs())
                vm = workdir / "voice_match.json"
                if vm.is_file():  # remember each character's reference voice for the next videos
                    with LOCK:
                        DB_.kv_set(VOICE_REFS, {"refs": json.loads(vm.read_text(encoding="utf-8"))["refs"],
                                                "ts": now_ts()})
            else:
                path = video.render(script, workdir, cfg, S.pixabay_api_key or "", ai=src,
                                    narration=approved_narration(item))
        except AIAuthError as e:
            src.finish_run()
            with LOCK:
                DB_.update_content(cid, status="awaiting_clips", error=str(e)[:2000])
            return reply(msg(
                f"⚠️ <b>Không tạo được clip tự động cho #{cid}: Gemini API key không hợp lệ.</b> Chưa tốn đồng nào.\n\n"
                "Cách sửa:\n1) Mở <b>aistudio.google.com/apikey</b> → <b>Create API key</b> → chép khoá (bắt đầu bằng "
                "<code>AIza</code>).\n2) Termius: <code>cd /opt/tiktok-agent &amp;&amp; nano .env</code> → dòng "
                "<code>GEMINI_API_KEY=</code> dán khoá mới (không dấu cách, không ngoặc kép) → lưu.\n"
                "3) Chạy lại lệnh cập nhật VPS (để hệ thống nhận khoá mới), rồi bấm <b>Thử tạo tự động lại</b>.\n\n"
                "Hoặc tạo tay miễn phí trên Google Flow với các prompt bên dưới.",
                kb([("🔁 Thử tạo tự động lại", f"pg:{cid}")])), prompt_messages(cid))
        except AIPolicyError as e:
            run = src.finish_run() or {}
            with LOCK:
                BUDGET.record("gemini_video", float(run.get("est_cost_usd", 0)), f"#{cid} (bị chặn chính sách)")
                m = manual().load(cid)
                tries = int(m.get("policy_retries", 0))
            if tries < 1 and m and e.scene < len(m["shots"]):  # rewrite the refused scene once and try again
                fixed = series.safer_prompt(m["shots"][e.scene]["prompt"], str(e))
                with LOCK:
                    m["shots"][e.scene]["prompt"] = fixed.prompt_en
                    m["policy_retries"] = tries + 1
                    manual().save(cid, m)
                    DB_.update_content(cid, status="rendering")
                return reply(msg(f"🛡 Google từ chối prompt cảnh {e.scene + 1} của #{cid} vì chính sách nội dung. "
                                 f"Đã viết lại an toàn hơn ({esc(fixed.changes_vi)}) và đang tạo lại…"),
                             next_step={"action": "generate", "id": cid})
            with LOCK:
                DB_.update_content(cid, status="awaiting_clips", error=str(e)[:2000])
            return reply(msg(f"⚠️ Google vẫn từ chối cảnh {e.scene + 1} của #{cid} vì chính sách nội dung. Bạn tạo tay "
                             "trên Flow (bấm 🛡 dưới cảnh để viết lại prompt), hoặc bấm Viết bản khác cho kịch bản.",
                             kb([("🔁 Thử tạo tự động lại", f"pg:{cid}")])), prompt_messages(cid))
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
        drive_note = upload_video_drive(cid, path)
        cap = (f"🎞 <b>Video #{cid}</b> · {esc(item['script']['title'])} · {dur:.0f}s{drive_note}\n\n"
               f"{esc(item['caption'])}")
        out = [media("video", path, cap, kb([("✅ Duyệt & đăng TikTok", f"va:{cid}")],
                                            [("🔁 Thay clip", f"vc:{cid}"), ("❌ Bỏ", f"vx:{cid}")]))]
        thumb = Path(path).parent / "thumbnail.jpg"
        if thumb.is_file():
            out.append(media("photo", thumb, f"🖼 <b>Ảnh bìa #{cid}</b>: “{esc(thumb_text(item['script']))}”\n"
                                             f"🔚 Câu kết ở clip cuối: “{esc(closing_line(item['script']))}”\n"
                                             "Đăng thẳng: TikTok lấy đúng khung này làm ảnh bìa. Đăng vào hộp nháp: "
                                             "khi đăng trong app, chọn ảnh bìa ở giây đầu (có chữ)."))
        if fallbacks:
            out.append(msg("⚠️ Một số cảnh thiếu clip AI nên dùng video stock: "
                           + ", ".join(str(f["scene"]) for f in fallbacks)))
        rep_file = Path(path).parent / "mix_report.json"
        warns = json.loads(rep_file.read_text(encoding="utf-8")).get("warnings", []) if rep_file.is_file() else []
        if warns:
            out.append(msg(f"🎵 <b>Kiểm tra âm thanh #{cid}:</b>\n" + "\n".join(f"• {esc(w)}" for w in warns)
                           + "\n\nVẫn duyệt được; muốn sửa: đổi bài nhạc to hơn (/nhac) hoặc tăng "
                           "<code>music_volume</code> trong config.yaml, rồi bấm 🔁 Thay clip để ghép lại."))
        return reply(out)
    except Exception as e:
        log.exception("render #%s failed", cid)
        if paid:  # automatic generation failed: go back to "waiting for clips" so the owner can retry or do it by hand
            with LOCK:
                DB_.update_content(cid, status="awaiting_clips", error=str(e)[:2000])
            return reply(msg(f"⚠️ Tạo clip tự động cho #{cid} lỗi: {esc(str(e)[:300])}\n\n"
                             "Bấm thử lại, hoặc tạo tay trên Google Flow với các prompt bên dưới.",
                             kb([("🔁 Thử tạo tự động lại", f"pg:{cid}")])), prompt_messages(cid))
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
        m = manual().load(cid)
        if m and m.get("policy_retries"):
            m["policy_retries"] = 0
            manual().save(cid, m)
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
                pid = p.direct_post(Path(item["video_path"]), item["caption"], tk["privacy_level"],
                                    cover_ms=int(float(S.raw.get("video", {}).get("thumbnail_at", 0.5)) * 1000))
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
                                   "('script_review','music_review','font_review','awaiting_clips','rendering','video_review') ORDER BY id").fetchall()
        rem = remaining_days()
    lines = ["📊 <b>Tình trạng</b>", ""]
    lines.append(f"Kế hoạch: #KH{lp['id']} ({lp['status']}), còn {rem} ý tưởng chưa viết" if lp else "Kế hoạch: chưa có")
    today = dt.datetime.now(TZ).date()
    lines.append(f"Mỗi video: {scenes_count()} cảnh (≈ {scenes_count() * CLIP_SEC}s, /socanh để đổi)")
    lines.append(f"Tần suất: {freq_label(freq_on(today)['days'])} · kịch bản tới: "
                 + ", ".join(d.strftime("%d/%m") for d in next_due_days(today, 3)) + " (/tansuat để đổi)")
    names = {"script_review": "chờ duyệt kịch bản", "music_review": "chờ duyệt nhạc nền", "font_review": "chờ duyệt chữ",
             "awaiting_clips": "chờ clip",
             "rendering": "đang ghép",
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
    names = ", ".join(c["name"] for c in cast)
    missing = [c["name"] for c in cast if not CAST.image_path(c)]
    tip = (f"\n\n💡 Chưa có ảnh mẫu cho: {esc(', '.join(missing))} — gõ /nhanvat để thêm (giúp clip giống mặt hơn)."
           if missing else "")
    return reply(msg(f"⏳ Đang lập kế hoạch 30 ngày với gia đình cố định ({esc(names)}) (2–5 phút)…{tip}"),
                 next_step={"action": "plan"})


def characters() -> dict:
    cast = cast_items()
    bible = fix_bible(((active_plan() or latest_plan() or {}).get("data") or {}).get("bible") or {})
    err = CAST.broken()
    head = ["👨‍👩‍👧‍👦 <b>6 nhân vật cố định của kênh</b> — dùng nguyên văn trong mọi kịch bản và prompt clip.", "",
            "Cách chỉnh (chỉ cần làm 1 lần, áp dụng mãi):",
            "📷 <b>Ảnh mẫu:</b> gửi ảnh vào đây, chú thích là tên nhân vật (vd <code>Ông</code>, "
            "<code>Bé Na</code>) — hoặc <b>Trả lời</b> thẻ nhân vật bằng ảnh.",
            "✏️ <b>Sửa mô tả:</b> <b>Trả lời</b> thẻ nhân vật, gõ <code>Tên: …</code> / <code>Ngoại hình: …</code> / "
            "<code>Giọng: …</code> / <code>Cách nói: …</code>",
            "🔄 <b>Thay ảnh:</b> bấm <b>📷 Thay ảnh</b> dưới thẻ rồi gửi ảnh mới.",
            "➕ <b>Thêm nhân vật:</b> gửi ảnh, chú thích <code>Tên - vai</code> → bấm <b>➕ Nhân vật mới</b>; hoặc gõ "
            "<code>/themnv Cô Lan - cô giáo chủ nhiệm</code>. Nhân vật thêm có nút xoá.",
            "🎙 <b>Giọng:</b> /giong để nghe và chọn giọng từng người.",
            "🗂 Hoặc sửa file <code>/opt/tiktok-agent/data/nhan_vat.yaml</code> bằng nano."]
    if err:
        head += ["", f"⚠️ File nhan_vat.yaml đang lỗi cú pháp nên bot dùng nhân vật mặc định: <i>{esc(err)}</i>"]
    return reply(msg("\n".join(head)), *[card_msg(i, c, bible) for i, c in enumerate(cast)])


def resend_prompts(text: str = "") -> dict:
    """/clip → every prompt of the video waiting for clips; /clip 2 → only scene 2 (also /clip 2 3)."""
    with LOCK:
        r = DB_.conn.execute("SELECT id FROM content WHERE status='awaiting_clips' ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return reply(msg("Không có video nào đang chờ clip."))
    want = [int(x) for x in re.findall(r"\d+", text.partition(" ")[2])]
    if not want:
        return reply(prompt_messages(r["id"]))
    cid = r["id"]
    mode = clip_mode(cid)
    if mode is None:
        return reply(manual_choice_msg(cid))
    item, m = DB_.get_content(cid), manual().load(cid)
    bible, n = content_bible(item), len(m["shots"])
    sc = item["script"]["scenes"]
    if mode == "extend":
        out = [extend_msg(cid, k - 1, n, item["script"], bible) if k > 1 else
               scene_prompt_msg(cid, 0, n, sc[0], m["shots"][0]["prompt"], ingredients_note(bible, sc[0], 0),
                                label="Đoạn 1 (prompt đầy đủ)") for k in want if 1 <= k <= n]
    else:
        out = [scene_prompt_msg(cid, k - 1, n, sc[k - 1], m["shots"][k - 1]["prompt"],
                                ingredients_note(bible, sc[k - 1], k - 1)) for k in want if 1 <= k <= n]
    return reply(*out) if out else reply(msg(f"Video #{cid} chỉ có {'đoạn' if mode == 'extend' else 'cảnh'} 1–{n}."))


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


try:
    _migrate_cast()
except Exception as _e:  # never block startup over old photos
    log.warning("Không chuyển được ảnh nhân vật cũ: %s", _e)


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
              "/trangthai": lambda: reply(msg(status_text())), "/ngansach": lambda: reply(msg(budget_text())),
             "/nhanvat": characters, "/tiktok": tiktok_links, "/nhac": lambda: reply(music_list()),
             "/phongcach": lambda: style_cmd(str(body.get("text", ""))),
             "/drive": drive_cmd, "/giong": lambda: voice_cmd_text(str(body.get("text", ""))),
             "/themnv": lambda: add_cast_cmd(str(body.get("text", ""))),
             "/clip": lambda: resend_prompts(str(body.get("text", ""))),
             "/tansuat": lambda: frequency_cmd(str(body.get("text", ""))),
             "/socanh": lambda: scenes_cmd(str(body.get("text", ""))),
             "/doithu": lambda: channels_cmd(str(body.get("text", "")), "competitors"),
             "/kenhmau": lambda: channels_cmd(str(body.get("text", "")), "reference"),
             "/font": lambda: font_cmd(str(body.get("text", "")))}
    return _safe(table.get(cmd, lambda: reply(msg(HELP))))


@app.post("/tg/button")
def tg_button(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    action, _, arg = str(body.get("data", "")).partition(":")
    mid = body.get("message_id")
    if action == "bo":  # approve a budget request with the amount the owner picked
        try:
            rid, cents = (int(x) for x in arg.split("_"))
        except ValueError:
            return reply()
        if not 0 < cents <= 100_000:
            return reply()
        return _safe(_budget_decision, rid, True, mid, cents / 100)
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
        "ma": lambda: approve_music(n, mid),
        "fa": lambda: approve_fonts(n, mid),
        "ff": lambda: cycle_font(n, mid),
        "fg": lambda: cycle_font(n, mid),
        "mu": lambda: use_newest_music(n, mid),
        "cm": lambda: choose_manual(n, mid, "independent"),
        "ce": lambda: choose_manual(n, mid, "extend"),
        "va": lambda: publish(n, mid),
        "vc": lambda: reset_clips(n, mid),
        "vx": lambda: _set_status(n, "video_review", "video_rejected", mid, f"❌ Đã bỏ video #{n}."),
        "rr": lambda: _retry_render(n, mid),
        "ba": lambda: _budget_decision(n, True, mid),
        "bx": lambda: _budget_decision(n, False, mid),
        "mx": lambda: delete_music(n, mid),
        "cs": lambda: choose_photo_slot(n, mid),
        "cp": lambda: ask_photo_for(n, mid),
        "cn": lambda: new_from_photo(mid),
        "cd": lambda: delete_cast_char(n, mid, False),
        "cy": lambda: delete_cast_char(n, mid, True),
        "nop": lambda: reply(clear_buttons(mid)),
        "gm": lambda: set_voice_mode(n, mid),
        "gp": lambda: voice_sample(n),
        "gv": lambda: cycle_voice(n, mid),
        "ge": lambda: ask_voice_edit(n),
        "gr": lambda: reset_voice_refs(mid),
        "tf": lambda: set_frequency(n, mid),
        "sn": lambda: set_scenes(n, mid),
        "pn": lambda: replan_for_channels(mid),
        "am": lambda: reply(msg("⏳ Đang đổi nhạc…"), next_step={"action": "audio", "id": n, "what": "music"}),
        "av": lambda: reply(msg("⏳ Đang đổi giọng đọc…"), next_step={"action": "audio", "id": n, "what": "voice"}),
        "dc": lambda: drive_check(mid),
        "fp": lambda: reply(msg("⏳ Đang viết lại prompt cho đúng chính sách Google…"),
                            next_step={"action": "fixprompt", "id": n}),
        "ds": lambda: reply(clear_buttons(mid), msg(("📤 Đồng bộ Google Drive:" + sync_drive()) if DRIVE else "")),
        "dx": lambda: (DRIVE.disconnect() if DRIVE else None) or reply(clear_buttons(mid),
                                                                        msg("🔌 Đã ngắt kết nối Google Drive.")),
        "pk": lambda: reply(clear_buttons(mid), msg("⏳ Đang quét đối thủ và lập kế hoạch 30 ngày (2–5 phút)…"),
                            next_step={"action": "plan"}),
        "cx": lambda: delete_cast_image(n, mid),
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


def _budget_decision(rid: int, approve: bool, mid: int | None, amount: float | None = None) -> dict:
    with LOCK:
        req = BUDGET.decide(rid, approve, amount)
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
    if m := re.search(r"\[GIONG:([\w-]+)\]", ref):
        return _safe(apply_voice, m.group(1), text)
    if m := re.search(r"\[NV:([\w-]+)\]", ref):
        return _safe(edit_cast, m.group(1), text)
    if not ref and (pv := _safe(pending_voice_text, text)) and pv.get("messages"):
        return pv
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


@app.post("/tg/other")
def tg_other(body: dict = BODY, x_worker_token: str = TOKEN) -> dict:
    _auth(x_worker_token)
    if (stop := _tg_guard(body)) is not None:
        return stop
    what = esc(" ".join(x for x in (str(body.get("kind") or ""), str(body.get("file_name") or ""),
                                     str(body.get("mime_type") or "")) if x))
    return reply(msg(f"🤔 Bot chưa dùng được file này ({what}).\n\n"
                     "• Nhạc nền: gửi file <b>mp3 / m4a</b> (dưới 20 MB)\n"
                     "• Clip cảnh: gửi <b>video mp4</b>, trả lời đúng tin \"Cảnh N\"\n"
                     "• Nhân vật: gửi <b>ảnh</b> kèm chú thích tên – vai"))


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
    if action == "plan_channels":
        return _safe(create_plan, str(body.get("feedback") or "")[:2000] or None)
    if action == "plan_redo":
        p = get_plan(int(n))
        return _safe(create_plan, body.get("feedback") or "Làm một kế hoạch khác hẳn.", p["data"] if p else None)
    if action == "script":
        return _safe(next_script)
    if action == "rewrite":
        return _safe(rewrite_script, int(n), body.get("feedback"))
    if action == "fixprompt":
        return _safe(fix_prompt, int(n))
    if action == "audio":
        return _safe(change_audio, int(n), body.get("what") or "music")
    if action == "photo":
        return _safe(describe_photo, body.get("path") or "", body.get("caption") or "", body.get("key") or "")
    if action == "paid":
        return _safe(request_paid, int(n), None)
    if action == "newcast":
        return _safe(create_cast, str(body.get("name") or "")[:40], str(body.get("role") or "")[:200])
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
