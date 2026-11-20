"""Keep each character's voice the same across separately generated 8-second clips.

Video models give a character a slightly different voice in every clip (a bit higher, lower, louder, softer).
After the clips are joined, every spoken line is attributed to its speaker (subtitle timing), and for each line we
measure the pitch (median F0) and loudness. Each character gets ONE reference — by default the median of its own
lines in THIS video (clips that already sound alike are left untouched; `match_memory: true` instead pulls every
video to the voice remembered from earlier videos) — and only lines that clearly drift are pulled back to it: pitch
with rubberband (tempo untouched, so the pace and lip-sync stay), loudness with a plain gain. A line whose speaker
is not certain (the subtitle timing had to be guessed) is never touched. Everything else is left alone.
"""
from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SR = 16000          # analysis rate
F0_MIN, F0_MAX = 70.0, 500.0
EDGE = 0.04         # crossfade at each corrected line's edges (s)


def _pcm(path: Path, start: float, dur: float) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                          "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def measure(path: Path, start: float, end: float) -> dict | None:
    """Median F0 (Hz) of the voiced frames and their RMS (dBFS); None if nothing voiced was found."""
    x = _pcm(path, start, max(0.1, end - start))
    if x.size < SR // 5:
        return None
    win, hop = int(0.04 * SR), int(0.01 * SR)
    lo, hi = int(SR / F0_MAX), int(SR / F0_MIN)
    f0s, energies = [], []
    gate = max(1e-4, 0.1 * float(np.sqrt(np.mean(x ** 2))))
    for i in range(0, x.size - win, hop):
        fr = x[i:i + win] * np.hanning(win)
        e = float(np.sqrt(np.mean(fr ** 2)))
        if e < gate:
            continue
        n = 1 << (2 * win - 1).bit_length()
        spec = np.fft.rfft(fr, n)
        ac = np.fft.irfft(spec * np.conj(spec))[:win]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        k = int(np.argmax(seg)) + lo
        if ac[k] / ac[0] < 0.45:  # not clearly periodic (noise, rain, consonants)
            continue
        if 1 < k < len(ac) - 1:  # parabolic interpolation for sub-sample accuracy
            a, b, c = ac[k - 1], ac[k], ac[k + 1]
            k = k + 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else k
        f0s.append(SR / k)
        energies.append(e)
    if len(f0s) < 5:
        return None
    rms = float(np.sqrt(np.mean(np.square(energies))))
    return {"f0": float(np.median(f0s)), "db": 20 * math.log10(max(rms, 1e-9)), "frames": len(f0s)}


def plan(lines: list[dict], refs: dict, cfg: dict) -> tuple[list[dict], dict]:
    """lines: [{start, end, speaker, f0, db}] (measured). refs: {speaker: {f0, db}} remembered from earlier videos.
    Returns (corrections [{start, end, ratio, gain_db}], updated refs)."""
    vc = (cfg.get("voice") or {})
    max_st = float(vc.get("match_max_semitones", 4))
    max_db = float(vc.get("match_max_db", 6))
    min_st = float(vc.get("match_min_semitones", 1.0))
    min_db = float(vc.get("match_min_db", 3.0))
    stored = {k: dict(v) for k, v in (refs or {}).items()}
    refs = dict(stored) if vc.get("match_memory", False) else {}
    lines = [ln for ln in lines if ln.get("sure", True)]
    by: dict[str, list[dict]] = {}
    for ln in lines:
        if ln.get("f0"):
            by.setdefault(ln["speaker"], []).append(ln)
    for spk, ls in by.items():  # a new character's reference = the median of its lines in this video
        if spk not in refs:
            refs[spk] = {"f0": float(np.median([l["f0"] for l in ls])), "db": float(np.median([l["db"] for l in ls])),
                         "lines": len(ls)}
    out = []
    for ln in lines:
        r = refs.get(ln["speaker"])
        if not r or not ln.get("f0"):
            continue
        st = 12 * math.log2(r["f0"] / ln["f0"])
        st = max(-max_st, min(max_st, st))
        gain = max(-max_db, min(max_db, r["db"] - ln["db"]))
        if abs(st) < min_st and abs(gain) < min_db:
            continue
        out.append({"start": ln["start"], "end": ln["end"], "speaker": ln["speaker"],
                    "ratio": round(2 ** (st / 12), 4) if abs(st) >= min_st else 1.0,
                    "gain_db": round(gain, 2) if abs(gain) >= min_db else 0.0})
    return out, {**stored, **refs}


def apply(src: Path, corrections: list[dict], total: float, out: Path) -> Path:
    """Re-render the audio with the corrected lines crossfaded in; the rest of the audio is untouched."""
    if not corrections:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-c:a", "pcm_s16le", str(out)], check=True)
        return out
    gate = "+".join(f"max(0,min(1,min((t-{c['start'] - EDGE:.3f})/{EDGE},({c['end'] + EDGE:.3f}-t)/{EDGE})))"
                    for c in corrections)
    n = len(corrections)
    parts = [f"[0:a]aformat=sample_fmts=fltp,asplit={n + 1}[base]" + "".join(f"[s{k}]" for k in range(n)),
             f"[base]volume='max(0,1-({gate}))':eval=frame[keep]"]
    mix = ["[keep]"]
    for k, c in enumerate(corrections):
        a, b = max(0.0, c["start"] - EDGE), c["end"] + EDGE
        fx = []
        if c["ratio"] != 1.0:
            fx.append(f"rubberband=pitch={c['ratio']}:formant=shifted")
        if c["gain_db"]:
            fx.append(f"volume={c['gain_db']}dB")
        ms = int(round(a * 1000))
        parts.append(f"[s{k}]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,{','.join(fx) or 'anull'},"
                     f"afade=t=in:d={EDGE},afade=t=out:st={max(0.0, b - a - EDGE):.3f}:d={EDGE},"
                     f"adelay={ms}|{ms}:all=1[c{k}]")
        mix.append(f"[c{k}]")
    parts.append(f"{''.join(mix)}amix=inputs={n + 1}:normalize=0:duration=first,atrim=0:{total:.3f}[a]")
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", ";".join(parts),
                           "-map", "[a]", "-c:a", "pcm_s16le", str(out)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"voice match ffmpeg: {proc.stderr[-600:]}")
    return out


def match(src: Path, labelled: list[tuple], refs: dict, cfg: dict, total: float,
          workdir: Path) -> tuple[Path, dict, list[dict]]:
    """labelled: [(start, end, speaker[, sure])]. Measure every line, plan corrections against the characters'
    references, render. Returns (audio file, updated refs, report)."""
    lines = []
    for a, b, spk, *rest in labelled:
        m = measure(src, a, b)
        lines.append({"start": a, "end": b, "speaker": spk, "sure": bool(rest[0]) if rest else True, **(m or {})})
    corr, refs = plan(lines, refs, cfg)
    out = apply(src, corr, total, workdir / "voices_matched.wav")
    report = [{"speaker": ln["speaker"], "start": round(ln["start"], 2), "f0": round(ln.get("f0") or 0, 1),
               "db": round(ln.get("db") or -120, 1), "sure": ln["sure"]} for ln in lines]
    for c in corr:
        log.info("Giọng %s %.1f–%.1fs: cao độ ×%.3f, %+.1f dB", c["speaker"], c["start"], c["end"], c["ratio"],
                 c["gain_db"])
    return out, refs, [*report, *({"fix": c} for c in corr)]
