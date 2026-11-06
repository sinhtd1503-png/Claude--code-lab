"""TikTok Content Posting API: OAuth + upload to inbox (draft) or direct post.

Docs: https://developers.tiktok.com/doc/content-posting-api-get-started
- inbox mode needs scope `video.upload`  → video lands in the creator's TikTok inbox to finish & post.
- direct mode needs scope `video.publish` → posts immediately. Until your app passes TikTok's audit,
  direct posts are restricted to private (SELF_ONLY) visibility.
"""
from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

from .db import DB

log = logging.getLogger(__name__)

API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_KEY = "tiktok_token"
SCOPES = "user.info.basic,video.upload,video.publish"


class TikTokError(RuntimeError):
    pass


class TikTokPublisher:
    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, db: DB):
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.db = db

    # ---------- OAuth ----------
    def authorize_url(self) -> str:
        params = {
            "client_key": self.client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": secrets.token_urlsafe(16),
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def _token_request(self, data: dict) -> dict:
        r = httpx.post(
            f"{API}/v2/oauth/token/",
            data={"client_key": self.client_key, "client_secret": self.client_secret, **data},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        body = r.json()
        if r.status_code != 200 or "access_token" not in body:
            raise TikTokError(f"Token error: {body}")
        body["expires_at"] = int(time.time()) + int(body.get("expires_in", 0)) - 60
        self.db.kv_set(TOKEN_KEY, body)
        return body

    def exchange_code(self, code: str) -> dict:
        return self._token_request({
            "code": urllib.parse.unquote(code),
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        })

    def _access_token(self) -> str:
        tok = self.db.kv_get(TOKEN_KEY)
        if not tok:
            raise TikTokError("Chưa kết nối TikTok. Chạy: python -m tiktok_agent.main tiktok-auth")
        if tok["expires_at"] <= time.time():
            log.info("Refreshing TikTok access token")
            tok = self._token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        return tok["access_token"]

    def _post(self, path: str, json_body: dict) -> dict:
        r = httpx.post(
            f"{API}{path}",
            json=json_body,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json; charset=UTF-8"},
            timeout=60,
        )
        body = r.json()
        err = body.get("error", {})
        if r.status_code != 200 or err.get("code") not in (None, "ok"):
            raise TikTokError(f"{path} failed: {body}")
        return body["data"]

    # ---------- Posting ----------
    def _upload(self, upload_url: str, video: Path) -> None:
        size = video.stat().st_size
        with video.open("rb") as fh:
            r = httpx.put(
                upload_url,
                content=fh.read(),
                headers={"Content-Type": "video/mp4", "Content-Range": f"bytes 0-{size - 1}/{size}"},
                timeout=600,
            )
        if r.status_code not in (200, 201):
            raise TikTokError(f"Upload failed {r.status_code}: {r.text[:500]}")

    def _source_info(self, video: Path) -> dict:
        size = video.stat().st_size
        if size > 64 * 1024 * 1024:
            raise TikTokError("Video >64MB: cần upload theo chunk (chưa hỗ trợ)")
        return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}

    def upload_to_inbox(self, video: Path) -> str:
        data = self._post("/v2/post/publish/inbox/video/init/", {"source_info": self._source_info(video)})
        self._upload(data["upload_url"], video)
        return data["publish_id"]

    def direct_post(self, video: Path, title: str, privacy_level: str) -> str:
        creator = self._post("/v2/post/publish/creator_info/query/", {})
        options = creator.get("privacy_level_options", [])
        if privacy_level not in options:
            log.warning("privacy %s not allowed (%s) — using %s", privacy_level, options, options[0])
            privacy_level = options[0]
        data = self._post("/v2/post/publish/video/init/", {
            "post_info": {
                "title": title[:2200],
                "privacy_level": privacy_level,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": self._source_info(video),
        })
        self._upload(data["upload_url"], video)
        return data["publish_id"]

    def status(self, publish_id: str) -> dict:
        return self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
