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

OUTRO_COLOR = "0x2a1f1a"   # warm dark the video fades into
SR = 48000
LEAD_SEC = 0.3             # each scene's narration starts this long after the cut


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout or "{}"
    return bool(json.loads(out).get("streams"))


def last_frame_of(src: Path, dst: Path) -> Path | None:
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.12", "-i", str(src), "-frames:v", "1",
                        "-q:v", "2", str(dst)], capture_output=True, text=True)
    return dst if p.returncode == 0 and dst.is_file() and dst.stat().st_size else None


def make_outro(last_seg: Path, workdir: Path, cfg: dict) -> Path | None:
    """Ending: the last frame held, a very slow ~3% zoom-out (over a blurred copy, so nothing jumps), optional
    centred text fading in/out, and a fade to warm dark in the final 1.2 s. Silent audio (music plays on top)."""
    vid = cfg.get("video", {}) or {}
    sec = float(vid.get("outro_sec", 3.5) or 0)
    if sec <= 0:
        return None
    frame = last_frame_of(last_seg, workdir / "outro_frame.jpg")
    if not frame:
        return None
    fade = min(1.2, sec / 2)
    z = 0.03
    g = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,split=2[bg][fg];"
         f"[bg]boxblur=20:2,eq=brightness=-0.06[bgb];"
         f"[fg]scale=w='trunc({W}*(1-{z}*t/{sec})/2)*2':h='trunc({H}*(1-{z}*t/{sec})/2)*2':eval=frame[fgs];"
         f"[bgb][fgs]overlay=x='(W-w)/2':y='(H-h)/2':eval=frame,fps={FPS},format=yuv420p")
    text = str(vid.get("outro_text") or "").strip()
    if text:
        tf = workdir / "outro_text.txt"
        tf.write_text(text, encoding="utf-8")
        t_in, t_out = 0.3, max(0.8, sec - fade - 0.1)
        alpha = f"if(lt(t,{t_in}),0,if(lt(t,{t_in + 0.6}),(t-{t_in})/0.6,if(lt(t,{t_out - 0.5}),1,max(0,({t_out}-t)/0.5))))"
        g += (f",drawtext=textfile='{tf.name}':font='{vid.get('font', 'DejaVu Sans')}':fontsize=64:fontcolor=white:"
              f"borderw=4:bordercolor=black@0.5:x=(w-text_w)/2:y=(h-text_h)/2:alpha='{alpha}'")
    g += f",fade=t=out:st={sec - fade:.3f}:d={fade:.3f}:color={OUTRO_COLOR}[v]"
    out = workdir / "outro.mkv"
    _run(["ffmpeg", "-y", "-loop", "1", "-t", f"{sec:.3f}", "-i", frame.name, "-f", "lavfi", "-i",
          f"anullsrc=r={SR}:cl=stereo", "-filter_complex", g, "-map", "[v]", "-map", "1:a", "-t", f"{sec:.3f}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
          out.name], cwd=workdir)
    return out


def subtitle_style(vid: dict) -> str:
    return (f"FontName={vid['font']},FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
            "BorderStyle=1,Outline=2,Shadow=0,Bold=1,Alignment=2,MarginV=70")


def final_mix(workdir: Path, cfg: dict, total: float, joined: str, voice_file: str | None,
              music: str | None, spans: list, ambient_from_joined: bool, voice_from_joined: bool = False) -> dict:
    """Burn subtitles and mix the soundtrack in one pass; also writes voice/music stems and checks the music."""
    from . import audio
    st = audio.music_settings(cfg)
    ins = ["-i", joined]
    k = 1
    vl = "[0:a]" if voice_from_joined else None
    if voice_file:
        ins += ["-i", voice_file]
        vl, k = f"[{k}:a]", k + 1
    ml, vol = None, None
    if music:
        bed = audio.music_bed(music, total, workdir / "music_bed.wav", st["xfade"])
        ins += ["-i", str(bed)]
        ml, k = f"[{k}:a]", k + 1
        vref = voice_file or (str(workdir / joined) if voice_from_joined else None)
        vol = audio.auto_music_volume(cfg, vref, bed, spans)
    amb = "[0:a]" if ambient_from_joined else None
    vpre = audio.stereo_prefix(voice_file) if voice_file else ""
    graph = audio.mix_graph(total, cfg, vl, ml, spans, amb, voice_pre=vpre, stems=False, volume=vol)
    _run(["ffmpeg", "-y", *ins, "-filter_complex",
          f"[0:v]subtitles=subs.srt:force_style='{subtitle_style(cfg['video'])}'[v];{graph}",
          "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-movflags", "+faststart",
          "final.mp4"], cwd=workdir)
    audio.write_stems(ins, audio.mix_graph(total, cfg, vl, ml, spans, amb, voice_pre=vpre, volume=vol), total, workdir,
                      cwd=workdir)
    warnings = []
    if not music:
        warnings.append("chưa có nhạc nền — gửi file mp3 cho bot để thêm vào thư viện (/nhac)")
    else:
        warnings += audio.check_music(workdir / "stem_music.wav", total, cfg)
    report = {"total": round(total, 3), "warnings": warnings, "spans": spans, "music_gain": vol,
              "music_windows": audio.window_levels(workdir / "stem_music.wav", total) if music else []}
    (workdir / "mix_report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def render(script: dict, workdir: Path, cfg: dict, stock_key: str, ai=None, narration: dict | None = None) -> Path:
    """Narrated video. `ai` is an optional clip source; scenes without an AI clip use Pixabay (or a warm background).

    keep_full_clip (default): an AI clip is never cut — each scene lasts max(clip, its narration), the clip is only
    slowed (max_stretch) when the narration is longer; the narration is split per scene and each piece starts at
    the beginning of its own scene, subtitles move with it, and the clip's ambient sound stays under the voice.
    `narration` = meta from audio.narration_track() with "path" set; generated here if omitted.
    """
    from . import audio  # local import: audio.py imports helpers from this module

    workdir.mkdir(parents=True, exist_ok=True)
    voice, vid = cfg["voice"], cfg["video"]
    keep_full = bool(vid.get("keep_full_clip", True))
    if narration is None:
        narration = audio.narration_track([s["narration"] for s in script["scenes"]],
                                          script.get("voice") or voice["name"], voice["rate"], workdir)
        narration["path"] = str(workdir / narration["file"])
    n = len(script["scenes"])
    sc = narration["scenes"]
    cut_durs = audio.scene_durations(narration)
    # voice piece of each scene in the continuous narration: [first word − 0.05, last word + 0.25] (never into the next)
    pieces = []
    for k in range(n):
        a = max(0.0, sc[k]["start"] - 0.05)
        nxt = sc[k + 1]["start"] if k + 1 < n else narration["duration"]
        b = min(max(sc[k]["end"] + 0.25, a), max(nxt - 0.02, a))
        pieces.append((a, b))
    need = [(b - a) + LEAD_SEC + PAD_SEC for a, b in pieces] if keep_full else cut_durs
    if ai is not None:
        ai.keep_full = keep_full
    used: set[str] = set()
    segments: list[Path] = []
    durs: list[float] = []
    look = "eq=saturation=0.9:brightness=0.02"  # soft, warm nostalgic look
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,fps={FPS},{look}"

    for i, scene in enumerate(script["scenes"]):
        clip, is_ai = None, False
        if ai is not None and ai.wants_ai(i, n):
            try:
                clip = ai.scene_clip(i, scene, script.get("visual_bible"), need[i], workdir)
                is_ai = True
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
        if clip and is_ai and keep_full:
            dur = max(_duration(clip), need[i])
            v_in = ["-i", str(clip)]
            vf = f"{base},tpad=stop_mode=clone:stop_duration={dur:.3f}"  # hold the last frame, never loop/cut
        elif clip:
            dur = need[i]
            v_in = ["-stream_loop", "-1", "-i", str(clip)]
            vf = base
        else:  # no clip → warm solid background
            dur = need[i]
            v_in = ["-f", "lavfi", "-i", f"color=c=0x3b2f2a:s={W}x{H}:r={FPS}"]
            vf = base
        if i == 0:
            vf += ",fade=t=in:st=0:d=0.3"
        amb = clip and is_ai and has_audio(clip)
        a_in = [] if amb else ["-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo"]
        a_map = "0:a" if amb else "1:a"
        seg = workdir / f"seg{i}.mkv"
        _run(["ffmpeg", "-y", *v_in, *a_in, "-filter_complex",
              f"[0:v]{vf}[v];[{a_map}]aresample={SR},aformat=sample_fmts=s16:channel_layouts=stereo,apad,"
              f"atrim=0:{dur:.3f}[a]", "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
              str(seg)])
        segments.append(seg)
        durs.append(dur)

    outro = make_outro(segments[-1], workdir, cfg)
    if outro:
        segments.append(outro)
    starts = [sum(durs[:k]) for k in range(n)]
    body = sum(durs)
    total = body + (_duration(outro) if outro else 0.0)

    # voice timeline + subtitles
    cues = narration["cues"]
    if keep_full:
        npath = str(Path(narration["path"]).resolve())
        up = audio.stereo_prefix(npath)
        ins, chains = [], []
        for k, (a, b) in enumerate(pieces):
            ins += ["-i", npath]
            ms = int(round((starts[k] + LEAD_SEC) * 1000))
            chains.append(f"[{k}:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,{up}aresample={SR},"
                          f"aformat=sample_fmts=fltp:channel_layouts=stereo,adelay={ms}|{ms}[p{k}]")
        mixin = "".join(f"[p{k}]" for k in range(n))
        voice_file = workdir / "voice_timeline.wav"
        _run(["ffmpeg", "-y", *ins, "-filter_complex",
              ";".join(chains) + f";{mixin}amix=inputs={n}:normalize=0,apad,atrim=0:{total:.3f}[a]",
              "-map", "[a]", "-c:a", "pcm_s16le", str(voice_file)])
        shift = []
        for c in cues:  # move each cue with its scene's narration piece
            k = max([j for j in range(n) if c["start"] >= pieces[j][0] - 0.01] or [0])
            off = starts[k] + LEAD_SEC - pieces[k][0]
            shift.append({**c, "start": c["start"] + off, "end": c["end"] + off})
        cues = shift
        spans = [(starts[k] + LEAD_SEC, starts[k] + LEAD_SEC + (b - a)) for k, (a, b) in enumerate(pieces)]
    else:
        voice_file = Path(narration["path"]).resolve()
        spans = [(s["start"], s["end"]) for s in sc]
    srt = []
    for k, c in enumerate(cues, 1):
        srt += [str(k), f"{_srt_time(c['start'])} --> {_srt_time(max(c['end'], c['start'] + 0.4))}", c["text"], ""]
    (workdir / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    (workdir / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segments))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"], cwd=workdir)
    (workdir / "timeline.json").write_text(json.dumps({"starts": starts, "durs": durs, "total": total,
                                                       "cues": cues}, ensure_ascii=False), encoding="utf-8")

    music = vid.get("background_music")
    final_mix(workdir, cfg, total, "joined.mkv", str(voice_file),
              str(Path(music).resolve()) if music and Path(music).exists() else None, spans,
              ambient_from_joined=True)
    final = workdir / "final.mp4"
    log.info("Rendered %s (%.1fs)", final, _duration(final))
    return final
