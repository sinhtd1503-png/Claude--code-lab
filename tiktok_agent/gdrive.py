"""Google Drive: keep one Google Doc with every approved script (dialogue included).

Sign-in uses Google's device flow ("TVs and Limited Input devices" OAuth client): the bot sends a short code,
the owner opens google.com/device on the iPad, types it and allows access. No redirect URL is needed.

Scope `drive.file` only lets the app see the files it created itself (the folder and the document) — not the
rest of the owner's Drive. The whole document is rebuilt from the database on every approval and uploaded over
the same file, so a failed upload is repaired by the next one.
"""
from __future__ import annotations

import json
import time

import httpx

from .db import DB

SCOPE = "https://www.googleapis.com/auth/drive.file"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
KEY = "gdrive"
FOLDER_NAME = "TikTok Agent"
DOC_NAME = "Kịch bản đã duyệt – TikTok Agent"


class DriveError(RuntimeError):
    pass


def _http() -> httpx.Client:  # separate function so tests can swap the transport
    return httpx.Client(timeout=60)


class GDrive:
    def __init__(self, db: DB, client_id: str, client_secret: str):
        self.db, self.client_id, self.client_secret = db, client_id, client_secret

    # ---------- state ----------
    def _state(self) -> dict:
        return self.db.kv_get(KEY) or {}

    def _save(self, **kw) -> None:
        self.db.kv_set(KEY, {**self._state(), **kw})

    def connected(self) -> bool:
        return bool(self._state().get("refresh_token"))

    def doc_link(self) -> str:
        return self._state().get("doc_link", "")

    # ---------- device sign-in ----------
    def start_device(self) -> dict:
        with _http() as c:
            r = c.post(DEVICE_URL, data={"client_id": self.client_id, "scope": SCOPE})
        body = r.json()
        if r.status_code != 200:
            raise DriveError(f"Google từ chối: {body.get('error_description') or body.get('error')}")
        self._save(device_code=body["device_code"], device_expires=time.time() + int(body.get("expires_in", 1800)))
        return {"url": body.get("verification_url") or body.get("verification_uri"), "code": body["user_code"],
                "minutes": int(body.get("expires_in", 1800)) // 60}

    def poll_device(self) -> str:
        """'ok' | 'pending' | 'denied' | 'expired'."""
        st = self._state()
        if not st.get("device_code") or time.time() > st.get("device_expires", 0):
            return "expired"
        with _http() as c:
            r = c.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                        "device_code": st["device_code"],
                                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        body = r.json()
        if r.status_code == 200 and body.get("refresh_token"):
            self._save(refresh_token=body["refresh_token"], access_token=body["access_token"],
                       access_expires=time.time() + int(body.get("expires_in", 3600)) - 60, device_code=None)
            return "ok"
        err = body.get("error", "")
        if err in ("authorization_pending", "slow_down"):
            return "pending"
        if err == "access_denied":
            return "denied"
        if err == "expired_token":
            return "expired"
        raise DriveError(f"Google báo lỗi: {body.get('error_description') or err}")

    def disconnect(self) -> None:
        self.db.kv_set(KEY, {})

    # ---------- API ----------
    def _token(self) -> str:
        st = self._state()
        if not st.get("refresh_token"):
            raise DriveError("Chưa kết nối Google Drive (gõ /drive)")
        if st.get("access_token") and time.time() < st.get("access_expires", 0):
            return st["access_token"]
        with _http() as c:
            r = c.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                        "refresh_token": st["refresh_token"], "grant_type": "refresh_token"})
        body = r.json()
        if r.status_code != 200:
            if body.get("error") == "invalid_grant":  # revoked, or 7-day expiry of an app left in "Testing"
                self._save(refresh_token=None, access_token=None)
                raise DriveError("Quyền truy cập Google Drive đã hết hạn hoặc bị thu hồi — gõ /drive để kết nối lại")
            raise DriveError(f"Không làm mới được quyền Google: {body.get('error_description') or body.get('error')}")
        self._save(access_token=body["access_token"], access_expires=time.time() + int(body.get("expires_in", 3600)) - 60)
        return body["access_token"]

    def _req(self, method: str, url: str, **kw) -> dict:
        with _http() as c:
            r = c.request(method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
        if r.status_code == 404:
            raise FileNotFoundError(url)
        if r.status_code >= 400:
            raise DriveError(f"Google Drive lỗi {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def _folder(self) -> str:
        fid = self._state().get("folder_id")
        if fid:
            try:
                f = self._req("GET", f"{API}/files/{fid}", params={"fields": "id,trashed"})
                if not f.get("trashed"):
                    return fid
            except FileNotFoundError:
                pass
        f = self._req("POST", f"{API}/files", params={"fields": "id"},
                      json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"})
        self._save(folder_id=f["id"], doc_id=None)
        return f["id"]

    @staticmethod
    def _multipart(meta: dict, html: str) -> tuple[bytes, str]:
        b = "tiktokagentboundary"
        body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{b}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n{html}\r\n--{b}--").encode()
        return body, f"multipart/related; boundary={b}"

    def save_doc(self, html: str) -> str:
        """Create or overwrite the Google Doc with `html`; returns its link."""
        doc_id = self._state().get("doc_id")
        if doc_id:
            body, ctype = self._multipart({}, html)
            try:
                f = self._upload("PATCH", doc_id, body, ctype)
                if not f.get("trashed"):
                    self._save(doc_link=f.get("webViewLink", ""))
                    return f.get("webViewLink", "")
            except FileNotFoundError:
                pass
        meta = {"name": DOC_NAME, "mimeType": "application/vnd.google-apps.document", "parents": [self._folder()]}
        body, ctype = self._multipart(meta, html)
        f = self._upload("POST", None, body, ctype)
        self._save(doc_id=f["id"], doc_link=f.get("webViewLink", ""))
        return f.get("webViewLink", "")

    def _upload(self, method: str, file_id: str | None, body: bytes, ctype: str) -> dict:
        url = f"{UPLOAD}/files/{file_id}" if file_id else f"{UPLOAD}/files"
        with _http() as c:
            r = c.request(method, url, params={"uploadType": "multipart", "fields": "id,webViewLink,trashed"},
                          headers={"Authorization": f"Bearer {self._token()}", "Content-Type": ctype}, content=body)
        if r.status_code == 404:
            raise FileNotFoundError(url)
        if r.status_code >= 400:
            raise DriveError(f"Google Drive lỗi {r.status_code}: {r.text[:300]}")
        return r.json()
