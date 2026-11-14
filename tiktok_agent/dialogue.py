"""Dialogue videos: characters talk to each other inside the AI clips; no narrator.

The AI video model (Google Flow / Gemini) is asked to voice the dialogue with lip-sync, so each clip already
carries the characters' voices and ambient sound. This module keeps that audio, adds light background music
that dips under the voices, and burns subtitles from the script's dialogue lines.

If a clip comes back without sound (or a stock fallback is used), each line is voiced by edge-tts with a
per-character voice (pitch/rate tuned for child / adult / elderly) so the video is never silent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path

import edge_tts

from . import audio
from .video import FPS, H, TTS_BACKOFF_SEC, W, _duration, _run, _srt_time, fetch_clip

log = logging.getLogger(__name__)

LEAD_SEC = 0.5   # first line starts a little after the cut
GAP_SEC = 0.3    # pause between lines


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout or "{}"
    return bool(json.loads(out).get("streams"))


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


async def _tts(text: str, voice: str, rate: str, pitch: str, out: Path) -> None:
    await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out))


def tts_line(text: str, spec: dict, out: Path) -> float:
    for attempt in range(len(TTS_BACKOFF_SEC) + 1):
        try:
            asyncio.run(_tts(text, spec.get("voice", "vi-VN-HoaiMyNeural"), spec.get("rate", "+0%"),
                             spec.get("pitch", "+0Hz"), out))
            return _duration(out)
        except edge_tts.exceptions.EdgeTTSException as e:
            if attempt == len(TTS_BACKOFF_SEC):
                raise
            log.warning("TTS attempt %d failed (%s), retrying in %ss…", attempt + 1, e, TTS_BACKOFF_SEC[attempt])
            time.sleep(TTS_BACKOFF_SEC[attempt])
    raise AssertionError("unreachable")


def voice_spec(speaker: str, characters: dict, cfg: dict) -> dict:
    fb = (cfg.get("voice") or {}).get("fallback") or {}
    vt = (characters.get(speaker) or {}).get("voice_type") or "female"
    return fb.get(vt) or fb.get("female") or {"voice": "vi-VN-HoaiMyNeural"}


def fallback_voices(lines: list[dict], characters: dict, cfg: dict, dur: float, workdir: Path, i: int) -> Path:
    """Voice each line with edge-tts and place it at its slot; returns a WAV exactly `dur` long."""
    out = workdir / f"dlg{i}.wav"
    if not lines:
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}", str(out)])
        return out
    inputs, chains = [], []
    for k, (ln, (start, _)) in enumerate(zip(lines, line_times(lines, dur))):
        f = workdir / f"dlg{i}_{k}.mp3"
        tts_line(ln["text"], voice_spec(ln["speaker"], characters, cfg), f)
        inputs += ["-i", str(f)]
        ms = int(start * 1000)
        chains.append(f"[{k}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms}[l{k}]")
    mix = "".join(f"[l{k}]" for k in range(len(lines)))
    _run(["ffmpeg", "-y", *inputs, "-filter_complex",
          ";".join(chains) + f";{mix}amix=inputs={len(lines)}:normalize=0,apad,atrim=0:{dur:.3f}[a]",
          "-map", "[a]", str(out)])
    return out


def render_dialogue(script: dict, workdir: Path, cfg: dict, stock_key: str, ai, bible: dict) -> Path:
    """`script["scenes"]`: [{lines, visual_query, keep_audio, prompt_full?}], `ai`: clip source (Flow/Gemini)."""
    workdir.mkdir(parents=True, exist_ok=True)
    vid = cfg["video"]
    clip_sec = float((cfg.get("ai_video") or {}).get("max_shot_sec", 8))
    characters = {c["name"]: c for c in (bible or {}).get("characters", [])}
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
                own_voice = has_audio(clip)
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
        dur = min(_duration(clip), clip_sec + 0.5) if (clip and own_voice) else clip_sec

        seg = workdir / f"seg{i}.mp4"
        v_in = ["-stream_loop", "-1", "-i", str(clip)] if clip else \
            ["-f", "lavfi", "-i", f"color=c=0x3b2f2a:s={W}x{H}:r={FPS}"]
        if own_voice:
            a_in, a_map = [], "0:a"
        else:
            a_in, a_map = ["-i", str(fallback_voices(lines, characters, cfg, dur, workdir, i))], "1:a"
        _run(["ffmpeg", "-y", *v_in, *a_in, "-filter_complex",
              f"[0:v]{vf},fade=t=in:st=0:d=0.25[v];[{a_map}]aresample=44100,aformat=channel_layouts=stereo,"
              f"apad,atrim=0:{dur:.3f}[a]",
              "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
              "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(seg)])
        segments.append(seg)

        for ln, (s, e) in zip(lines, line_times(lines, dur)):
            srt += [str(idx), f"{_srt_time(t0 + s)} --> {_srt_time(t0 + e)}", ln["text"], ""]
            idx += 1
        t0 += dur

    (workdir / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    (workdir / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segments))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=workdir)

    total = t0
    style = (f"FontName={vid['font']},FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
             "BorderStyle=1,Outline=2,Shadow=0,Bold=1,Alignment=2,MarginV=70")
    music = vid.get("background_music")
    inputs = ["-i", "joined.mp4"]
    if music and Path(music).exists():
        inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        afilter = audio.music_filter(total, float(vid.get("music_volume", 0.15)), bool(vid.get("music_duck", True)),
                                     voice="[0:a]", music="[1:a]")
    else:
        afilter = f"[0:a]aresample=44100,aformat=channel_layouts=stereo,apad,atrim=0:{total:.3f}[a]"
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", f"[0:v]subtitles=subs.srt:force_style='{style}'[v];{afilter}",
          "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "final.mp4"], cwd=workdir)
    final = workdir / "final.mp4"
    log.info("Rendered dialogue video %s (%.1fs)", final, _duration(final))
    return final


def music_preview(music: str | None, total: float, cfg: dict, out: Path) -> Path | None:
    """The background track as it will sit in the video (length, volume up for listening, fades)."""
    if not music or not Path(music).exists():
        return None
    fade = min(2.0, total / 4)
    _run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(music), "-af",
          f"afade=t=in:d=0.8,afade=t=out:st={max(0.0, total - fade):.2f}:d={fade:.2f}",
          "-t", f"{total:.3f}", "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
    return out
