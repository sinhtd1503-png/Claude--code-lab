"""Continuous voice-over + background music.

The whole script is read in ONE text-to-speech pass, so intonation flows from scene to scene instead of
sounding like separate clips glued together. Word timings from edge-tts give:
  - where each scene starts/ends in the narration (the video cuts follow the voice), and
  - subtitle cues that match the speech exactly.
The same narration file is used for the preview the owner approves on Telegram and for the final video.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import edge_tts

from .video import TTS_BACKOFF_SEC, _duration, _run

log = logging.getLogger(__name__)

TICKS = 10_000_000  # edge-tts offsets are in 100-ns units
TAIL_SEC = 0.9      # music/picture keep going a little after the last word
MAX_WORDS = 7       # words per subtitle line


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("đ", "d").replace("Đ", "D")).lower()
    return "".join(ch for ch in s if ch.isalnum())


async def _stream(text: str, voice: str, rate: str, out: Path) -> list[dict]:
    words: list[dict] = []
    com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    with out.open("wb") as fh:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({"text": chunk["text"], "start": chunk["offset"] / TICKS,
                              "end": (chunk["offset"] + chunk["duration"]) / TICKS})
    return words


def _tts_words(text: str, voice: str, rate: str, out: Path) -> list[dict]:
    for attempt in range(len(TTS_BACKOFF_SEC) + 1):
        try:
            words = asyncio.run(_stream(text, voice, rate, out))
            if out.stat().st_size == 0:
                raise edge_tts.exceptions.NoAudioReceived("empty audio")
            return words
        except edge_tts.exceptions.EdgeTTSException as e:
            if attempt == len(TTS_BACKOFF_SEC):
                raise
            log.warning("TTS attempt %d failed (%s), retrying in %ss…", attempt + 1, e, TTS_BACKOFF_SEC[attempt])
            time.sleep(TTS_BACKOFF_SEC[attempt])
    raise AssertionError("unreachable")


def _assign(words: list[dict], narrations: list[str]) -> list[int]:
    """Scene index for every spoken word, by walking the normalized characters of each scene's text."""
    bounds, total = [], 0
    for n in narrations:
        total += len(_norm(n))
        bounds.append(total)
    out, pos, k = [], 0, 0
    for w in words:
        while k < len(bounds) - 1 and pos >= bounds[k]:
            k += 1
        out.append(k)
        pos += len(_norm(w["text"]))
    return out


def _even_chunks(items: list, max_n: int = MAX_WORDS) -> list[list]:
    if not items:
        return []
    k = -(-len(items) // max_n)
    size = -(-len(items) // k)
    return [items[x:x + size] for x in range(0, len(items), size)]


def _cues(words: list[dict], scene_of: list[int], texts: list[str]) -> list[dict]:
    """Subtitle lines: never cross a scene, break at commas/full stops, evenly sized (≤ MAX_WORDS words).
    Lines show the script's own words (with punctuation); timings come from the spoken words."""
    cues = []
    for k, text in enumerate(texts):
        ws = [w for w, s in zip(words, scene_of) if s == k]
        if not ws:
            continue
        tokens = text.split()
        if len(tokens) != len(ws):  # TTS split words differently → show spoken words as they are
            tokens = [w["text"] for w in ws]
        phrases, cur = [], []
        for idx, tok in enumerate(tokens):
            cur.append(idx)
            if re.search(r"[,;:.!?…]$", tok):
                phrases.append(cur)
                cur = []
        if cur:
            phrases.append(cur)
        merged: list[list[int]] = []  # glue tiny phrases ("Ngày ấy,") to the next one when it still fits
        for ph in phrases:
            if merged and len(merged[-1]) <= 2 and len(merged[-1]) + len(ph) <= MAX_WORDS:
                merged[-1] += ph
            else:
                merged.append(ph)
        for ph in merged:
            for chunk in _even_chunks(ph):
                cues.append({"start": ws[chunk[0]]["start"], "end": ws[chunk[-1]]["end"],
                             "text": " ".join(tokens[c] for c in chunk)})
    return cues


def narration_track(narrations: list[str], voice: str, rate: str, workdir: Path) -> dict:
    """Read all scenes in one pass. Returns {"file", "duration", "scenes": [{start, end}], "cues": [...]}."""
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "narration.mp3"
    texts = [n.strip() if re.search(r"[.!?…]$", n.strip()) else n.strip() + "." for n in narrations]
    words = _tts_words(" ".join(texts), voice, rate, out)
    dur = _duration(out)
    if not words:  # no timings (should not happen) → split evenly by text length
        lens = [max(1, len(t)) for t in texts]
        t, scenes = 0.0, []
        for ln in lens:
            d = dur * ln / sum(lens)
            scenes.append({"start": t, "end": t + d})
            t += d
        cues = [{"start": s["start"], "end": s["end"], "text": t} for s, t in zip(scenes, texts)]
    else:
        scene_of = _assign(words, texts)
        scenes = []
        for k in range(len(texts)):
            ws = [w for w, s in zip(words, scene_of) if s == k]
            scenes.append({"start": ws[0]["start"], "end": ws[-1]["end"]} if ws else None)
        for k, s in enumerate(scenes):  # a scene the aligner missed borrows the gap around it
            if s is None:
                prev = scenes[k - 1]["end"] if k and scenes[k - 1] else 0.0
                scenes[k] = {"start": prev, "end": prev}
        cues = _cues(words, scene_of, texts)
    meta = {"file": out.name, "duration": dur, "voice": voice, "rate": rate, "scenes": scenes, "cues": cues}
    (workdir / "narration.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def scene_durations(meta: dict) -> list[float]:
    """Picture length of each scene: from its first word to the next scene's first word (cuts land on the voice)."""
    sc = meta["scenes"]
    starts = [0.0] + [s["start"] for s in sc[1:]]
    ends = starts[1:] + [max(meta["duration"], sc[-1]["end"]) + TAIL_SEC]
    return [max(1.0, e - s) for s, e in zip(starts, ends)]


def music_filter(total: float, volume: float, duck: bool, voice: str = "[voice]", music: str = "[1:a]") -> str:
    """ffmpeg filter graph: music looped/faded, ducked under the voice; output label [a]."""
    fade = min(2.0, total / 4)
    mus = (f"{music}aresample=44100,aformat=channel_layouts=stereo,volume={volume},"
           f"afade=t=in:d=0.8,afade=t=out:st={max(0.0, total - fade):.2f}:d={fade:.2f}[m0];")
    v = f"{voice}aresample=44100,aformat=channel_layouts=stereo,apad,atrim=0:{total:.3f}"
    if duck:
        return (mus + f"{v},asplit=2[vo][key];"
                "[m0][key]sidechaincompress=threshold=0.03:ratio=5:attack=30:release=500[m];"
                "[vo][m]amix=inputs=2:duration=first:normalize=0[a]")
    return mus + f"{v}[vo];[vo][m0]amix=inputs=2:duration=first:normalize=0[a]"


def preview(narration: Path, music: str | None, total: float, cfg: dict, out: Path) -> Path:
    """MP3 of exactly what the video will sound like (voice + music), for approval on Telegram."""
    vid = cfg.get("video", {})
    if music and Path(music).exists():
        _run(["ffmpeg", "-y", "-i", str(narration), "-stream_loop", "-1", "-i", str(music),
              "-filter_complex", music_filter(total, float(vid.get("music_volume", 0.18)),
                                              bool(vid.get("music_duck", True)), voice="[0:a]"),
              "-map", "[a]", "-t", f"{total:.3f}", "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
    else:
        _run(["ffmpeg", "-y", "-i", str(narration), "-af", f"apad,atrim=0:{total:.3f}",
              "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
    return out
