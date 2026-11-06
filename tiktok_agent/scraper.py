"""Fetch competitor TikTok videos via Apify's TikTok scraper actor."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

ACTOR = "clockworks~tiktok-scraper"
URL = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"


def _normalize(item: dict) -> dict | None:
    if not item.get("id"):
        return None
    author = item.get("authorMeta") or {}
    music = item.get("musicMeta") or {}
    video = item.get("videoMeta") or {}
    return {
        "id": str(item["id"]),
        "author": author.get("name", ""),
        "author_followers": author.get("fans", 0) or 0,
        "caption": item.get("text", "") or "",
        "hashtags": [h.get("name") for h in item.get("hashtags") or [] if h.get("name")],
        "plays": item.get("playCount", 0) or 0,
        "likes": item.get("diggCount", 0) or 0,
        "comments": item.get("commentCount", 0) or 0,
        "shares": item.get("shareCount", 0) or 0,
        "saves": item.get("collectCount", 0) or 0,
        "duration": video.get("duration", 0) or 0,
        "music": music.get("musicName", "") or "",
        "url": item.get("webVideoUrl", "") or "",
        "created_at": int(item.get("createTime", 0) or 0),
    }


def scrape_profiles(token: str, profiles: list[str], per_profile: int) -> list[dict]:
    payload = {
        "profiles": profiles,
        "resultsPerPage": per_profile,
        "profileSorting": "latest",
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSubtitles": False,
    }
    log.info("Scraping %d profiles via Apify…", len(profiles))
    resp = httpx.post(URL, params={"token": token}, json=payload, timeout=600)
    resp.raise_for_status()
    videos = [v for v in (_normalize(i) for i in resp.json()) if v]
    log.info("Got %d videos", len(videos))
    return videos
