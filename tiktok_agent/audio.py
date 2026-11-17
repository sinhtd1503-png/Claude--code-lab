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
import math
import re
import subprocess
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


# ---------------------------------------------------------------- the soundtrack mix (video AND previews)

SR = 48000


def music_settings(cfg: dict) -> dict:
    v = (cfg or {}).get("video", {}) or {}
    return {"volume": float(v.get("music_volume", 0.22)), "duck": bool(v.get("music_duck", True)),
            "floor": float(v.get("music_floor", 0.5)), "xfade": float(v.get("music_crossfade", 1.5)),
            "fade_out": float(v.get("music_fade_out", 2.5)), "outro": float(v.get("outro_sec", 3.5)),
            "min_dbfs": float(v.get("music_min_dbfs", -35)),
            "level": str(v.get("music_level", "auto")), "below": float(v.get("music_below_voice_db", 12)),
            "ambient": float(v.get("clip_ambient_volume", 0.35))}


def stereo_prefix(path: str | Path | None) -> str:
    """Mono → stereo by copying the channel (ffmpeg's default conversion would make it 3 dB quieter)."""
    if not path:
        return ""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return "pan=stereo|c0=c0|c1=c0," if out == "1" else ""


def music_bed(music: str, total: float, out: Path, xfade: float = 1.5) -> Path:
    """One continuous music track exactly `total` seconds long. A track shorter than the video is repeated with a
    `xfade`-second crossfade at every join (never a hard loop)."""
    d = _duration(Path(music))
    norm = f"{stereo_prefix(music)}aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo"
    if d >= total + 1.0 or d <= 2 * xfade + 0.5:
        loop = ["-stream_loop", "-1"] if d < total else []
        _run(["ffmpeg", "-y", *loop, "-i", str(music), "-af", f"{norm},atrim=0:{total:.3f}", "-t", f"{total:.3f}",
              "-c:a", "pcm_s16le", str(out)])
        return out
    n = 1
    while n * d - (n - 1) * xfade < total + 2.0:  # margin: decoded mp3s are often a little shorter than reported
        n += 1
    ins = [x for _ in range(n) for x in ("-i", str(music))]
    chain = ";".join(f"[{k}:a]{norm}[i{k}]" for k in range(n))
    prev = "i0"
    for k in range(1, n):
        chain += f";[{prev}][i{k}]acrossfade=d={xfade}:c1=qsin:c2=qsin[x{k}]"
        prev = f"x{k}"
    _run(["ffmpeg", "-y", *ins, "-filter_complex", f"{chain};[{prev}]atrim=0:{total:.3f}[m]", "-map", "[m]",
          "-c:a", "pcm_s16le", str(out)])
    return out


def rms_db(path: str | Path, ss: float | None = None, t: float | None = None) -> float:
    a = ["ffmpeg", "-v", "info"] + (["-ss", f"{ss:.3f}"] if ss is not None else []) + \
        (["-t", f"{t:.3f}"] if t else []) + ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    m = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", subprocess.run(a, capture_output=True, text=True).stderr)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -120.0


def auto_music_volume(cfg: dict, voice: str | Path | None, bed: str | Path,
                      spans: list[tuple[float, float]]) -> float:
    """Music gain so the bed sits `music_below_voice_db` under the voices — whatever the clips' or the track's own
    loudness — so it is always audible but never covers the dialogue. music_level: fixed → music_volume as is."""
    st = music_settings(cfg)
    if st["level"] != "auto" or not voice:
        return st["volume"]
    sp = [(a, b) for a, b in merge_spans(spans, 0, 0) if b - a > 0.3][:12]
    levels = [rms_db(voice, a, b - a) for a, b in sp] if sp else [rms_db(voice)]
    levels = [x for x in levels if x > -90]
    m = rms_db(bed)
    if not levels or m < -90:
        return st["volume"]
    voice_db = 10 * math.log10(sum(10 ** (x / 10) for x in levels) / len(levels))
    target = max(voice_db - st["below"], st["min_dbfs"] + 4)  # never so low that the music seems to vanish
    gain = 10 ** ((target - m) / 20)
    return round(max(0.05, min(6.0, gain)), 4)  # up to +15.6 dB for very quiet tracks


def merge_spans(spans: list[tuple[float, float]], pre: float = 0.1, post: float = 0.3) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((max(0.0, a - pre), b + post) for a, b in spans if b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def mix_graph(total: float, cfg: dict, voice: str | None, music: str | None, spans: list[tuple[float, float]],
              ambient: str | None = None, voice_pre: str = "", ambient_pre: str = "", stems: bool = True,
              volume: float | None = None) -> str:
    """ffmpeg filter graph → [a] (final mix), [vst] (voice stem), [mst] (music stem).

    • voice  : passed through untouched (only resampled to a common format) — volume and speed never change
    • music  : the continuous bed at `music_volume`; ducked ONLY while someone speaks (key = speech spans, so rain or
               wind in a clip never pushes it down), ratio 2, and never below `music_floor` (50%) of its level;
               full level in the outro, fading out only in the last `music_fade_out` seconds (after the last line)
    • ambient: clip sound under a narration at `clip_ambient_volume`
    """
    st = music_settings(cfg)
    fmt = f"aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo"
    parts, mix = [], []
    vsplit = ",asplit=2[v1][vst]" if stems else "[v1]"
    if voice:
        parts.append(f"{voice}{voice_pre}{fmt},apad,atrim=0:{total:.3f}{vsplit}")
        mix.append("[v1]")
    else:
        parts.append(f"anullsrc=r={SR}:cl=stereo,atrim=0:{total:.3f}{vsplit}")
        mix.append("[v1]")
    if music:
        fo = min(st["fade_out"], max(0.5, st["outro"] - 0.3)) if st["outro"] > 0 else min(st["fade_out"], total / 4)
        parts.append(f"{music}{fmt},apad,atrim=0:{total:.3f},volume={st['volume'] if volume is None else volume},"
                     "afade=t=in:d=0.4,"
                     f"afade=t=out:st={max(0.0, total - fo):.3f}:d={fo:.3f}[m0]")
        sp = merge_spans(spans)
        if st["duck"] and sp:
            gate = "+".join(f"between(t,{a},{b})" for a, b in sp)
            key = f"0.5*sin(2*PI*997*t)*min(1,{gate})"
            floor = max(0.0, min(1.0, st["floor"]))
            parts.append(f"aevalsrc=exprs='{key}|{key}':s={SR}:d={total:.3f}[key]")
            parts.append("[m0]asplit=2[mfix][mduck]")
            parts.append("[mduck][key]sidechaincompress=threshold=0.02:ratio=2:attack=40:release=600[mcomp]")
            parts.append(f"[mfix]volume={floor}[mf];[mcomp]volume={1 - floor}[mc];"
                         "[mf][mc]amix=inputs=2:normalize=0" + (",asplit=2[m1][mst]" if stems else "[m1]"))
        else:
            parts.append("[m0]asplit=2[m1][mst]" if stems else "[m0]anull[m1]")
        mix.append("[m1]")
    elif stems:
        parts.append(f"anullsrc=r={SR}:cl=stereo,atrim=0:{total:.3f}[mst]")
    if ambient:
        parts.append(f"{ambient}{ambient_pre}{fmt},apad,atrim=0:{total:.3f},volume={st['ambient']}[amb]")
        mix.append("[amb]")
    parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[a]" if len(mix) > 1
                 else f"{mix[0]}anull[a]")
    return ";".join(parts)


def write_stems(ins: list[str], graph: str, total: float, workdir: Path, cwd: Path | None = None) -> None:
    """Voice and music stems from the same graph (built with stems=True), in their own audio-only pass (sharing the
    command with the video would stop them when the video ends and lose the last second)."""
    _run(["ffmpeg", "-y", *ins, "-filter_complex", graph,
          "-map", "[a]", "-t", f"{total:.3f}", "-f", "null", "-",
          "-map", "[vst]", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(workdir / "stem_voice.wav"),
          "-map", "[mst]", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(workdir / "stem_music.wav")], cwd=cwd)


def window_levels(stem: Path, total: float, win: float = 8.0) -> list[dict]:
    """RMS (dBFS) of a stem in consecutive windows; a short last piece (< win/2) joins the previous window."""
    edges, t = [], 0.0
    while t < total - 1e-6:
        edges.append([t, min(total, t + win)])
        t += win
    if len(edges) > 1 and edges[-1][1] - edges[-1][0] < win / 2:
        edges[-2][1] = edges.pop()[1]
    out = []
    for a, b in edges:
        p = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(stem),
                            "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", p.stderr)
        db = float(m.group(1)) if m and m.group(1) != "-inf" else -120.0
        out.append({"start": round(a, 2), "end": round(b, 2), "rms_db": db})
    return out


def check_music(stem: Path, total: float, cfg: dict) -> list[str]:
    """Warnings for 8-second windows where the music is too quiet (or missing)."""
    lim = music_settings(cfg)["min_dbfs"]
    warn = []
    for w in window_levels(stem, total):
        if w["rms_db"] < lim:
            warn.append(f"nhạc nền quá nhỏ ở giây {w['start']:.0f}–{w['end']:.0f} ({w['rms_db']:.1f} dBFS < {lim:.0f})")
    for x in warn:
        log.warning("Kiểm tra nhạc: %s", x)
    return warn


def soundtrack(out: Path, total: float, cfg: dict, voice_file: str | None, music: str | None,
               spans: list[tuple[float, float]], workdir: Path, ambient_file: str | None = None,
               codec: list[str] | None = None) -> dict:
    """Render the mixed soundtrack + stems to files (used by previews; videos use mix_graph in their final pass)."""
    bed = music_bed(music, total, workdir / "music_bed.wav", music_settings(cfg)["xfade"]) if music else None
    ins, k = [], 0
    vl = ml = al = None
    for f, tag in ((voice_file, "v"), (str(bed) if bed else None, "m"), (ambient_file, "a")):
        if f:
            ins += ["-i", f]
            if tag == "v":
                vl = f"[{k}:a]"
            elif tag == "m":
                ml = f"[{k}:a]"
            else:
                al = f"[{k}:a]"
            k += 1
    kw = dict(voice_pre=stereo_prefix(voice_file), ambient_pre=stereo_prefix(ambient_file),
              volume=auto_music_volume(cfg, voice_file, bed, spans) if bed else None)
    ms = workdir / "stem_music.wav"
    _run(["ffmpeg", "-y", *ins, "-filter_complex", mix_graph(total, cfg, vl, ml, spans, al, stems=False, **kw),
          "-map", "[a]", "-t", f"{total:.3f}", *(codec or ["-c:a", "libmp3lame", "-b:a", "160k"]), str(out)])
    write_stems(ins, mix_graph(total, cfg, vl, ml, spans, al, **kw), total, workdir)
    return {"out": out, "music_stem": ms, "voice_stem": workdir / "stem_voice.wav"}


def preview(narration: Path, music: str | None, total: float, cfg: dict, out: Path,
            spans: list[tuple[float, float]] | None = None) -> Path:
    """MP3 of what the video will sound like (voice + music bed + the same ducking, outro and fade-out)."""
    outro = music_settings(cfg)["outro"]
    soundtrack(out, total + outro, cfg, str(narration), music if music and Path(music).exists() else None,
               spans or [], out.parent)
    return out
