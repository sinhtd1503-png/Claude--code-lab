"""AI video source: Gemini Omni Flash via the Gemini API (successor of Veo on the Gemini API).

The API key is read by the SDK from the GEMINI_API_KEY environment variable (loaded from .env);
it is never passed around, printed or logged by this module.

For each scene: split into shots of at most `max_shot_sec`, build a prompt per shot that repeats the
same character/setting/style description (for consistency), generate, poll, download, and concatenate.
Every generated shot is recorded in the `ai_usage` table (daily budget) and every run is appended to
data/logs/ai_cost.jsonl with its estimated cost.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import math
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import DB

log = logging.getLogger(__name__)

# Approximate video output tokens per second, from Google's pricing page (Nov 2026).
# Used only when the API does not return usage numbers.
EST_TOKENS_PER_SEC = {"360p": 1931, "720p": 5834, "1080p": 8688, "4k": 17376}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, day TEXT, content_id INTEGER, scene INTEGER, shot INTEGER,
    seconds REAL, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    status TEXT, interaction_id TEXT
);
"""


class AIBudgetExceeded(RuntimeError):
    pass


class AIVideoSource:
    def __init__(self, cfg: dict, db: DB, data_dir: Path, timezone: str):
        self.cfg = cfg
        self.db = db
        self.tz = ZoneInfo(timezone)
        self.log_path = data_dir / "logs" / "ai_cost.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.db.conn.executescript(SCHEMA)
        self._client = None
        self.run: dict | None = None

    # ---------- config helpers ----------
    @property
    def client(self):
        if self._client is None:
            from google import genai  # reads GEMINI_API_KEY from the environment
            self._client = genai.Client()
        return self._client

    def wants_ai(self, scene_index: int, n_scenes: int) -> bool:
        if not self.cfg.get("enabled"):
            return False
        sel = self.cfg.get("scenes", "none")
        if sel == "all":
            return True
        if sel == "none":
            return False
        if sel == "first":
            return scene_index == 0
        if isinstance(sel, list):
            # 1-based in config for readability: [1, 3] = scene 1 and scene 3
            return (scene_index + 1) in sel
        raise ValueError(f"ai_video.scenes không hợp lệ: {sel!r}")

    def _today(self) -> str:
        return dt.datetime.now(self.tz).date().isoformat()

    def used_today(self) -> float:
        r = self.db.conn.execute(
            "SELECT COALESCE(SUM(seconds),0) s FROM ai_usage WHERE day=? AND status='ok'", (self._today(),)
        ).fetchone()
        return float(r["s"])

    def remaining_today(self) -> float:
        return max(0.0, self.cfg["daily_limit_sec"] - self.used_today())

    def _cost(self, seconds: float, in_tok: int | None, out_tok: int | None) -> tuple[float, int, int]:
        res = self.cfg["resolution"]
        out_tok = out_tok or int(seconds * EST_TOKENS_PER_SEC.get(res, EST_TOKENS_PER_SEC["720p"]))
        in_tok = in_tok or 0
        cost = (out_tok * self.cfg["price_per_1m_output_tokens"]
                + in_tok * self.cfg["price_per_1m_input_tokens"]) / 1e6
        return round(cost, 4), in_tok, out_tok

    # ---------- run bookkeeping (one run = one rendered video) ----------
    def start_run(self, content_id: int | None) -> None:
        self.run = {"content_id": content_id, "started": dt.datetime.now(self.tz).isoformat(timespec="seconds"),
                    "model": self.cfg["model"], "resolution": self.cfg["resolution"],
                    "shots": [], "ai_seconds": 0.0, "est_cost_usd": 0.0, "fallbacks": []}

    def finish_run(self) -> dict | None:
        if not self.run:
            return None
        run, self.run = self.run, None
        run["finished"] = dt.datetime.now(self.tz).isoformat(timespec="seconds")
        run["day_total_seconds"] = round(self.used_today(), 1)
        run["day_limit_seconds"] = self.cfg["daily_limit_sec"]
        run["est_cost_usd"] = round(run["est_cost_usd"], 4)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
        log.info("AI video run: %.0fs, est $%.4f (today %.0f/%ss) → %s", run["ai_seconds"], run["est_cost_usd"],
                 run["day_total_seconds"], run["day_limit_seconds"], self.log_path)
        return run

    def note_fallback(self, scene_index: int, reason: str) -> None:
        if self.run is not None:
            self.run["fallbacks"].append({"scene": scene_index + 1, "reason": reason})

    # ---------- prompts ----------
    @staticmethod
    def plan_shots(duration: float, max_shot: int, min_shot: int = 3) -> list[int]:
        n = max(1, math.ceil(duration / max_shot))
        each = min(max_shot, max(min_shot, math.ceil(duration / n)))
        return [each] * n

    @staticmethod
    def build_prompt(bible: dict | None, description: str) -> str:
        bible = bible or {}
        parts = [
            "Vertical 9:16 cinematic shot for a short nostalgic family story.",
            f"Visual style (keep identical in every shot): {bible.get('style', 'warm, soft natural light, gentle film grain')}.",
        ]
        if bible.get("characters"):
            parts.append(f"Characters (same appearance, clothing and age in every shot): {bible['characters']}.")
        if bible.get("setting"):
            parts.append(f"Setting (same place in every shot): {bible['setting']}.")
        parts.append(f"Shot: {description}")
        parts.append("Slow, gentle camera movement. Do not show any text, captions, logos or watermarks. "
                     "No dialogue, no lip-sync; ambient sound only.")
        return "\n".join(parts)

    # ---------- generation ----------
    def _generate_shot(self, prompt: str, seconds: int, out: Path) -> tuple[str, int | None, int | None]:
        c = self.cfg
        inter = self.client.interactions.create(
            model=c["model"],
            input=prompt,
            response_format={"type": "video", "aspect_ratio": "9:16",
                             "resolution": c["resolution"], "duration": f"{seconds}s"},
            background=True,
        )
        deadline = time.time() + c.get("timeout_sec", 900)
        while inter.status in ("queued", "in_progress"):
            if time.time() > deadline:
                raise TimeoutError(f"AI shot quá {c.get('timeout_sec', 900)}s chưa xong")
            time.sleep(c.get("poll_interval_sec", 10))
            inter = self.client.interactions.get(inter.id)
        if inter.status != "completed" or not inter.output_video:
            errs = "; ".join(str(getattr(e, "message", e)) for e in (inter.errors or []))
            raise RuntimeError(f"AI shot thất bại (status={inter.status}) {errs}".strip())

        v = inter.output_video
        if v.data:
            out.write_bytes(base64.b64decode(v.data))
        elif v.uri:
            out.write_bytes(self.client.files.download(file=v.uri))
        else:
            raise RuntimeError("AI shot không trả về dữ liệu video")
        u = inter.usage
        return inter.id, (u.total_input_tokens if u else None), (u.total_output_tokens if u else None)

    def scene_clip(self, scene_index: int, scene: dict, bible: dict | None,
                   duration: float, workdir: Path) -> Path:
        """Generate all shots for one scene and concatenate them (video only). Raises on failure/budget."""
        shots = self.plan_shots(duration, self.cfg["max_shot_sec"])
        need = sum(shots)
        if need > self.remaining_today():
            raise AIBudgetExceeded(f"cần {need}s, hôm nay còn {self.remaining_today():.0f}s")

        descriptions = scene.get("shots") or [scene.get("visual_query", "")]
        files: list[Path] = []
        for k, sec in enumerate(shots):
            desc = descriptions[k % len(descriptions)]
            prompt = self.build_prompt(bible, desc)
            out = workdir / f"ai_s{scene_index}_{k}.mp4"
            log.info("AI shot scene %d/%d (%ds): %s", scene_index + 1, k + 1, sec, desc[:80])
            t0 = time.time()
            try:
                iid, in_tok, out_tok = self._generate_shot(prompt, sec, out)
            except Exception as e:
                self._record(scene_index, k, 0, 0, 0, 0.0, "failed", None)
                raise RuntimeError(str(e)[:500]) from None
            cost, in_tok, out_tok = self._cost(sec, in_tok, out_tok)
            self._record(scene_index, k, sec, in_tok, out_tok, cost, "ok", iid)
            if self.run is not None:
                self.run["shots"].append({"scene": scene_index + 1, "shot": k + 1, "seconds": sec,
                                          "input_tokens": in_tok, "output_tokens": out_tok,
                                          "est_cost_usd": cost, "gen_time_sec": round(time.time() - t0),
                                          "prompt": prompt})
                self.run["ai_seconds"] += sec
                self.run["est_cost_usd"] += cost
            files.append(out)

        if len(files) == 1:
            return files[0]
        lst = workdir / f"ai_s{scene_index}.txt"
        lst.write_text("".join(f"file '{f.name}'\n" for f in files))
        joined = workdir / f"ai_s{scene_index}.mp4"
        # re-encode while joining: shots may differ slightly in timing/fps
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst.name, "-an",
             "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", joined.name],
            cwd=workdir, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ghép shot AI lỗi: {proc.stderr[-800:]}")
        return joined

    def _record(self, scene, shot, seconds, in_tok, out_tok, cost, status, iid) -> None:
        self.db.conn.execute(
            "INSERT INTO ai_usage (ts, day, content_id, scene, shot, seconds, input_tokens, output_tokens,"
            " cost_usd, status, interaction_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), self._today(), (self.run or {}).get("content_id"), scene + 1, shot + 1,
             seconds, in_tok, out_tok, cost, status, iid))
        self.db.conn.commit()
