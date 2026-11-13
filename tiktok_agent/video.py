"""Render a faceless 9:16 video: AI voice (edge-tts) + Pixabay stock clips + burned subtitles."""
from __future__ import annotations

import asyncio
import json
import math
import logging
import random
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import httpx

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
PAD_SEC = 0.35  # small pause after each scene's narration


# ---------- helpers ----------

def _run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd[:6])}…\n{proc.stderr[-2000:]}")


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _chunk_words(text: str, max_words: int = 7) -> list[str]:
    """Split narration into short, evenly sized subtitle lines (no orphan words)."""
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        words = sentence.split()
        if not words:
            continue
        k = math.ceil(len(words) / max_words)
        size = math.ceil(len(words) / k)
        chunks += [" ".join(words[i:i + size]) for i in range(0, len(words), size)]
    return chunks


# ---------- TTS ----------

async def _tts(text: str, voice: str, rate: str, out: Path) -> None:
    await edge_tts.Communicate(text, voice, rate=rate).save(str(out))


# edge-tts fails in bursts ("No audio was received") that can last ~20s; back off long enough to outlast them.
TTS_BACKOFF_SEC = (2, 4, 8, 15, 25, 40)


def synthesize(text: str, voice: str, rate: str, out: Path) -> float:
    for attempt in range(len(TTS_BACKOFF_SEC) + 1):
        try:
            asyncio.run(_tts(text, voice, rate, out))
            return _duration(out)
        except edge_tts.exceptions.EdgeTTSException as e:
            if attempt == len(TTS_BACKOFF_SEC):
                raise
            log.warning("TTS attempt %d failed (%s), retrying in %ss…", attempt + 1, e, TTS_BACKOFF_SEC[attempt])
            time.sleep(TTS_BACKOFF_SEC[attempt])
    raise AssertionError("unreachable")


# ---------- Pixabay ----------

PIXABAY_URL = "https://pixabay.com/api/videos/"


def _pick_rendition(hit: dict) -> dict | None:
    """Pick the smallest rendition that is still >=1080px tall, else the tallest available."""
    files = [f for f in (hit.get("videos") or {}).values() if f.get("url") and f.get("height")]
    if not files:
        return None
    hd = [f for f in files if f["height"] >= 1080]
    return min(hd, key=lambda f: f["height"]) if hd else max(files, key=lambda f: f["height"])


def fetch_clip(api_key: str, query: str, fallback_query: str, out: Path, used: set[str]) -> Path | None:
    """Download a stock clip from Pixabay. Portrait clips are preferred; landscape ones get center-cropped."""
    for q in (query, fallback_query):
        r = httpx.get(
            PIXABAY_URL,
            params={"key": api_key, "q": q[:100], "video_type": "film",
                    "safesearch": "true", "per_page": 30},
            timeout=30,
        )
        r.raise_for_status()
        hits = [h for h in r.json().get("hits", []) if str(h["id"]) not in used]
        random.shuffle(hits)
        # portrait first (height > width), keep random order within each group
        hits.sort(key=lambda h: not any(f.get("height", 0) > f.get("width", 0)
                                         for f in (h.get("videos") or {}).values()))
        for h in hits:
            best = _pick_rendition(h)
            if not best:
                continue
            with httpx.stream("GET", best["url"], timeout=120, follow_redirects=True) as resp:
                resp.raise_for_status()
                with out.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
            used.add(str(h["id"]))
            log.info("Clip for %r → pixabay #%s (%sx%s)", q, h["id"], best["width"], best["height"])
            return out
    return None


# ---------- render ----------

def render(script: dict, workdir: Path, cfg: dict, stock_key: str, ai=None, narration: dict | None = None) -> Path:
    """`ai` is an optional AIVideoSource; scenes selected in config use AI video, others Pixabay.

    The voice-over is ONE continuous track (see audio.py); scene lengths follow the voice so cuts land between
    sentences. `narration` = meta from audio.narration_track() with "path" set (the approved preview's voice);
    if omitted it is generated here.
    """
    from . import audio  # local import: audio.py imports helpers from this module

    workdir.mkdir(parents=True, exist_ok=True)
    voice, vid = cfg["voice"], cfg["video"]
    if narration is None:
        narration = audio.narration_track([s["narration"] for s in script["scenes"]],
                                          script.get("voice") or voice["name"], voice["rate"], workdir)
        narration["path"] = str(workdir / narration["file"])
    durs = audio.scene_durations(narration)
    used: set[str] = set()
    segments: list[Path] = []

    for i, scene in enumerate(script["scenes"]):
        dur = durs[i]
        clip = None
        if ai is not None and ai.wants_ai(i, len(script["scenes"])):
            try:
                clip = ai.scene_clip(i, scene, script.get("visual_bible"), dur, workdir)
            except Exception as e:  # budget exceeded, safety block, timeout… → fall back to stock
                log.warning("AI video scene %d → dùng Pixabay thay thế: %s", i + 1, e)
                ai.note_fallback(i, str(e)[:300])
        if clip is None and stock_key:
            try:
                clip = fetch_clip(stock_key, scene["visual_query"], vid["fallback_query"],
                                  workdir / f"c{i}.mp4", used)
            except httpx.HTTPStatusError as e:  # don't log the URL: it contains the API key
                log.warning("Pixabay failed for %r: HTTP %s", scene["visual_query"], e.response.status_code)
            except httpx.HTTPError as e:
                log.warning("Pixabay failed for %r: %s", scene["visual_query"], type(e).__name__)
        seg = workdir / f"seg{i}.mp4"
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,fps={FPS},"
              f"eq=saturation=0.9:brightness=0.02,"  # soft, warm nostalgic look
              f"fade=t=in:st=0:d=0.3")
        if clip:
            video_in = ["-stream_loop", "-1", "-i", str(clip)]
        else:  # no stock clip found → warm solid background
            video_in = ["-f", "lavfi", "-i", f"color=c=0x3b2f2a:s={W}x{H}:r={FPS}"]
        _run(["ffmpeg", "-y", *video_in, "-vf", vf, "-an", "-t", f"{dur:.3f}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", str(seg)])
        segments.append(seg)

    srt = []
    for k, c in enumerate(narration["cues"], 1):
        srt += [str(k), f"{_srt_time(c['start'])} --> {_srt_time(max(c['end'], c['start'] + 0.4))}", c["text"], ""]
    (workdir / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    (workdir / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segments))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"],
         cwd=workdir)

    total = sum(durs)
    style = (f"FontName={vid['font']},FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
             "BorderStyle=1,Outline=2,Shadow=0,Bold=1,Alignment=2,MarginV=70")
    final = workdir / "final.mp4"
    music = vid.get("background_music")
    inputs = ["-i", "joined.mp4", "-i", str(Path(narration["path"]).resolve())]
    if music and Path(music).exists():
        inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        afilter = audio.music_filter(total, float(vid.get("music_volume", 0.18)), bool(vid.get("music_duck", True)),
                                     voice="[1:a]", music="[2:a]")
    else:
        afilter = f"[1:a]aresample=44100,aformat=channel_layouts=stereo,apad,atrim=0:{total:.3f}[a]"
    _run(["ffmpeg", "-y", *inputs, "-filter_complex",
          f"[0:v]subtitles=subs.srt:force_style='{style}'[v];{afilter}",
          "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "final.mp4"],
         cwd=workdir)
    log.info("Rendered %s (%.1fs)", final, _duration(final))
    return final
