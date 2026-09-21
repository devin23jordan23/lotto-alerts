import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from .features import Candidate, quote_ok
from .models import ET, Snapshot


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                symbol TEXT, at TEXT, payload TEXT, PRIMARY KEY(symbol, at));
            CREATE TABLE IF NOT EXISTS decisions (
                symbol TEXT, at TEXT, state TEXT, reason TEXT, score REAL, features TEXT);
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, day TEXT, symbol TEXT, side TEXT, contract TEXT,
                at TEXT, entry_ask REAL, payload TEXT, snapshot TEXT, delivery TEXT,
                max_return REAL, min_return REAL, latest_return REAL, last_quote TEXT,
                closed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS milestones (
                alert_id TEXT, percent INTEGER, at TEXT, PRIMARY KEY(alert_id, percent));
            CREATE INDEX IF NOT EXISTS alerts_day ON alerts(day, symbol);
        """)

    def record(self, snap: Snapshot) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)",
                            (snap.symbol, snap.at.isoformat(), json.dumps(snap.to_dict())))

    def decision(self, snap: Snapshot, state: str, reason: str, candidate: Candidate | None = None):
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                            (snap.symbol, snap.at.isoformat(), state, reason,
                             candidate.score if candidate else None,
                             json.dumps({"parts": candidate.parts, "metrics": candidate.metrics,
                                         "score_change": candidate.score_change} if candidate else {})))

    def recent(self, symbol: str, day: str) -> list[Snapshot]:
        rows = self.db.execute("SELECT payload FROM snapshots WHERE symbol=? ORDER BY at DESC LIMIT 20", (symbol,))
        return sorted([s for r in rows if (s := Snapshot.from_dict(json.loads(r[0]))).day == day], key=lambda s: s.at)

    def count(self, day: str, symbol: str | None = None) -> int:
        query, args = "SELECT COUNT(*) FROM alerts WHERE day=?", [day]
        if symbol:
            query += " AND symbol=?"
            args.append(symbol)
        return self.db.execute(query, args).fetchone()[0]

    def last_alert(self, day: str, symbol: str):
        return self.db.execute("SELECT * FROM alerts WHERE day=? AND symbol=? ORDER BY at DESC LIMIT 1", (day, symbol)).fetchone()

    def already_alerted(self, day: str, contract: str) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE day=? AND contract=?", (day, contract)).fetchone() is not None

    def queue(self, candidate: Candidate, payload: dict, dry_run: bool) -> str:
        snap, option = candidate.snapshot, candidate.option
        ident = sha256(f"{snap.day}:{snap.symbol}:{option.symbol}:{snap.at.isoformat()}".encode()).hexdigest()[:16]
        payload["embeds"][0]["footer"] = {"text": f"Potential setup • Research score, not probability • {ident}"}
        with self.db:
            self.db.execute("""INSERT INTO alerts
                (id, day, symbol, side, contract, at, entry_ask, payload, snapshot, delivery,
                 max_return, min_return, latest_return, last_quote)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ident, snap.day, snap.symbol, option.side, option.symbol, snap.at.isoformat(),
                 option.ask, json.dumps(payload), json.dumps(snap.to_dict()), "dry_run" if dry_run else "pending",
                 option.bid / option.ask - 1, option.bid / option.ask - 1,
                 option.bid / option.ask - 1, option.quote_time.isoformat()))
        return ident

    def track(self, snap: Snapshot, settings) -> list[dict]:
        milestones = []
        options = {o.symbol: o for o in snap.options}
        with self.db:
            for row in self.db.execute("SELECT * FROM alerts WHERE symbol=? AND closed=0", (snap.symbol,)).fetchall():
                if row["day"] != snap.day or (snap.session_end and snap.at >= snap.session_end):
                    self.db.execute("UPDATE alerts SET closed=1 WHERE id=?", (row["id"],))
                    continue
                option = options.get(row["contract"])
                if not option or not quote_ok(option, snap, settings):
                    continue
                if option.quote_time <= datetime.fromisoformat(row["last_quote"]):
                    continue
                change = option.bid / row["entry_ask"] - 1
                self.db.execute("UPDATE alerts SET max_return=?, min_return=?, latest_return=?, last_quote=? WHERE id=?",
                                (max(row["max_return"], change), min(row["min_return"], change),
                                 change, option.quote_time.isoformat(), row["id"]))
                for percent in (50, 100, 200, 300, 500):
                    if change + 1e-9 >= percent / 100:
                        cursor = self.db.execute("INSERT OR IGNORE INTO milestones VALUES (?, ?, ?)",
                                                 (row["id"], percent, snap.at.isoformat()))
                        if cursor.rowcount:
                            milestones.append({"alert_id": row["id"], "symbol": snap.symbol,
                                               "percent": percent, "bid": option.bid,
                                               "entry_ask": row["entry_ask"]})
        return milestones

    def active_options(self, day: str):
        result = {}
        for row in self.db.execute("SELECT symbol, contract, snapshot FROM alerts WHERE day=? AND closed=0", (day,)):
            snap = Snapshot.from_dict(json.loads(row["snapshot"]))
            option = next(o for o in snap.options if o.symbol == row["contract"])
            result.setdefault(row["symbol"], []).append(option)
        return result

    def summary(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT id, day, symbol, contract, at, entry_ask,
            delivery, latest_return, max_return, min_return, last_quote, closed FROM alerts ORDER BY at""")]
