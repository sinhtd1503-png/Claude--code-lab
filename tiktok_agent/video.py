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


def synthesize(text: str, voice: str, rate: str, out: Path, retries: int = 5) -> float:
    for attempt in range(1, retries + 1):
        try:
            asyncio.run(_tts(text, voice, rate, out))
            return _duration(out)
        except edge_tts.exceptions.EdgeTTSException as e:  # service is occasionally flaky
            if attempt == retries:
                raise
            log.warning("TTS attempt %d failed (%s), retrying…", attempt, e)
            time.sleep(2 * attempt)
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

def render(script: dict, workdir: Path, cfg: dict, stock_key: str, ai=None) -> Path:
    """`ai` is an optional AIVideoSource; scenes selected in config use AI video, others Pixabay."""
    workdir.mkdir(parents=True, exist_ok=True)
    voice, vid = cfg["voice"], cfg["video"]
    used: set[str] = set()
    segments: list[Path] = []
    srt_lines: list[str] = []
    t0, idx = 0.0, 1

    for i, scene in enumerate(script["scenes"]):
        audio = workdir / f"s{i}.mp3"
        dur = synthesize(scene["narration"], voice["name"], voice["rate"], audio) + PAD_SEC

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
        _run(["ffmpeg", "-y", *video_in, "-i", str(audio),
              "-filter_complex", f"[0:v]{vf}[v];[1:a]apad,aresample=44100[a]",
              "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "160k", "-ac", "2", str(seg)])
        segments.append(seg)

        # subtitles: spread scene duration across chunks by character length
        chunks = _chunk_words(scene["narration"])
        speak = dur - PAD_SEC
        total_chars = sum(len(c) for c in chunks) or 1
        t = t0
        for c in chunks:
            d = speak * len(c) / total_chars
            srt_lines += [str(idx), f"{_srt_time(t)} --> {_srt_time(t + d)}", c, ""]
            idx, t = idx + 1, t + d
        t0 += dur

    (workdir / "subs.srt").write_text("\n".join(srt_lines), encoding="utf-8")
    (workdir / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segments))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"],
         cwd=workdir)

    style = (f"FontName={vid['font']},FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
             "BorderStyle=1,Outline=2,Shadow=0,Bold=1,Alignment=2,MarginV=70")
    final = workdir / "final.mp4"
    music = vid.get("background_music")
    if music and Path(music).exists():
        _run(["ffmpeg", "-y", "-i", "joined.mp4", "-stream_loop", "-1", "-i", str(Path(music).resolve()),
              "-filter_complex",
              f"[0:v]subtitles=subs.srt:force_style='{style}'[v];"
              f"[1:a]volume={vid['music_volume']}[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
              "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
              "final.mp4"], cwd=workdir)
    else:
        _run(["ffmpeg", "-y", "-i", "joined.mp4", "-vf", f"subtitles=subs.srt:force_style='{style}'",
              "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
              "-c:a", "copy", "-movflags", "+faststart", "final.mp4"], cwd=workdir)
    log.info("Rendered %s (%.1fs)", final, _duration(final))
    return final
