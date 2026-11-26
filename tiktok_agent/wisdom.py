"""Topic 3 – "Những lời răn dạy": no characters talking — ONE background image, a calm reading voice, subtitles that
follow the voice, light music under it.

The owner approves, in order: the text → the background image (made by the system; or their own photo, or a
description to make another one) → fonts + subtitle look (preview drawn on that very image; or a description) →
the reading voice with the music (or a description such as "trầm hơn, chậm hơn"). Then the video is built here:
the image with a very slow zoom, the voice, subtitles timed word by word, the cover text and the closing line.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from . import audio, fonts
from .content import _parse
from .video import FPS, H, W, _duration, _run, _srt_time, final_mix, make_outro

log = logging.getLogger(__name__)

LEAD_SEC = 1.2   # the cover text shows alone first, then the voice starts
TAIL_SEC = 1.5   # a breath after the last word before the ending


# ---------------------------------------------------------------- background image

def image_prompt(script: dict, style_en: str, extra: str = "") -> str:
    base = (extra or script.get("image_prompt_en") or "").strip().rstrip(".")
    return (f"{base}. {style_en.strip().rstrip('.')}. Vertical 9:16 composition, calm and uncluttered, the lower third "
            "quiet and darker so subtitles stay readable. Wholesome, everyone fully clothed. No text, no letters, no "
            "captions, no logos, no watermark.")


def gemini_image(prompt: str, out: Path, model: str) -> Path:
    """One 9:16 image from the Gemini API. "gemini-*-image" models work with a normal AI Studio key (Developer
    API); "imagen-*" models only on Vertex AI. Raises on any failure (the caller offers the manual way)."""
    from google import genai  # reads GEMINI_API_KEY from the environment
    from google.genai import types
    client = genai.Client()
    if model.startswith("imagen"):
        res = client.models.generate_images(model=model, prompt=prompt,
                                            config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16"))
        imgs = getattr(res, "generated_images", None) or []
        if not imgs or not getattr(imgs[0].image, "image_bytes", None):
            raise RuntimeError("Gemini không trả về ảnh (có thể prompt bị chặn)")
        out.write_bytes(imgs[0].image.image_bytes)
        return out
    try:
        cfg = types.GenerateContentConfig(response_modalities=["IMAGE"],
                                          image_config=types.ImageConfig(aspect_ratio="9:16"))
    except Exception:  # older SDK without ImageConfig: any ratio, cropped to 9:16 afterwards
        cfg = types.GenerateContentConfig(response_modalities=["IMAGE"])
    res = client.models.generate_content(model=model, contents=prompt, config=cfg)
    for cand in getattr(res, "candidates", None) or []:
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            data = getattr(getattr(part, "inline_data", None), "data", None)
            if data:
                out.write_bytes(data)
                return out
    why = getattr(getattr(res, "prompt_feedback", None), "block_reason", None) or \
        next((getattr(c, "finish_reason", None) for c in getattr(res, "candidates", None) or []), None)
    raise RuntimeError(f"Gemini không trả về ảnh{f' ({why})' if why else ''}")


def pixabay_image(api_key: str, query: str, out: Path, skip: int = 0) -> Path:
    """A vertical photo from Pixabay (free, safe search). `skip` picks a different result on each retry."""
    r = httpx.get("https://pixabay.com/api/", params={"key": api_key, "q": query[:90], "image_type": "photo",
                                                      "orientation": "vertical", "safesearch": "true", "per_page": 20},
                  timeout=30)
    r.raise_for_status()
    hits = r.json().get("hits") or []
    if not hits:
        raise RuntimeError(f"Pixabay không có ảnh cho '{query}'")
    hit = hits[skip % len(hits)]
    data = httpx.get(hit.get("largeImageURL") or hit["webformatURL"], timeout=60).content
    out.write_bytes(data)
    return out


def normalize_image(src: Path, out: Path) -> Path:
    """Any photo → a 1080x1920 JPEG (cover-cropped), so previews and the video use exactly the same frame."""
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf",
          f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1", "-frames:v", "1", "-q:v", "2",
          str(out)])
    return out


# ---------------------------------------------------------------- owner descriptions → style / voice (Claude)

FontKey = Literal["bvp_xb", "bvp", "mali", "itim", "pacifico", "lobster", "prata", "taviraj", "dejavu"]


class StylePick(BaseModel):
    cover: FontKey = Field(description="Font chữ ảnh bìa (đầu video)")
    subtitle: FontKey = Field(description="Font phụ đề")
    ending: FontKey = Field(description="Font câu kết (cuối video)")
    color: str = Field(description="Màu chữ phụ đề, mã hex RGB 6 ký tự không có #, vd FFFFFF, FFE8A3")
    size: float = Field(description="Cỡ phụ đề so với mặc định, 0.8–1.6")
    box: bool = Field(description="Có nền mờ sau chữ phụ đề không")
    position: Literal["bottom", "lower", "middle"] = Field(description="Vị trí phụ đề: phía dưới / cao hơn (1/3 dưới) / giữa màn hình")
    note_vi: str = Field(description="Đã chọn gì, 1 câu tiếng Việt")


def style_from_text(desc: str, current: dict) -> StylePick:
    catalog = {k: v["label"] for k, v in fonts.CATALOG.items()}
    system = ("Bạn chọn font chữ và kiểu phụ đề cho video TikTok tiếng Việt theo mô tả của chủ kênh. Chỉ chọn trong "
              "danh mục font có sẵn (đều đủ dấu tiếng Việt). Phần chủ kênh không nhắc tới thì giữ như hiện tại.")
    user = (f"Danh mục font:\n{json.dumps(catalog, ensure_ascii=False)}\nKiểu hiện tại:\n"
            f"{json.dumps(current, ensure_ascii=False)}\nMô tả của chủ kênh:\n<desc>\n{desc}\n</desc>")
    out = _parse(system, user, StylePick, effort="low", max_tokens=1500)
    out.size = max(0.8, min(1.6, out.size))
    out.color = "".join(ch for ch in out.color.upper() if ch in "0123456789ABCDEF")[:6].ljust(6, "F")
    return out


class VoicePick(BaseModel):
    voice: Literal["vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"] = Field(description="Giọng gốc: nam / nữ")
    pitch: int = Field(description="Cao độ, Hz, -60…+60 (âm = trầm)")
    rate: int = Field(description="Tốc độ, %, -40…+30 (âm = chậm)")
    note_vi: str = Field(description="Đã chỉnh gì, 1 câu tiếng Việt")


def voice_from_text(desc: str, current: dict) -> VoicePick:
    system = ("Bạn chỉnh giọng đọc TTS tiếng Việt (edge-tts) theo mô tả của chủ kênh: chọn giọng gốc nam/nữ, cao độ "
              "(Hz) và tốc độ (%). Giọng đọc lời răn dạy nên trầm ấm, chậm rãi. Phần không nhắc tới thì giữ như hiện tại.")
    user = f"Giọng hiện tại:\n{json.dumps(current, ensure_ascii=False)}\nMô tả:\n<desc>\n{desc}\n</desc>"
    out = _parse(system, user, VoicePick, effort="low", max_tokens=800)
    out.pitch, out.rate = max(-60, min(60, out.pitch)), max(-40, min(30, out.rate))
    return out


class MusicPick(BaseModel):
    track: str = Field(description="Tên bài trong thư viện hợp nhất với mô tả (đúng tên trong danh sách), hoặc '' để "
                                   "giữ bài hiện tại")
    level_db: float = Field(description="Mức nhạc so với mặc định, dB, -12…+12 (dương = to hơn); giữ như hiện tại nếu "
                                        "chủ kênh không nhắc tới độ to nhỏ")
    none: bool = Field(default=False, description="True nếu chủ kênh muốn bỏ nhạc nền")
    note_vi: str = Field(description="Đã chọn gì, 1 câu tiếng Việt")


def music_from_text(desc: str, tracks: list[str], current: dict, mood: str = "") -> MusicPick:
    system = ("Bạn chọn nhạc nền cho video TikTok theo mô tả của chủ kênh, CHỈ trong thư viện nhạc có sẵn (đoán không khí "
              "bài qua tên file), và chỉnh mức to nhỏ của nhạc so với giọng. Không bịa tên bài.")
    user = (f"Thư viện: {json.dumps(tracks, ensure_ascii=False)}\nHiện tại: {json.dumps(current, ensure_ascii=False)}\n"
            f"Không khí kịch bản gợi ý: {mood}\nMô tả của chủ kênh:\n<desc>\n{desc}\n</desc>")
    out = _parse(system, user, MusicPick, effort="low", max_tokens=800)
    out.level_db = max(-12.0, min(12.0, float(out.level_db)))
    if out.track not in tracks:
        out.track = ""
    return out


# ---------------------------------------------------------------- narration + render

def narrations(script: dict) -> list[str]:
    return [s["narration"] for s in script.get("scenes") or [] if s.get("narration", "").strip()]


def voice_track(script: dict, voice: dict, workdir: Path, model: str = "gemini-2.5-flash-preview-tts") -> dict:
    """The reading voice → meta with "path". engine "gemini": a described voice (Gemini TTS, one call per line so
    every subtitle is timed to its own sentence); otherwise edge-tts with word timings."""
    if voice.get("engine") == "gemini":
        return gemini_voice_track(narrations(script), voice, workdir, model)
    meta = audio.narration_track(narrations(script), voice.get("voice", "vi-VN-NamMinhNeural"),
                                 voice.get("rate", "-12%"), workdir, voice.get("pitch", "+0Hz"))
    meta["path"] = str(workdir / meta["file"])
    return meta


# ---------------------------------------------------------------- described voices (Gemini TTS)

# Gemini TTS prebuilt voices and their character (from Google's voice list); all speak Vietnamese
GEMINI_VOICES = {
    "Charon": "nam, trầm, rõ ràng, như người kể chuyện", "Algenib": "nam, trầm khàn, từng trải",
    "Gacrux": "nam/nữ trung niên, chín chắn, điềm đạm", "Iapetus": "nam, rõ ràng, ấm", "Orus": "nam, chắc, vững vàng",
    "Alnilam": "nam, chắc chắn, nghiêm", "Rasalgethi": "nam, truyền cảm, như phát thanh viên",
    "Umbriel": "nam, thoải mái, nhẹ nhàng", "Schedar": "nam, đều đặn, bình tĩnh", "Achird": "nam, thân thiện",
    "Fenrir": "nam, sôi nổi", "Puck": "nam trẻ, vui tươi", "Enceladus": "nam, thì thầm, nhiều hơi thở",
    "Sadaltager": "nam, hiểu biết", "Zubenelgenubi": "nam, tự nhiên như trò chuyện",
    "Sulafat": "nữ, ấm áp", "Vindemiatrix": "nữ, dịu dàng", "Achernar": "nữ, mềm mại", "Kore": "nữ, chắc chắn",
    "Despina": "nữ, êm mượt", "Aoede": "nữ, nhẹ nhàng thoáng đãng", "Callirrhoe": "nữ, thoải mái",
    "Erinome": "nữ, trong trẻo", "Leda": "nữ trẻ", "Autonoe": "nữ, tươi sáng", "Zephyr": "nữ, sáng",
    "Laomedeia": "nữ, vui tươi", "Pulcherrima": "nữ, mạnh mẽ", "Sadachbia": "nữ, sinh động", "Algieba": "nam, êm mượt",
}
GeminiVoiceName = Literal[tuple(GEMINI_VOICES)]  # type: ignore[valid-type]


class GeminiVoicePick(BaseModel):
    name: GeminiVoiceName = Field(description="Giọng gốc hợp nhất với mô tả")
    style_en: str = Field(description="TIẾNG ANH, 1–2 câu: chỉ dẫn cách đọc cho TTS (tuổi, chất giọng, cảm xúc, nhịp, "
                                      "khoảng nghỉ, giọng miền Bắc Việt Nam), vd 'Read in Vietnamese with a Northern "
                                      "accent, like a wise elderly man, deep warm voice, slow, gentle pauses'")
    note_vi: str = Field(description="Đã chọn giọng thế nào, 1 câu tiếng Việt")


def design_voice(desc: str, current: dict) -> GeminiVoicePick:
    system = ("Bạn thiết kế giọng đọc cho video TikTok tiếng Việt bằng Gemini TTS: chọn MỘT giọng gốc trong danh mục và "
              "viết chỉ dẫn cách đọc bằng tiếng Anh theo đúng mô tả của chủ kênh. Luôn yêu cầu đọc tiếng Việt giọng "
              "miền Bắc trừ khi chủ kênh muốn khác.")
    user = (f"Danh mục giọng: {json.dumps(GEMINI_VOICES, ensure_ascii=False)}\nGiọng hiện tại: "
            f"{json.dumps(current, ensure_ascii=False)}\nMô tả của chủ kênh:\n<desc>\n{desc}\n</desc>")
    return _parse(system, user, GeminiVoicePick, effort="low", max_tokens=800)


def gemini_tts(text: str, voice: dict, out: Path, model: str) -> float:
    """One line with a Gemini prebuilt voice + a spoken-style instruction → WAV; returns its duration."""
    from google import genai  # reads GEMINI_API_KEY from the environment
    from google.genai import types
    client = genai.Client()
    style = (voice.get("style") or "Read in Vietnamese, Northern accent, warm and calm").strip().rstrip(".:")
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice.get("name") or "Charon"))))
    res = client.models.generate_content(model=model, contents=f"{style}: {text}", config=cfg)
    data = None
    for cand in getattr(res, "candidates", None) or []:
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            data = data or getattr(getattr(part, "inline_data", None), "data", None)
    if not data:
        raise RuntimeError("Gemini TTS không trả về âm thanh")
    raw = out.with_suffix(".pcm")
    raw.write_bytes(data)  # 24 kHz, 16-bit, mono PCM
    _run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", str(raw),
          "-af", "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
                 "silenceremove=start_periods=1:start_threshold=-50dB,areverse", str(out)])
    raw.unlink(missing_ok=True)
    return _duration(out)


def _line_cues(text: str, start: float, end: float, max_words: int = audio.MAX_WORDS) -> list[dict]:
    """Subtitle lines for one spoken sentence: split at commas, then evenly, timed by word count."""
    words = text.split()
    if not words:
        return []
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or (w[-1:] in ",;:" and len(cur) >= 3):
            groups.append(cur)
            cur = []
    if cur:
        if groups and len(cur) <= 2 and len(groups[-1]) + len(cur) <= max_words + 2:
            groups[-1] += cur
        else:
            groups.append(cur)
    out, t, per = [], start, (end - start) / len(words)
    for g in groups:
        out.append({"start": round(t, 3), "end": round(t + per * len(g), 3), "text": " ".join(g)})
        t += per * len(g)
    return out


def gemini_voice_track(lines: list[str], voice: dict, workdir: Path, model: str, gap: float = 0.45) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    parts, cues, scenes, t = [], [], [], 0.0
    for k, line in enumerate(lines):
        f = workdir / f"g_{k}.wav"
        d = gemini_tts(line, voice, f, model)
        parts.append(f)
        scenes.append({"start": round(t, 3), "end": round(t + d, 3)})
        cues += _line_cues(line, t, t + d)
        t += d + gap
    ins = [x for f in parts for x in ("-i", str(f))]
    pads = "".join(f"[{k}:a]aresample=24000,apad=pad_dur={gap}[p{k}];" for k in range(len(parts)))
    out = workdir / "narration.mp3"
    _run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex",
          pads + "".join(f"[p{k}]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]", "-map", "[a]",
          "-c:a", "libmp3lame", "-b:a", "192k", str(out)])
    for f in parts:
        f.unlink(missing_ok=True)
    meta = {"file": out.name, "duration": _duration(out), "engine": "gemini", "name": voice.get("name"),
            "style": voice.get("style"), "scenes": scenes, "cues": cues}
    (workdir / "narration.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {**meta, "path": str(out)}


def total_sec(meta: dict) -> float:
    return LEAD_SEC + float(meta["duration"]) + TAIL_SEC


def spans(meta: dict) -> list[tuple[float, float]]:
    return [(LEAD_SEC + c["start"], LEAD_SEC + c["end"]) for c in meta.get("cues") or []]


def preview(meta: dict, music: str | None, cfg: dict, out: Path) -> Path:
    """Voice + music exactly as in the video (same lead, same ducking, outro fade)."""
    d = out.parent
    lead = d / "voice_lead.wav"
    _run(["ffmpeg", "-y", "-v", "error", "-i", meta["path"], "-af",
          f"adelay={int(LEAD_SEC * 1000)}:all=1,apad,atrim=0:{total_sec(meta):.3f}", str(lead)])
    return audio.preview(lead, music, total_sec(meta), cfg, out, spans=spans(meta))


def render(script: dict, image: Path, meta: dict, workdir: Path, cfg: dict) -> Path:
    """`cfg["video"]` carries fonts, sub_style, background_music. Returns final.mp4."""
    workdir.mkdir(parents=True, exist_ok=True)
    body = total_sec(meta)
    frames = int(round(body * FPS))
    # very slow push-in (≈6%) on a slightly larger copy, so the still image breathes without jitter
    zoom = (f"scale={int(W * 1.25)}:{int(H * 1.25)}:force_original_aspect_ratio=increase,"
            f"crop={int(W * 1.25)}:{int(H * 1.25)},zoompan=z='min(1+0.06*on/{frames},1.06)':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},setsar=1,format=yuv420p,"
            "fade=t=in:st=0:d=0.4")
    seg = workdir / "seg0.mkv"
    _run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(image), "-f", "lavfi", "-i",
          "anullsrc=r=48000:cl=stereo", "-filter_complex", f"[0:v]{zoom}[v]", "-map", "[v]", "-map", "1:a",
          "-t", f"{body:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "pcm_s16le", str(seg)])
    segments = [seg]
    outro = make_outro(seg, workdir, cfg, script.get("closing_line") or None)
    if outro:
        segments.append(outro)
    (workdir / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segments))
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"],
         cwd=workdir)
    total = body + (_duration(outro) if outro else 0.0)
    srt = []
    for k, c in enumerate(meta.get("cues") or [], 1):
        srt += [str(k), f"{_srt_time(LEAD_SEC + c['start'])} --> {_srt_time(LEAD_SEC + c['end'])}", c["text"], ""]
    (workdir / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    voice = workdir / "voice_lead.wav"
    _run(["ffmpeg", "-y", "-v", "error", "-i", meta["path"], "-af",
          f"adelay={int(LEAD_SEC * 1000)}:all=1,apad,atrim=0:{total:.3f}", str(voice)])
    music = cfg["video"].get("background_music")
    final_mix(workdir, cfg, total, "joined.mkv", str(voice),
              str(Path(music).resolve()) if music and Path(music).exists() else None, spans(meta),
              ambient_from_joined=False, thumb_text=script.get("thumb_text", ""))
    final = workdir / "final.mp4"
    log.info("Rendered wisdom video %s (%.1fs)", final, _duration(final))
    return final
