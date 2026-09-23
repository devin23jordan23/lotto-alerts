import json
import sqlite3
import zlib
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from .features import Candidate, quote_ok
from .config import Settings
from .models import ET, Snapshot


def read_snapshot(payload):
    # SQLite accepts BLOBs in the existing payload column; retain compatibility
    # with uncompressed observations from earlier deployments.
    return Snapshot.from_dict(json.loads(zlib.decompress(payload) if isinstance(payload,bytes) else payload))


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
            CREATE INDEX IF NOT EXISTS snapshots_at ON snapshots(at);
            CREATE TABLE IF NOT EXISTS discovery (
                day TEXT, symbol TEXT, at TEXT, promoted INTEGER, payload TEXT,
                PRIMARY KEY(symbol, at));
            CREATE INDEX IF NOT EXISTS discovery_day ON discovery(day);
            CREATE TABLE IF NOT EXISTS nightly_runs (
                day TEXT PRIMARY KEY, generated_at TEXT, report TEXT);
        """)

    def record(self, snap: Snapshot) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)",
                            (snap.symbol, snap.at.isoformat(), zlib.compress(json.dumps(snap.to_dict()).encode(),level=3)))

    def record_discovery(self, rows):
        with self.db:
            for row in rows:
                at = row["observed_at"]
                self.db.execute("INSERT OR IGNORE INTO discovery VALUES (?, ?, ?, ?, ?)",
                                (at.astimezone(ET).date().isoformat(), row["symbol"], at.isoformat(),
                                 int(row["promoted"]), json.dumps(row, default=lambda v:v.isoformat())))

    def decision(self, snap: Snapshot, state: str, reason: str, candidate: Candidate | None = None):
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                            (snap.symbol, snap.at.isoformat(), state, reason,
                             candidate.score if candidate else None,
                             json.dumps({"parts": candidate.parts, "metrics": candidate.metrics,
                                         "score_change": candidate.score_change,
                                         "setup": asdict(candidate.setup) if candidate.setup else None,
                                         "side": candidate.option.side, "contract": candidate.option.symbol,
                                         "spot": snap.spot, "entry_ask": candidate.option.ask,
                                         "blockers": candidate.blockers} if candidate else {})))

    def recent(self, symbol: str, day: str) -> list[Snapshot]:
        rows = self.db.execute("SELECT payload FROM snapshots WHERE symbol=? ORDER BY at DESC LIMIT 30", (symbol,))
        return sorted([s for r in rows if (s := read_snapshot(r[0])).day == day], key=lambda s: s.at)

    def session_snapshots(self, day: str):
        """Return every stored observation for one session in timestamp order.

        This is intentionally separate from ``recent``: nightly research needs the
        complete captured session, while live evaluation only needs a short window.
        """
        # Both UTC live records and ET replay records share the ET calendar date
        # during the regular US session; avoid loading prior days' large payloads.
        rows = self.db.execute("SELECT payload FROM snapshots WHERE at >= ? AND at < ? ORDER BY at",
                               (day, (datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()))
        for row in rows:
            snap = read_snapshot(row[0])
            if snap.day == day:
                yield snap

    def end_of_day_options(self, day: str, close_time: time | None = None) -> list[dict]:
        """Stream fresh sampled quotes; never retain an entire day's chains in RAM."""
        opening = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET)
        default_close = opening.replace(hour=16, minute=0)
        if close_time is not None:
            default_close = min(default_close, datetime.combine(opening.date(), close_time, ET)+timedelta(minutes=1))
        settings, records = Settings(), {}
        for snap in self.session_snapshots(day):
            closing = min(default_close, snap.session_end or default_close)
            for option in snap.options:
                if not quote_ok(option, snap, settings) or not opening <= option.quote_time < closing:
                    continue
                row = records.get(option.symbol)
                if row and option.quote_time <= row["_last"]:
                    continue
                if row is None:
                    row = records[option.symbol] = {
                        "day":day, "underlying":snap.symbol, "contract":option.symbol,
                        "expiry":option.expiry.isoformat(), "side":option.side, "strike":option.strike,
                        "entry_ask":option.ask, "max_bid":option.bid, "min_bid":option.bid,
                        "max_return_at":option.quote_time.isoformat(), "min_return_at":option.quote_time.isoformat(),
                        "first_quote":option.quote_time.isoformat(), "open_covered":option.quote_time < opening+timedelta(minutes=1),
                        "quote_count":0, "sampled_path":True, "largest_quote_gap_seconds":0, "_last":option.quote_time,
                    }
                row["largest_quote_gap_seconds"] = max(row["largest_quote_gap_seconds"], (option.quote_time-row["_last"]).total_seconds())
                row["_last"] = option.quote_time
                row["last_quote"] = option.quote_time.isoformat()
                row["quote_count"] += 1
                row["last_observed_bid"] = option.bid
                row["close_covered"] = option.quote_time >= closing-timedelta(minutes=1)
                if option.bid > row["max_bid"]:
                    row["max_bid"], row["max_return_at"] = option.bid, option.quote_time.isoformat()
                if option.bid < row["min_bid"]:
                    row["min_bid"], row["min_return_at"] = option.bid, option.quote_time.isoformat()
        for row in records.values():
            row.pop("_last")
            row["observed_return"] = row["last_observed_bid"]/row["entry_ask"]-1 if row["quote_count"]>1 else None
            row["close_bid"] = row["last_observed_bid"] if row["close_covered"] else None
            row["open_to_close_return"] = row["observed_return"] if row["open_covered"] and row["close_covered"] else None
            row["max_return"] = row["max_bid"]/row["entry_ask"]-1
            row["min_return"] = row["min_bid"]/row["entry_ask"]-1
        return sorted(records.values(), key=lambda row:(row["underlying"], -row["max_return"], row["contract"]))

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
