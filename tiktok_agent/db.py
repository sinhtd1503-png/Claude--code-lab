"""SQLite storage for scraped competitor videos and generated content."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitor_videos (
    id TEXT PRIMARY KEY,
    author TEXT,
    author_followers INTEGER,
    caption TEXT,
    hashtags TEXT,
    plays INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER,
    duration INTEGER,
    music TEXT,
    url TEXT,
    created_at INTEGER,
    scraped_at INTEGER
);
CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER,
    status TEXT,              -- generating | pending_review | approved | rejected | posted | failed
    idea TEXT,                -- JSON
    script TEXT,              -- JSON
    video_path TEXT,
    caption TEXT,
    feedback TEXT,
    telegram_message_id INTEGER,
    publish_id TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- competitor videos ----
    def upsert_videos(self, videos: list[dict]) -> int:
        now = int(time.time())
        for v in videos:
            self.conn.execute(
                """INSERT INTO competitor_videos VALUES
                   (:id,:author,:author_followers,:caption,:hashtags,:plays,:likes,:comments,
                    :shares,:saves,:duration,:music,:url,:created_at,:scraped_at)
                   ON CONFLICT(id) DO UPDATE SET plays=excluded.plays, likes=excluded.likes,
                     comments=excluded.comments, shares=excluded.shares, saves=excluded.saves,
                     author_followers=excluded.author_followers, scraped_at=excluded.scraped_at""",
                {**v, "hashtags": json.dumps(v["hashtags"], ensure_ascii=False), "scraped_at": now},
            )
        self.conn.commit()
        return len(videos)

    def top_videos(self, since_ts: int, limit: int) -> list[dict]:
        # Engagement score: weight actions that signal strong emotional resonance.
        rows = self.conn.execute(
            """SELECT *, (likes + 3*comments + 4*shares + 4*saves) AS score
               FROM competitor_videos WHERE created_at >= ?
               ORDER BY score DESC LIMIT ?""",
            (since_ts, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["hashtags"] = json.loads(d["hashtags"] or "[]")
            out.append(d)
        return out

    # ---- content ----
    def create_content(self, idea: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO content (created_at, status, idea) VALUES (?, 'generating', ?)",
            (int(time.time()), json.dumps(idea, ensure_ascii=False)),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_content(self, cid: int, **fields) -> None:
        for k in ("idea", "script"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE content SET {cols} WHERE id=?", (*fields.values(), cid))
        self.conn.commit()

    def get_content(self, cid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM content WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("idea", "script"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def recent_titles(self, limit: int = 30) -> list[str]:
        rows = self.conn.execute(
            "SELECT script FROM content WHERE script IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["script"]).get("title", "") for r in rows]

    # ---- key/value (tokens etc.) ----
    def kv_get(self, k: str) -> dict | None:
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else None

    def kv_set(self, k: str, v: dict) -> None:
        self.conn.execute(
            "INSERT INTO kv VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)),
        )
        self.conn.commit()
