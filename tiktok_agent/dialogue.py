"""Dialogue videos: characters talk to each other inside the AI clips; no narrator.

The AI video model (Google Flow / Gemini) is asked to voice the dialogue with lip-sync, so each clip already
carries the characters' voices and ambient sound. This module keeps that audio, adds light background music
that dips under the voices, and burns subtitles from the script's dialogue lines.

Voice modes (config `voice.dialogue_voice`, switchable with /giong):
  • "fixed": every line is voiced by edge-tts with the character's own fixed voice (cast `tts`), so a character
    sounds exactly the same in every clip — whether the clip came from Gemini or was made by hand on Flow. The
    same tracks are built at script approval (preview) and reused at render, so what was approved is what ships.
  • "clip": keep the voices the video model generated (better lip-sync, may drift between clips); a clip without
    sound still gets the fixed voices.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import edge_tts

from . import audio
from .video import FPS, H, TTS_BACKOFF_SEC, W, _duration, _run, _srt_time, fetch_clip

log = logging.getLogger(__name__)

SR = 48000      # common sample rate of all segments (AI clips are 48 kHz)
LEAD_SEC = 0.5   # first line starts a little after the cut
GAP_SEC = 0.3    # pause between lines


def channels(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 2


def to_stereo(path: Path) -> str:
    """Filter prefix that makes stereo WITHOUT changing loudness (ffmpeg's default mono→stereo lowers it 3 dB)."""
    return "pan=stereo|c0=c0|c1=c0," if channels(path) == 1 else ""


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout or "{}"
    return bool(json.loads(out).get("streams"))


def speech_times(clip: Path, lines: list[dict], dur: float) -> list[tuple[float, float]] | None:
    """Subtitle times from where the voices really are in a clip (silence detection); None if it can't tell."""
    if not lines:
        return []
    proc = subprocess.run(["ffmpeg", "-v", "info", "-i", str(clip), "-af", "silencedetect=noise=-30dB:d=0.25",
                           "-vn", "-f", "null", "-"], capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    silent = list(zip(starts, ends + [dur] * (len(starts) - len(ends))))
    spans, t = [], 0.0
    for a, b in silent:
        if a - t > 0.15:
            spans.append((t, a))
        t = b
    if dur - t > 0.15:
        spans.append((t, dur))
    if not spans or sum(b - a for a, b in spans) > 0.95 * dur:  # no pauses found (music/ambience): can't tell
        return None
    if len(spans) == len(lines):
        return [(round(a, 3), round(b, 3)) for a, b in spans]
    a0, b0 = spans[0][0], spans[-1][1]  # spread the lines over the spoken window, by length
    words = [max(1, len(ln["text"].split())) for ln in lines]
    out, t = [], a0
    for w in words:
        d = (b0 - a0) * w / sum(words)
        out.append((round(t, 3), round(t + d, 3)))
        t += d
    return out


def line_times(lines: list[dict], dur: float) -> list[tuple[float, float]]:
    """Spread the lines over the scene, proportional to their length (used for subtitles and fallback voices)."""
    if not lines:
        return []
    words = [max(1, len(ln["text"].split())) for ln in lines]
    avail = max(1.0, dur - LEAD_SEC - 0.4 - GAP_SEC * (len(lines) - 1))
    t, out = LEAD_SEC, []
    for w in words:
        d = avail * w / sum(words)
        out.append((t, t + d))
        t += d + GAP_SEC
    return out


def _char(speaker: str, characters: dict) -> dict:
    s = (speaker or "").strip().lower()
    return next((c for n, c in characters.items() if n.strip().lower() == s), {})


def voice_spec(speaker: str, characters: dict, cfg: dict) -> dict:
    """The character's own fixed voice (cast `tts`), else a preset for its voice type."""
    c = _char(speaker, characters)
    if (c.get("tts") or {}).get("voice"):
        return dict(c["tts"])
    fb = (cfg.get("voice") or {}).get("fallback") or {}
    vt = c.get("voice_type") or "female"
    return fb.get(vt) or fb.get("female") or {"voice": "vi-VN-HoaiMyNeural"}


def _pct(v: str) -> int:
    try:
        return int(str(v).strip().rstrip("%Hz").replace("+", ""))
    except ValueError:
        return 0


# delivery words → small rate / volume changes, so the fixed voice still sounds like it means it
EMOTION = [(("whisper", "thì thầm", "khẽ"), -8, -25), (("excited", "shout", "reo", "hào hứng", "cheer", "fast"), 8, 0),
           (("sad", "choked", "nghẹn", "tearful", "slow", "tender", "soft", "trầm"), -8, -10),
           (("laugh", "giggl", "cười"), 4, 0)]


def line_spec(line: dict, characters: dict, cfg: dict) -> dict:
    spec = voice_spec(line.get("speaker", ""), characters, cfg)
    mood = f"{line.get('delivery_en', '')} {line.get('emotion_vi', '')}".lower()
    rate, vol = _pct(spec.get("rate", "+0%")), 0
    for words, dr, dv in EMOTION:
        if any(w in mood for w in words):
            rate, vol = rate + dr, min(vol, dv) if dv else vol
            break
    return {**spec, "rate": f"{max(-40, min(40, rate)):+d}%", "volume": f"{vol:+d}%"}


async def _tts_v(text: str, spec: dict, out: Path) -> None:
    await edge_tts.Communicate(text, spec.get("voice", "vi-VN-HoaiMyNeural"), rate=spec.get("rate", "+0%"),
                               pitch=spec.get("pitch", "+0Hz"), volume=spec.get("volume", "+0%")).save(str(out))


def _tts_spec(text: str, spec: dict, out: Path) -> float:
    for attempt in range(len(TTS_BACKOFF_SEC) + 1):
        try:
            asyncio.run(_tts_v(text, spec, out))
            return _duration(out)
        except edge_tts.exceptions.EdgeTTSException as e:
            if attempt == len(TTS_BACKOFF_SEC):
                raise
            log.warning("TTS attempt %d failed (%s), retrying in %ss…", attempt + 1, e, TTS_BACKOFF_SEC[attempt])
            time.sleep(TTS_BACKOFF_SEC[attempt])
    raise AssertionError("unreachable")


def place_lines(durs: list[float], dur: float) -> tuple[list[tuple[float, float]], float]:
    """Start/end of each spoken line inside a `dur`-second scene, one after another, never overlapping.
    Returns (times, tempo): tempo > 1 means the lines must be sped up that much to fit."""
    if not durs:
        return [], 1.0
    n, tail = len(durs), 0.3
    room = dur - LEAD_SEC - tail - GAP_SEC * (n - 1)
    tempo = min(1.35, max(1.0, sum(durs) / room)) if room > 0 else 1.35
    durs = [d / tempo for d in durs]
    slack = max(0.0, room - sum(durs))
    gap = GAP_SEC + min(0.8, slack / (n + 1))
    t, out = LEAD_SEC + min(0.6, slack / (n + 1)), []
    for d in durs:
        e = min(dur - 0.05, t + d)
        out.append((round(t, 3), round(e, 3)))
        t = e + gap
    return out, tempo


def scene_voice(lines: list[dict], characters: dict, cfg: dict, dur: float, workdir: Path, tag: str) -> dict:
    """Voice every line of one scene with the speakers' fixed voices → {'path': WAV exactly `dur` long, 'times'}."""
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"voice_{tag}.wav"
    if not lines:
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}", str(out)])
        return {"path": str(out), "times": []}
    files, durs = [], []
    for k, ln in enumerate(lines):
        f = workdir / f"voice_{tag}_{k}.mp3"
        durs.append(_tts_spec(ln["text"], line_spec(ln, characters, cfg), f))
        files.append(f)
    times, tempo = place_lines(durs, dur)
    inputs, chains = [], []
    for k, (f, (start, _)) in enumerate(zip(files, times)):
        inputs += ["-i", str(f)]
        ms = int(start * 1000)
        speed = f"atempo={tempo:.3f}," if tempo > 1.001 else ""
        chains.append(f"[{k}:a]{speed}aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms}[l{k}]")
    mix = "".join(f"[l{k}]" for k in range(len(files)))
    _run(["ffmpeg", "-y", *inputs, "-filter_complex",
          ";".join(chains) + f";{mix}amix=inputs={len(files)}:normalize=0,apad,atrim=0:{dur:.3f}[a]",
          "-map", "[a]", str(out)])
    return {"path": str(out), "times": times}


def voice_hash(scenes: list[dict], characters: dict, cfg: dict, dur: float) -> str:
    specs = [[(ln["text"], line_spec(ln, characters, cfg)) for ln in s.get("lines") or []] for s in scenes]
    return hashlib.sha1(json.dumps([specs, dur], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def voice_tracks(scenes: list[dict], characters: dict, cfg: dict, dur: float, outdir: Path) -> list[dict]:
    """Fixed-voice audio for every scene, cached in `outdir` (built at approval, reused at render)."""
    outdir.mkdir(parents=True, exist_ok=True)
    h = voice_hash(scenes, characters, cfg, dur)
    meta = outdir / "voices.json"
    if meta.is_file():
        old = json.loads(meta.read_text(encoding="utf-8"))
        if old.get("hash") == h and all(Path(t["path"]).is_file() for t in old["tracks"]):
            return old["tracks"]
    tracks = [scene_voice(s.get("lines") or [], characters, cfg, dur, outdir, f"s{i}") for i, s in enumerate(scenes)]
    meta.write_text(json.dumps({"hash": h, "tracks": tracks}, ensure_ascii=False), encoding="utf-8")
    return tracks


def join_tracks(tracks: list[dict], out: Path) -> Path:
    """Concatenate scene voice WAVs (each already exactly one scene long)."""
    ins = [x for t in tracks for x in ("-i", t["path"])]
    _run(["ffmpeg", "-y", *ins, "-filter_complex",
          "".join(f"[{k}:a]" for k in range(len(tracks))) + f"concat=n={len(tracks)}:v=0:a=1[a]", "-map", "[a]",
          str(out)])
    return out


def tts_line(text: str, spec: dict, out: Path) -> float:
    return _tts_spec(text, spec, out)


def fallback_voices(lines: list[dict], characters: dict, cfg: dict, dur: float, workdir: Path, i: int) -> Path:
    """Clip without sound: voice its lines with the fixed character voices; returns a WAV exactly `dur` long."""
    return Path(scene_voice(lines, characters, cfg, dur, workdir, f"fb{i}")["path"])


def render_dialogue(script: dict, workdir: Path, cfg: dict, stock_key: str, ai, bible: dict,
                    voice_mode: str = "clip", voice_dir: Path | None = None) -> Path:
    """`script["scenes"]`: [{lines, visual_query, keep_audio, prompt_full?}], `ai`: clip source (Flow/Gemini).
    voice_mode "fixed": replace clip audio with the characters' fixed voices (cached in `voice_dir`)."""
    workdir.mkdir(parents=True, exist_ok=True)
    vid = cfg["video"]
    clip_sec = float((cfg.get("ai_video") or {}).get("max_shot_sec", 8))
    characters = {c["name"]: c for c in (bible or {}).get("characters", [])}
    fixed = None
    if voice_mode == "fixed":
        fixed = voice_tracks(script["scenes"], characters, cfg, clip_sec, voice_dir or workdir / "voices")
    used: set[str] = set()
    segments, srt, t0, idx = [], [], 0.0, 1
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,fps={FPS},"
          "format=yuv420p")

    for i, scene in enumerate(script["scenes"]):
        lines = scene.get("lines") or []
        clip, own_voice = None, False
        if ai is not None and ai.wants_ai(i, len(script["scenes"])):
            try:
                clip = ai.scene_clip(i, scene, None, clip_sec, workdir)
                own_voice = has_audio(clip) and fixed is None
            except Exception as e:
                log.warning("Clip AI cảnh %d lỗi → dùng video dự phòng: %s", i + 1, e)
                ai.note_fallback(i, str(e)[:300])
                if getattr(ai, "strict", False):
                    raise
        if clip is None and stock_key:
            try:
                clip = fetch_clip(stock_key, scene.get("visual_query", ""), vid["fallback_query"],
                                  workdir / f"c{i}.mp4", used)
            except Exception as e:
                log.warning("Pixabay lỗi cảnh %d: %s", i + 1, type(e).__name__)
        # A clip with its own voices is used whole and untouched: full length, original speed, original audio.
        dur = _duration(clip) if (clip and own_voice) else clip_sec

        seg = workdir / f"seg{i}.mkv"
        if clip and own_voice:
            v_in = ["-i", str(clip)]
        elif clip:
            v_in = ["-stream_loop", "-1", "-i", str(clip)]
        else:
            v_in = ["-f", "lavfi", "-i", f"color=c=0x3b2f2a:s={W}x{H}:r={FPS}"]
        times = line_times(lines, dur)
        up = ""
        if own_voice:
            a_in, a_map, up = [], "0:a", to_stereo(clip)
            times = speech_times(clip, lines, dur) or times
        elif fixed is not None:
            a_in, a_map, times = ["-i", fixed[i]["path"]], "1:a", [tuple(t) for t in fixed[i]["times"]]
        else:
            fb = scene_voice(lines, characters, cfg, dur, workdir, f"fb{i}")
            a_in, a_map, times = ["-i", fb["path"]], "1:a", fb["times"]
        fade = ",fade=t=in:st=0:d=0.25" if i == 0 else ""  # no dip to black between scenes: they must flow
        # audio: only resampled/converted to a common format (no volume, speed or EQ change), kept as lossless PCM
        _run(["ffmpeg", "-y", *v_in, *a_in, "-filter_complex",
              f"[0:v]{vf}{fade}[v];[{a_map}]{up}aresample={SR},aformat=sample_fmts=s16:channel_layouts=stereo,"
              f"apad,atrim=0:{dur:.3f}[a]",
              "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
              "-crf", "18", "-c:a", "pcm_s16le", str(seg)])
        segments.append(seg)

        for ln, (s, e) in zip(lines, times):
            srt += [str(idx), f"{_srt_time(t0 + s)} --> {_srt_time(t0 + e)}", ln["text"], ""]
            idx += 1
        t0 += dur

    (workdir / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    (workdir / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segments))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"], cwd=workdir)

    total = t0
    style = (f"FontName={vid['font']},FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
             "BorderStyle=1,Outline=2,Shadow=0,Bold=1,Alignment=2,MarginV=70")
    music = vid.get("background_music")
    inputs = ["-i", "joined.mkv"]
    if music and Path(music).exists():
        inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        # voices pass through at their original level; only the music is faded and dipped under them
        afilter = audio.music_filter(total, float(vid.get("music_volume", 0.15)), bool(vid.get("music_duck", True)),
                                     voice="[0:a]", music="[1:a]", rate=SR)
    else:
        afilter = f"[0:a]anull[a]"
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", f"[0:v]subtitles=subs.srt:force_style='{style}'[v];{afilter}",
          "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-movflags", "+faststart", "final.mp4"],
         cwd=workdir)
    final = workdir / "final.mp4"
    log.info("Rendered dialogue video %s (%.1fs)", final, _duration(final))
    return final


def dialogue_preview(tracks: list[dict], music: str | None, total: float, cfg: dict, out: Path) -> Path:
    """Fixed voices of every scene + background music, exactly as in the final video."""
    joined = join_tracks(tracks, out.with_name("voices_joined.wav"))
    return audio.preview(joined, music, total, cfg, out)


def music_preview(music: str | None, total: float, cfg: dict, out: Path) -> Path | None:
    """The background track as it will sit in the video (length, volume up for listening, fades)."""
    if not music or not Path(music).exists():
        return None
    fade = min(2.0, total / 4)
    _run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(music), "-af",
          f"afade=t=in:d=0.8,afade=t=out:st={max(0.0, total - fade):.2f}:d={fade:.2f}",
          "-t", f"{total:.3f}", "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
    return out
