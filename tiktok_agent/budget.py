"""Spending ledger + approval gate for paid services.

Every paid call first asks `Budget.ensure(service, estimated_usd)`. If the month's allowance
(free tier + amounts the owner approved on Telegram) would be exceeded, nothing is spent: a budget
request is created and `BudgetNeeded` is raised, so the bot can ask "Duyệt ngân sách?".
"""
from __future__ import annotations

import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

from .db import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, month TEXT, service TEXT, usd REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS budget_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, month TEXT, service TEXT, amount_usd REAL,
    reason TEXT, resume TEXT, status TEXT          -- pending | approved | rejected
);
"""

LABELS = {"claude": "Claude API (viết kế hoạch & kịch bản)",
          "apify": "Apify (quét kênh đối thủ)",
          "gemini_video": "Gemini API (tạo clip AI tự động)"}


class BudgetNeeded(Exception):
    def __init__(self, request: dict):
        super().__init__(f"Cần duyệt ngân sách {request['service']}")
        self.request = request


class Budget:
    def __init__(self, db: DB, cfg: dict, timezone: str):
        self.db = db
        self.cfg = cfg or {}
        self.tz = ZoneInfo(timezone)
        db.conn.executescript(SCHEMA)

    def month(self) -> str:
        return dt.datetime.now(self.tz).strftime("%Y-%m")

    def _svc(self, service: str) -> dict:
        return (self.cfg.get("services") or {}).get(service, {})

    def spent(self, service: str, month: str | None = None) -> float:
        r = self.db.conn.execute("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE service=? AND month=?",
                                 (service, month or self.month())).fetchone()
        return float(r["s"])

    def approved(self, service: str, month: str | None = None) -> float:
        r = self.db.conn.execute("SELECT COALESCE(SUM(amount_usd),0) s FROM budget_requests "
                                 "WHERE service=? AND month=? AND status='approved'",
                                 (service, month or self.month())).fetchone()
        return float(r["s"])

    def allowance(self, service: str) -> float:
        return float(self._svc(service).get("free_monthly_usd", 0)) + self.approved(service)

    def record(self, service: str, usd: float, detail: str = "") -> None:
        self.db.conn.execute("INSERT INTO costs (ts, month, service, usd, detail) VALUES (?,?,?,?,?)",
                             (int(time.time()), self.month(), service, round(usd, 5), detail[:500]))
        self.db.conn.commit()

    def ensure(self, service: str, est_usd: float, reason: str, resume: dict | None = None) -> None:
        if self.spent(service) + est_usd <= self.allowance(service) + 1e-9:
            return
        pending = self.db.conn.execute(
            "SELECT * FROM budget_requests WHERE service=? AND month=? AND status='pending' ORDER BY id DESC",
            (service, self.month())).fetchone()
        if pending:
            req = dict(pending)
            if resume and not req["resume"]:
                self.db.conn.execute("UPDATE budget_requests SET resume=? WHERE id=?",
                                     (json.dumps(resume), req["id"]))
                self.db.conn.commit()
                req["resume"] = json.dumps(resume)
            raise BudgetNeeded(req)
        need = self.spent(service) + est_usd - self.allowance(service)
        amount = max(float(self._svc(service).get("suggest_monthly_usd", 0)), round(need + 0.005, 2))
        cur = self.db.conn.execute(
            "INSERT INTO budget_requests (ts, month, service, amount_usd, reason, resume, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (int(time.time()), self.month(), service, round(amount, 2), reason,
             json.dumps(resume) if resume else None))
        self.db.conn.commit()
        raise BudgetNeeded(self.get(cur.lastrowid))

    def get(self, rid: int) -> dict | None:
        r = self.db.conn.execute("SELECT * FROM budget_requests WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def decide(self, rid: int, approve: bool) -> dict | None:
        req = self.get(rid)
        if not req or req["status"] != "pending":
            return req
        self.db.conn.execute("UPDATE budget_requests SET status=? WHERE id=?",
                             ("approved" if approve else "rejected", rid))
        self.db.conn.commit()
        return self.get(rid)

    def report(self) -> list[dict]:
        out = []
        for service in (self.cfg.get("services") or {}):
            out.append({"service": service, "label": LABELS.get(service, service),
                        "spent": round(self.spent(service), 3),
                        "free": float(self._svc(service).get("free_monthly_usd", 0)),
                        "approved": self.approved(service)})
        return out
