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
FIELDS = ["key", "name", "role", "aliases", "look_en", "voice_en", "speech_vi", "voice_type", "tts", "image", "modern"]
MODERN_FIELDS = ("role", "look_en", "speech_vi", "image")  # the present-day version used by topic 2 (same name/voice)

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
#   modern    : phiên bản HIỆN ĐẠI cho chủ đề 2 Gia đình (role, look_en, speech_vi, image) — chỉ ghi phần bạn đã sửa;
#               phần còn lại lấy từ config.yaml (topics → giadinh → cast). Cùng tên và giọng với bản gốc.
"""


def norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(s or "")).lower().split())


def deaccent(s: str) -> str:
    s = norm(s).replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


VOICES = {"vi-VN-HoaiMyNeural": "Hoài My (nữ)", "vi-VN-NamMinhNeural": "Nam Minh (nam)"}
PITCH_RANGE, RATE_RANGE = (-100, 100), (-50, 100)


def _num(v) -> int:
    m = re.search(r"[+-]?\d+", str(v if v is not None else ""))
    return int(m.group()) if m else 0


def norm_pitch(v) -> str:
    """'5', '+5hz', '-10 Hz', 0 → '+5Hz' (edge-tts needs a sign and Hz), clamped to ±100 Hz."""
    return f"{max(PITCH_RANGE[0], min(PITCH_RANGE[1], _num(v))):+d}Hz"


def norm_rate(v) -> str:
    """'0%', '5', '-10 %' → '+0%' (edge-tts needs a sign and %), clamped to -50…+100 %."""
    return f"{max(RATE_RANGE[0], min(RATE_RANGE[1], _num(v))):+d}%"


def norm_voice(v) -> str:
    v = str(v or "")
    return v if re.fullmatch(r"[a-z]{2,}-[A-Z]{2,}-\w+Neural", v) else "vi-VN-HoaiMyNeural"


_PITCH = r"(?:pitch|cao\s*độ|cao\s*do|độ\s*cao|do\s*cao)"
_RATE = r"(?:rate|speed|tốc\s*độ|toc\s*do|tốc|toc)"
STEP = 5


def parse_voice(text: str, cur: dict) -> tuple[dict, list[str]] | None:
    """Owner's voice request → new tts dict + what changed (Vietnamese), or None if nothing understood.

    Absolute:  'pitch -10 rate +5', 'cao độ: 20Hz, tốc độ -5%', or two bare numbers '-10 +5' (pitch, rate)
    Relative:  'trầm hơn', 'cao hơn nữa', 'nhanh hơn', 'chậm hơn nhiều' (step 5, 'nhiều' = 10)
    Voice:     'giọng nam' / 'giọng nữ' / 'Nam Minh' / 'Hoài My'
    """
    t = norm(text)
    pitch, rate, voice = _num(cur.get("pitch")), _num(cur.get("rate")), cur.get("voice") or "vi-VN-HoaiMyNeural"
    seen = []
    m = re.search(_PITCH + r"\s*[:=]?\s*([+-]?\s*\d+)", t)
    if m:
        pitch = _num(m.group(1).replace(" ", "")); seen.append("pitch")
    m = re.search(_RATE + r"\s*[:=]?\s*([+-]?\s*\d+)", t)
    if m:
        rate = _num(m.group(1).replace(" ", "")); seen.append("rate")
    if not seen:
        nums = re.findall(r"(?<![\w])[+-]?\d+(?:\s*(?:hz|%))?", t)
        if len(nums) == 2:
            pitch, rate = _num(nums[0]), _num(nums[1]); seen += ["pitch", "rate"]
        elif len(nums) == 1 and ("hz" in nums[0]):
            pitch = _num(nums[0]); seen.append("pitch")
        elif len(nums) == 1 and "%" in nums[0]:
            rate = _num(nums[0]); seen.append("rate")
    big = 2 if re.search(r"\b(?:nhiều|nhieu|hẳn|rõ|lắm|lam)\b", t) else 1
    for words, what, sign in ((("trầm hơn", "thấp hơn", "tram hon", "thap hon", "sâu hơn", "già hơn"), "pitch", -1),
                              (("cao hơn", "thanh hơn", "trong hơn", "cao hon", "trẻ hơn", "non hơn"), "pitch", 1),
                              (("nhanh hơn", "nhanh hon", "lẹ hơn"), "rate", 1),
                              (("chậm hơn", "cham hon", "từ tốn hơn", "chậm rãi hơn"), "rate", -1)):
        if what not in seen and any(w in t for w in words):
            if what == "pitch":
                pitch += sign * STEP * big
            else:
                rate += sign * STEP * big
            seen.append(what)
    if re.search(r"giọng\s*nam|nam\s*minh|giong\s*nam|\bnam\b", t):
        voice = "vi-VN-NamMinhNeural"; seen.append("voice")
    elif re.search(r"giọng\s*nữ|hoài\s*my|hoai\s*my|giong\s*nu|\bnữ\b", t):
        voice = "vi-VN-HoaiMyNeural"; seen.append("voice")
    if not seen:
        return None
    new = {"voice": voice, "pitch": norm_pitch(pitch), "rate": norm_rate(rate)}
    notes = []
    if new["voice"] != cur.get("voice"):
        notes.append(f"giọng gốc {VOICES.get(new['voice'], new['voice'])}")
    if new["pitch"] != norm_pitch(cur.get("pitch")):
        notes.append(f"cao độ {norm_pitch(cur.get('pitch'))} → {new['pitch']}")
    if new["rate"] != norm_rate(cur.get("rate")):
        notes.append(f"tốc độ {norm_rate(cur.get('rate'))} → {new['rate']}")
    if _num(pitch) != _num(new["pitch"]) or _num(rate) != _num(new["rate"]):
        notes.append(f"(giới hạn: cao độ {PITCH_RANGE[0]}…+{PITCH_RANGE[1]}Hz, tốc độ {RATE_RANGE[0]}…+{RATE_RANGE[1]}%)")
    return new, notes


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
            "tts": {"voice": norm_voice(tts.get("voice")), "pitch": norm_pitch(tts.get("pitch")),
                    "rate": norm_rate(tts.get("rate"))},
            "image": str(c.get("image") or ""),
            "modern": {k: " ".join(str(v).split()) for k, v in (c.get("modern") or {}).items()
                       if k in MODERN_FIELDS and str(v or "").strip()},
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
