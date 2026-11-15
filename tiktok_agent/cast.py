"""The channel's fixed family: 6 characters (Ông, Bà, Bố, Mẹ, bé trai, bé gái) with a fixed look, voice and way of
speaking, used word for word in every plan, script and clip prompt — that is what keeps faces and voices the same
from clip to clip, for automatic (Gemini) and hand-made (Google Flow) clips alike.

Source of truth: `data/nhan_vat.yaml` (outside git, survives updates). It is created on first run from the `cast:`
defaults in config.yaml. The owner changes it by:
  • sending a photo on Telegram (caption = the role, e.g. "Ông") → the bot describes the look from the photo and
    keeps the photo as the reference image ("Ingredients" in Flow, reference image for Gemini);
  • replying to a character card on Telegram with "Tên: …", "Ngoại hình: …", "Giọng: …" lines;
  • buttons on /giong to pick each character's fixed voice;
  • or editing the file with nano.
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

FILE_NAME = "nhan_vat.yaml"
KEYS = ["ong", "ba", "bo", "me", "be_trai", "be_gai"]
FIELDS = ["key", "name", "role", "aliases", "look_en", "voice_en", "speech_vi", "voice_type", "tts", "image"]

# Words that point to a role when they start a photo caption (accents matter: "bà" ≠ "ba")
ROLE_ALIASES = {
    "ong": ["ông", "ông nội", "ông ngoại", "ong", "ong noi", "ong ngoai", "grandpa", "grandfather"],
    "ba": ["bà", "bà nội", "bà ngoại", "ba noi", "ba ngoai", "grandma", "grandmother"],
    "bo": ["bố", "cha", "bo", "father", "dad"],
    "me": ["mẹ", "má", "u", "me", "mother", "mom"],
    "be_trai": ["bé trai", "con trai", "cậu bé", "thằng bé", "cu", "be trai", "boy"],
    "be_gai": ["bé gái", "con gái", "cô bé", "be gai", "con gai", "girl"],
}

HEADER = """# Nhân vật cố định của kênh — dùng NGUYÊN VĂN trong mọi kịch bản và prompt clip (giữ mặt, áo, giọng nhất quán).
# Sửa qua Telegram: gửi ảnh kèm chú thích vai (vd "Ông"), trả lời thẻ nhân vật trong /nhanvat, đổi giọng trong /giong.
# Hoặc sửa trực tiếp file này (nano) rồi gửi /nhanvat để kiểm tra. Không cần chạy lại lệnh cài.
#   name      : tên gọi trong truyện (phụ đề, thoại)        role     : vai trò, tính cách (tiếng Việt)
#   look_en   : ngoại hình CỐ ĐỊNH, tiếng Anh (khuôn mặt, tóc, áo quần + màu). Trẻ em: KHÔNG ghi số tuổi.
#   voice_en  : giọng nói CỐ ĐỊNH, tiếng Anh (để Flow/Gemini lồng tiếng giống nhau giữa các clip)
#   speech_vi : cách xưng hô, câu cửa miệng (Claude dùng khi viết thoại)
#   tts       : giọng cố định khi ghép video (edge-tts): voice vi-VN-HoaiMyNeural (nữ) / vi-VN-NamMinhNeural (nam),
#               pitch (+/-Hz: cao/trầm), rate (+/-%: nhanh/chậm)
#   image     : ảnh mẫu (đường dẫn trong thư mục data/), tự điền khi bạn gửi ảnh qua Telegram
"""


def norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(s or "")).lower().split())


def deaccent(s: str) -> str:
    s = norm(s).replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


class Cast:
    def __init__(self, data_dir: Path, defaults: list[dict] | None):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / FILE_NAME
        self.defaults = [self._clean(d, i) for i, d in enumerate(defaults or [])]
        self._lock = threading.RLock()

    # ---------- load / save ----------
    @staticmethod
    def _clean(c: dict, i: int) -> dict:
        c = dict(c or {})
        key = str(c.get("key") or (KEYS[i] if i < len(KEYS) else f"nv{i + 1}"))
        tts = c.get("tts") or {}
        return {
            "key": key,
            "name": str(c.get("name") or key).strip(),
            "role": " ".join(str(c.get("role") or "").split()),
            "aliases": [str(a) for a in (c.get("aliases") or [])],
            "look_en": " ".join(str(c.get("look_en") or "").split()),
            "voice_en": " ".join(str(c.get("voice_en") or "").split()),
            "speech_vi": " ".join(str(c.get("speech_vi") or "").split()),
            "voice_type": str(c.get("voice_type") or "female"),
            "tts": {"voice": str(tts.get("voice") or "vi-VN-HoaiMyNeural"), "pitch": str(tts.get("pitch") or "+0Hz"),
                    "rate": str(tts.get("rate") or "+0%")},
            "image": str(c.get("image") or ""),
        }

    def load(self) -> list[dict]:
        with self._lock:
            if not self.path.is_file():
                if not self.defaults:
                    return []
                self.save(self.defaults)
                return [dict(d) for d in self.defaults]
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:  # owner broke the file with nano: keep working on the defaults
                log.warning("%s lỗi cú pháp (%s) → dùng nhân vật mặc định", self.path, e)
                return [dict(d) for d in self.defaults]
            items = raw.get("characters") if isinstance(raw, dict) else raw
            out = [self._clean(c, i) for i, c in enumerate(items or []) if isinstance(c, dict)]
            return out or [dict(d) for d in self.defaults]

    def broken(self) -> str:
        """Syntax error message if the owner's file can't be read, else ''."""
        if not self.path.is_file():
            return ""
        try:
            yaml.safe_load(self.path.read_text(encoding="utf-8"))
            return ""
        except yaml.YAMLError as e:
            return str(e)[:300]

    def save(self, items: list[dict]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = yaml.safe_dump({"characters": [{k: c.get(k) for k in FIELDS} for c in items]},
                                  allow_unicode=True, sort_keys=False, width=110)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(HEADER + "\n" + body, encoding="utf-8")
            tmp.replace(self.path)

    # ---------- lookup ----------
    def get(self, key: str) -> dict | None:
        return next((c for c in self.load() if c["key"] == key), None)

    def find(self, text: str) -> dict | None:
        """Character a caption / speaker name refers to: exact name, role alias at the start, or accent-free match."""
        t = norm(text)
        if not t:
            return None
        items = self.load()
        head = re.split(r"\s*[-–:,(]\s*", t, maxsplit=1)[0]
        for c in items:  # exact name
            if head == norm(c["name"]) or t == norm(c["name"]):
                return c
        cands: list[tuple[int, dict]] = []
        for c in items:
            words = [norm(c["name"])] + [norm(a) for a in c.get("aliases") or []] + ROLE_ALIASES.get(c["key"], [])
            for w in words:
                if w and (t == w or t.startswith(w + " ") or head == w):
                    cands.append((len(w), c))
        if not cands:  # accent-free fallback, multi-letter names only ("be na" → "Bé Na")
            dt, dh = deaccent(t), deaccent(head)
            for c in items:
                for w in [c["name"]] + list(c.get("aliases") or []):
                    dw = deaccent(w)
                    if len(dw) > 3 and (dh == dw or dt.startswith(dw + " ") or dt == dw):
                        cands.append((len(dw), c))
        return max(cands, key=lambda x: x[0])[1] if cands else None

    def update(self, key: str, **fields) -> dict | None:
        with self._lock:
            items = self.load()
            for i, c in enumerate(items):
                if c["key"] == key:
                    merged = {**c, **{k: v for k, v in fields.items() if k in FIELDS and k != "key"}}
                    items[i] = self._clean(merged, i)
                    self.save(items)
                    return items[i]
        return None

    def add(self, item: dict) -> dict:
        """Add a new character (key nv7, nv8, …) at the end of the cast."""
        with self._lock:
            items = self.load()
            n = max([int(c["key"][2:]) for c in items if re.fullmatch(r"nv\d+", c["key"])] + [len(KEYS)]) + 1
            new = self._clean({**item, "key": f"nv{n}"}, len(items))
            items.append(new)
            self.save(items)
            return new

    def remove(self, key: str) -> dict | None:
        with self._lock:
            items = self.load()
            gone = next((c for c in items if c["key"] == key), None)
            if gone:
                self.save([c for c in items if c["key"] != key])
            return gone

    # ---------- for prompts ----------
    def by_name(self) -> dict[str, dict]:
        return {c["name"]: c for c in self.load()}

    def image_path(self, c: dict) -> Path | None:
        if not c.get("image"):
            return None
        p = (self.data_dir / c["image"]).resolve()
        return p if p.is_relative_to(self.data_dir.resolve()) and p.is_file() else None


def bible_characters(items: list[dict]) -> list[dict]:
    """What goes into a series bible (and from there into every script and clip prompt)."""
    return [{"key": c["key"], "name": c["name"], "role": c["role"], "look_en": c["look_en"],
             "voice_en": c["voice_en"], "speech_vi": c["speech_vi"], "voice_type": c["voice_type"],
             "tts": c["tts"], "image": c["image"]} for c in items]


EDIT_FIELDS = {"tên": "name", "ten": "name", "vai": "role", "ngoại hình": "look_en", "ngoai hinh": "look_en",
               "look": "look_en", "giọng": "voice_en", "giong": "voice_en", "voice": "voice_en",
               "cách nói": "speech_vi", "cach noi": "speech_vi", "xưng hô": "speech_vi", "xung ho": "speech_vi"}


def parse_edit(text: str) -> dict:
    """'Tên: Bé Na\\nGiọng: sweet little girl voice' → {'name': 'Bé Na', 'voice_en': '...'}"""
    out = {}
    for line in str(text or "").splitlines():
        k, sep, v = line.partition(":")
        if sep and v.strip() and norm(k) in EDIT_FIELDS:
            out[EDIT_FIELDS[norm(k)]] = v.strip()[:600]
    return out
