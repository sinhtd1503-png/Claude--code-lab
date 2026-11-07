"""Orchestration: scrape → analyze → script → render. Publishing happens after Telegram approval."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import os

from . import content, scraper, video
from .ai_video import AIVideoSource
from .config import Settings
from .db import DB
from .publisher import TikTokPublisher

log = logging.getLogger(__name__)

REPORT_KEY = "last_trend_report"


class Pipeline:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = DB(settings.data_dir / "agent.db")
        self.publisher = (
            TikTokPublisher(settings.tiktok_client_key, settings.tiktok_client_secret,
                            settings.tiktok_redirect_uri, self.db)
            if settings.tiktok_client_key else None
        )
        ai_cfg = settings.raw.get("ai_video") or {}
        self.ai = None
        if ai_cfg.get("enabled"):
            if os.getenv("GEMINI_API_KEY"):
                self.ai = AIVideoSource(ai_cfg, self.db, settings.data_dir, settings["schedule"]["timezone"])
            else:
                log.warning("ai_video.enabled nhưng thiếu GEMINI_API_KEY → chỉ dùng Pixabay")

    # ---- step 1+2: research ----
    def research(self) -> dict:
        cfg = self.s["scraper"]
        competitors = [c for c in self.s["competitors"] if c and not c.startswith("username_doi_thu")]
        if self.s.apify_token and competitors:
            vids = scraper.scrape_profiles(self.s.apify_token, competitors, cfg["videos_per_profile"])
            self.db.upsert_videos(vids)
        else:
            log.warning("Bỏ qua scrape (thiếu APIFY_TOKEN hoặc chưa cấu hình competitors)")

        since = int(time.time()) - cfg["lookback_days"] * 86400
        top = self.db.top_videos(since, cfg["top_n_for_analysis"])
        report = content.analyze_trends(self.s["niche"], top, self.db.recent_titles())
        data = report.model_dump()
        self.db.kv_set(REPORT_KEY, data)
        return data

    # ---- step 3+4: script + video ----
    def generate(self, idea: dict, report: dict | None = None, *,
                 feedback: str | None = None, previous_script: dict | None = None,
                 content_id: int | None = None) -> int:
        report = report if report is not None else self.db.kv_get(REPORT_KEY)
        cid = content_id or self.db.create_content(idea)
        self.db.update_content(cid, status="generating", error=None)
        try:
            script = content.write_script(
                self.s["niche"], idea, report, self.s["content"]["target_duration_sec"],
                feedback=feedback, previous=previous_script,
            ).model_dump()
            self.db.update_content(cid, script=script)
            workdir = self.s.data_dir / "videos" / f"{cid}_{int(time.time())}"
            if self.ai:
                self.ai.start_run(cid)
            try:
                path = video.render(script, workdir, self.s.raw, self.s.pixabay_api_key or "", ai=self.ai)
            finally:
                if self.ai:
                    self.ai.finish_run()
            caption = script["caption"] + "\n\n" + " ".join(f"#{h.lstrip('#')}" for h in script["hashtags"])
            self.db.update_content(cid, video_path=str(path), caption=caption,
                                   status="pending_review", feedback=feedback)
        except Exception as e:
            log.exception("Generation failed for content %s", cid)
            self.db.update_content(cid, status="failed", error=str(e)[:2000])
            raise
        return cid

    def daily(self) -> list[int]:
        report = self.research()
        n = self.s["content"].get("videos_per_day", 1)
        ids = []
        for idea in report["ideas"][:n]:
            try:
                ids.append(self.generate(idea, report))
            except Exception:
                continue
        return ids

    def regenerate(self, cid: int, feedback: str | None = None) -> int:
        item = self.db.get_content(cid)
        return self.generate(item["idea"], feedback=feedback, previous_script=item["script"], content_id=cid)

    # ---- step 6: publish (only after approval) ----
    def publish(self, cid: int) -> str:
        if not self.publisher:
            raise RuntimeError("Chưa cấu hình TIKTOK_CLIENT_KEY/SECRET")
        item = self.db.get_content(cid)
        if item["status"] not in ("approved", "pending_review"):
            raise RuntimeError(f"Content {cid} đang ở trạng thái {item['status']}, không đăng được")
        tk = self.s["tiktok"]
        if tk["post_mode"] == "direct":
            pid = self.publisher.direct_post(Path(item["video_path"]), item["caption"], tk["privacy_level"])
        else:
            pid = self.publisher.upload_to_inbox(Path(item["video_path"]))
        self.db.update_content(cid, status="posted", publish_id=pid)
        return pid
