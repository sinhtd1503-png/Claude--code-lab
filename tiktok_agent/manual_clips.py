"""Free AI video via Google Flow (manual step): the agent writes one prompt per shot, the channel owner
generates each clip in Google Flow (free daily credits), sends it back to the Telegram bot, and the
agent stitches the clips in place of stock footage.

No Google API or browser automation is used: Flow has no API, and automating its web UI would break
Google's terms and risk the account.

State for each content lives in data/clips/<content_id>/manifest.json:
  {"shots": [{"scene": 1, "shot": 1, "seconds": 5, "prompt": "...", "file": null, "tg_message_id": 123}]}
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai_video import AIVideoSource
from .db import DB

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30


class ManualClipSource:
    """Same interface as AIVideoSource for video.render(), but clips come from the user."""

    provider = "google_flow_manual"

    def __init__(self, cfg: dict, db: DB, data_dir: Path, timezone: str):
        self.cfg = cfg
        self.manual = cfg.get("manual", {})
        self.db = db
        self.tz = ZoneInfo(timezone)
        self.clips_root = data_dir / "clips"
        self.log_path = data_dir / "logs" / "ai_cost.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.content_id: int | None = None
        self.run: dict | None = None

    # ---------- shared helpers ----------
    wants_ai = AIVideoSource.wants_ai

    def _today(self) -> str:
        return dt.datetime.now(self.tz).date().isoformat()

    def manifest_path(self, cid: int) -> Path:
        return self.clips_root / str(cid) / "manifest.json"

    def load(self, cid: int) -> dict | None:
        p = self.manifest_path(cid)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save(self, cid: int, manifest: dict) -> None:
        p = self.manifest_path(cid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def shots_requested_today(self) -> int:
        n = 0
        for p in self.clips_root.glob("*/manifest.json"):
            m = json.loads(p.read_text(encoding="utf-8"))
            if m.get("day") == self._today():
                n += len(m["shots"])
        return n

    # ---------- step 1: plan shots + prompts (before rendering) ----------
    def prepare(self, cid: int, script: dict, durations: list[float]) -> dict | None:
        """Create the shot list for AI scenes, within the free daily allowance. None = no AI shots."""
        old = self.load(cid)
        if old and old.get("day") == self._today():
            self.manifest_path(cid).unlink()  # a regeneration replaces today's request for this content
        remaining = max(0, self.manual.get("max_shots_per_day", 4) - self.shots_requested_today())
        bible = script.get("visual_bible")
        shots = []
        for i, (scene, dur) in enumerate(zip(script["scenes"], durations)):
            if not self.wants_ai(i, len(script["scenes"])):
                continue
            if self.manual.get("one_shot_per_scene", True):
                plan = [self.cfg["max_shot_sec"]]  # one free 8s clip per scene; render fits it to the voice-over
            else:
                plan = AIVideoSource.plan_shots(dur, self.cfg["max_shot_sec"])
            if len(plan) > remaining - len(shots):
                log.info("Hết lượt Flow miễn phí hôm nay → cảnh %d dùng Pixabay", i + 1)
                continue
            descs = scene.get("shots") or [scene.get("visual_query", "")]
            for k, sec in enumerate(plan):
                shots.append({"scene": i + 1, "shot": k + 1, "seconds": sec,
                              "prompt": AIVideoSource.build_prompt(bible, descs[k % len(descs)]),
                              "file": None, "tg_message_id": None})
        if not shots:
            return None
        manifest = {"content_id": cid, "day": self._today(), "shots": shots}
        self.save(cid, manifest)
        return manifest

    def attach(self, cid: int, index: int, src: Path) -> dict:
        m = self.load(cid)
        shot = m["shots"][index]
        dst = self.manifest_path(cid).parent / f"s{shot['scene']}_{shot['shot']}{src.suffix or '.mp4'}"
        src.replace(dst)
        shot["file"] = str(dst)
        self.save(cid, m)
        return m

    @staticmethod
    def missing(m: dict) -> list[int]:
        return [i for i, s in enumerate(m["shots"]) if not s["file"]]

    def find_by_message(self, tg_message_id: int) -> tuple[int, int] | None:
        for p in self.clips_root.glob("*/manifest.json"):
            m = json.loads(p.read_text(encoding="utf-8"))
            for i, s in enumerate(m["shots"]):
                if s.get("tg_message_id") == tg_message_id:
                    return m["content_id"], i
        return None

    # ---------- step 2: render-time interface used by video.render ----------
    def start_run(self, content_id: int | None) -> None:
        self.content_id = content_id
        self.run = {"content_id": content_id, "provider": self.provider,
                    "started": dt.datetime.now(self.tz).isoformat(timespec="seconds"),
                    "shots": [], "ai_seconds": 0.0, "est_cost_usd": 0.0, "fallbacks": []}

    def note_fallback(self, scene_index: int, reason: str) -> None:
        if self.run is not None:
            self.run["fallbacks"].append({"scene": scene_index + 1, "reason": reason})

    def scene_clip(self, scene_index: int, scene: dict, bible: dict | None, duration: float, workdir: Path) -> Path:
        m = self.load(self.content_id) if self.content_id else None
        shots = [s for s in (m or {}).get("shots", []) if s["scene"] == scene_index + 1]
        if not shots:
            raise RuntimeError("cảnh này không được yêu cầu clip AI")
        if any(not s["file"] for s in shots):
            raise RuntimeError("chưa nhận đủ clip từ Google Flow")
        parts = []
        base_vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                   f"crop={W}:{H},setsar=1,fps={FPS},format=yuv420p")
        if scene.get("keep_audio"):
            # Dialogue clip: keep the characters' voices; never slow it down (that would distort the speech).
            s = shots[0]
            out = workdir / f"flow_s{s['scene']}_dlg.mp4"
            _ffmpeg(["-i", s["file"], "-t", f"{duration + 0.5:.3f}", "-vf", base_vf, "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out)])
            if self.run is not None:
                self.run["shots"].append({"scene": s["scene"], "shot": 1, "seconds": round(duration, 2)})
                self.run["ai_seconds"] += duration
            return out
        if len(shots) == 1:
            # One clip for the whole scene: if the voice-over is longer, slow the clip down slightly
            # (up to max_stretch) instead of visibly looping it; if shorter, just cut it.
            s = shots[0]
            clip_len = _probe_duration(Path(s["file"]))
            factor = min(max(1.0, duration / clip_len), self.manual.get("max_stretch", 1.25))
            out = workdir / f"flow_s{s['scene']}_1.mp4"
            _ffmpeg(["-i", s["file"], "-an", "-vf", f"setpts={factor:.4f}*PTS,{base_vf}", "-t", f"{duration:.3f}",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
            if self.run is not None:
                self.run["shots"].append({"scene": s["scene"], "shot": 1, "seconds": round(duration, 2),
                                          "clip_sec": round(clip_len, 2), "slowdown": round(factor, 3)})
                self.run["ai_seconds"] += duration
            return out
        for s in shots:  # several clips: trim each to its planned length and normalize format
            out = workdir / f"flow_s{s['scene']}_{s['shot']}.mp4"
            _ffmpeg(["-i", s["file"], "-t", str(s["seconds"]), "-an", "-vf", base_vf,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
            parts.append(out)
            if self.run is not None:
                self.run["shots"].append({"scene": s["scene"], "shot": s["shot"], "seconds": s["seconds"]})
                self.run["ai_seconds"] += s["seconds"]
        if len(parts) == 1:
            return parts[0]
        lst = workdir / f"flow_s{scene_index}.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        joined = workdir / f"flow_s{scene_index}.mp4"
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", joined.name], cwd=workdir)
        return joined

    def finish_run(self) -> dict | None:
        if not self.run:
            return None
        run, self.run = self.run, None
        run["finished"] = dt.datetime.now(self.tz).isoformat(timespec="seconds")
        run["ai_seconds"] = round(run["ai_seconds"], 1)
        run["flow_credits_est"] = len(run["shots"]) * self.manual.get("credits_per_shot", 12)
        run["flow_daily_credits"] = self.manual.get("daily_credits", 50)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
        log.info("Flow run: %d shots, %.0fs, $0 (≈%d Flow credits)", len(run["shots"]), run["ai_seconds"],
                 run["flow_credits_est"])
        return run


def _probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def _ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg lỗi khi xử lý clip Flow: {proc.stderr[-600:]}")
