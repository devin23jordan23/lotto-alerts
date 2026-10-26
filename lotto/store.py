import json
import sqlite3
import zlib
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from .features import Candidate, quote_ok
from .config import Settings, STRATEGY_VERSION
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
                closed INTEGER DEFAULT 0, phase TEXT NOT NULL DEFAULT 'LEGACY',
                parent_id TEXT, thesis_id TEXT);
            CREATE TABLE IF NOT EXISTS milestones (
                alert_id TEXT, percent INTEGER, at TEXT, PRIMARY KEY(alert_id, percent));
            CREATE INDEX IF NOT EXISTS alerts_day ON alerts(day, symbol);
            CREATE TABLE IF NOT EXISTS research_ideas (
                id TEXT PRIMARY KEY, day TEXT, symbol TEXT, contract TEXT,
                at TEXT, expires_at TEXT, reason TEXT, entry_ask REAL,
                snapshot TEXT, last_quote TEXT, max_return REAL, min_return REAL,
                latest_return REAL, closed INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS research_ideas_day ON research_ideas(day, symbol);
            CREATE INDEX IF NOT EXISTS snapshots_at ON snapshots(at);
            CREATE TABLE IF NOT EXISTS discovery (
                day TEXT, symbol TEXT, at TEXT, promoted INTEGER, payload TEXT,
                PRIMARY KEY(symbol, at));
            CREATE INDEX IF NOT EXISTS discovery_day ON discovery(day);
            CREATE TABLE IF NOT EXISTS nightly_runs (
                day TEXT PRIMARY KEY, generated_at TEXT, report TEXT);
            CREATE TABLE IF NOT EXISTS chain_coverage (
                day TEXT, symbol TEXT, at TEXT, promoted INTEGER, contracts INTEGER,
                spot REAL, flow_side TEXT, flow_cluster INTEGER,
                PRIMARY KEY(symbol, at));
            CREATE INDEX IF NOT EXISTS chain_coverage_day ON chain_coverage(day, symbol);
            CREATE TABLE IF NOT EXISTS health_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT NOT NULL, reason TEXT NOT NULL,
                notified_at TEXT, recovered_at TEXT);
            CREATE TABLE IF NOT EXISTS candidate_evaluations (
                day TEXT, symbol TEXT, at TEXT, contract TEXT, strategy TEXT,
                qualifying INTEGER, score REAL, features TEXT,
                PRIMARY KEY(symbol,at,contract,strategy));
            CREATE INDEX IF NOT EXISTS candidate_evaluations_day ON candidate_evaluations(day);
            CREATE TABLE IF NOT EXISTS user_feedback (
                alert_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, notes TEXT NOT NULL,
                recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quality_shadow (
                candidate_id TEXT, policy_id TEXT, symbol TEXT, at TEXT, contract TEXT,
                side TEXT, setup TEXT, phase TEXT, tier TEXT, reasons TEXT, labels TEXT,
                trend_escape INTEGER, entry_ask REAL, strategy TEXT, live_outcome TEXT,
                alert_id TEXT, PRIMARY KEY(candidate_id, policy_id));
            CREATE INDEX IF NOT EXISTS quality_shadow_at ON quality_shadow(at);
            CREATE INDEX IF NOT EXISTS quality_shadow_symbol_at ON quality_shadow(symbol,at);
            CREATE TABLE IF NOT EXISTS quality_quotes (
                candidate_id TEXT, at TEXT, quote_time TEXT, bid REAL, ask REAL,
                PRIMARY KEY(candidate_id,quote_time));
            CREATE TABLE IF NOT EXISTS stock_ideas (
                id TEXT PRIMARY KEY, day TEXT, symbol TEXT, side TEXT, at TEXT,
                stock_price REAL, trigger REAL, invalidation REAL, setup TEXT,
                metrics TEXT, payload TEXT, delivery TEXT, delivered_at TEXT,
                max_stock_return REAL DEFAULT 0, min_stock_return REAL DEFAULT 0,
                latest_stock_return REAL DEFAULT 0, last_stock_quote TEXT);
            CREATE INDEX IF NOT EXISTS stock_ideas_day ON stock_ideas(day,symbol,side,at);
            CREATE TABLE IF NOT EXISTS stock_decisions (
                symbol TEXT, at TEXT, side TEXT, reason TEXT, metrics TEXT);
        """)
        # Railway volumes retain older databases. Do not recast historical alerts
        # as active trades when the phase distinction did not exist yet.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(alerts)")}
        with self.db:
            if "phase" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN phase TEXT NOT NULL DEFAULT 'LEGACY'")
            if "parent_id" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN parent_id TEXT")
            if "thesis_id" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN thesis_id TEXT")
            for name, declaration in (("quality_tier", "TEXT NOT NULL DEFAULT 'MAIN'"),
                                      ("quality_policy", "TEXT"), ("delivered_at", "TEXT"),
                                      ("setup_name", "TEXT"), ("setup_trigger", "REAL"),
                                      ("expiry", "TEXT")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE alerts ADD COLUMN {name} {declaration}")

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

    def record_chain_coverage(self, rows):
        with self.db:
            self.db.executemany("""INSERT OR IGNORE INTO chain_coverage
                (day,symbol,at,promoted,contracts,spot,flow_side,flow_cluster)
                VALUES (?,?,?,?,?,?,?,?)""",
                [(r["at"].astimezone(ET).date().isoformat(), r["symbol"], r["at"].isoformat(),
                  int(r["promoted"]), r["contracts"], r["spot"], r["flow_side"], r["flow_cluster"])
                 for r in rows])

    def decision(self, snap: Snapshot, state: str, reason: str, candidate: Candidate | None = None):
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                            (snap.symbol, snap.at.isoformat(), state, reason,
                             candidate.score if candidate else None,
                             json.dumps({"strategy": STRATEGY_VERSION, "parts": candidate.parts, "metrics": candidate.metrics,
                                         "score_change": candidate.score_change,
                                         "setup": asdict(candidate.setup) if candidate.setup else None,
                                         "side": candidate.option.side, "contract": candidate.option.symbol,
                                         "spot": snap.spot, "entry_ask": candidate.option.ask,
                                         "blockers": candidate.blockers} if candidate else {})))
            if candidate is not None:
                self.db.execute("UPDATE quality_shadow SET live_outcome=? WHERE symbol=? AND at=? AND contract=?",
                                (f"{state}: {reason}",snap.symbol,snap.at.isoformat(),candidate.option.symbol))

    def quality_shadow(self, candidate, decision, phase):
        snap, option = candidate.snapshot, candidate.option
        ident = sha256(f"{snap.day}:{snap.symbol}:{option.symbol}:{snap.at.isoformat()}".encode()).hexdigest()[:16]
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO quality_shadow
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ident,decision.policy_id,snap.symbol,snap.at.isoformat(),option.symbol,option.side,
                 candidate.setup.name if candidate.setup else None,phase,decision.tier,
                 json.dumps(decision.reasons),json.dumps(decision.labels),int(decision.trend_escape),
                 option.ask,STRATEGY_VERSION,'evaluating',None))
            self.db.execute("INSERT OR IGNORE INTO quality_quotes VALUES(?,?,?,?,?)",
                            (ident,snap.at.isoformat(),option.quote_time.isoformat(),option.bid,option.ask))

    def record_candidates(self, snap, candidates, settings):
        """Preserve both directions/expiries, including groups that lose ranking."""
        with self.db:
            for candidate in candidates:
                features = {"parts":candidate.parts, "metrics":candidate.metrics,
                            "setup":asdict(candidate.setup) if candidate.setup else None,
                            "side":candidate.option.side, "expiry":candidate.option.expiry.isoformat(),
                            "entry_ask":candidate.option.ask, "entry_bid":candidate.option.bid,
                            "spot":snap.spot, "blockers":candidate.blockers,
                            "settings":asdict(settings)}
                self.db.execute("INSERT OR IGNORE INTO candidate_evaluations VALUES (?,?,?,?,?,?,?,?)",
                    (snap.day,snap.symbol,snap.at.isoformat(),candidate.option.symbol,STRATEGY_VERSION,
                     int(candidate.qualifying),candidate.score,json.dumps(features)))

    def add_feedback(self, alert_id, outcome, notes=""):
        if outcome not in {"worked","failed","mixed"}:
            raise ValueError("Feedback outcome must be worked, failed, or mixed")
        if not self.db.execute("SELECT 1 FROM alerts WHERE id=?",(alert_id,)).fetchone():
            raise ValueError("Unknown alert ID")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO user_feedback VALUES (?,?,?,?)",
                            (alert_id,outcome,notes,datetime.now(timezone.utc).isoformat()))

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

    def last_alert(self, day: str, symbol: str, phase: str | None = None,
                   side: str | None = None, expiry: str | None = None,
                   exclude_tier_c: bool = False):
        query = "SELECT * FROM alerts WHERE day=? AND symbol=?"
        args = [day, symbol]
        if phase is not None:
            query += " AND phase=?"
            args.append(phase)
        if side is not None:
            query += " AND side=?"
            args.append(side)
        if expiry is not None:
            query += " AND expiry=?"
            args.append(expiry)
        if exclude_tier_c:
            query += " AND quality_tier!='C'"
        return self.db.execute(query + " ORDER BY at DESC LIMIT 1", args).fetchone()

    def count_direction(self, day: str, symbol: str, side: str, expiry: str) -> int:
        return self.db.execute("""SELECT COUNT(*) FROM alerts WHERE day=? AND symbol=? AND side=?
                AND expiry=? AND phase='ACTIVE' AND quality_tier!='C'""",
                (day,symbol,side,expiry)).fetchone()[0]

    def already_alerted(self, day: str, contract: str) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE day=? AND contract=?", (day, contract)).fetchone() is not None

    def last_stock_idea(self, day: str, symbol: str, side: str):
        return self.db.execute("""SELECT * FROM stock_ideas WHERE day=? AND symbol=? AND side=?
                                ORDER BY at DESC LIMIT 1""", (day, symbol, side)).fetchone()

    def stock_decision(self, idea, reason: str):
        with self.db:
            self.db.execute("INSERT INTO stock_decisions VALUES (?,?,?,?,?)",
                            (idea.symbol, idea.at.isoformat(), idea.side, reason, json.dumps(idea.metrics)))

    def queue_stock_idea(self, idea, payload: dict, dry_run: bool, mode: str):
        # Stock-only ideas are stored for grading and never posted. `mode` is ignored
        # so an environment flag cannot turn stock cards back on.
        del mode
        delivery = "dry_run" if dry_run else "internal"
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO stock_ideas
                (id,day,symbol,side,at,stock_price,trigger,invalidation,setup,metrics,payload,delivery,last_stock_quote)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (idea.id, idea.day, idea.symbol, idea.side, idea.at.isoformat(), idea.price,
                 idea.trigger, idea.invalidation, idea.setup, json.dumps(idea.metrics),
                 json.dumps(payload), delivery, idea.at.isoformat()))

    def track_stock_ideas(self, row: dict):
        at = row["observed_at"]
        day = at.astimezone(ET).date().isoformat()
        with self.db:
            for idea in self.db.execute("""SELECT id,side,stock_price,max_stock_return,min_stock_return,last_stock_quote
                    FROM stock_ideas WHERE day=? AND symbol=?""", (day, row["symbol"])).fetchall():
                if at.isoformat() <= idea["last_stock_quote"]:
                    continue
                direction = 1 if idea["side"] == "CALL" else -1
                change = direction*(row["price"]/idea["stock_price"]-1)
                self.db.execute("""UPDATE stock_ideas SET max_stock_return=?,min_stock_return=?,
                        latest_stock_return=?,last_stock_quote=? WHERE id=?""",
                        (max(idea["max_stock_return"],change),min(idea["min_stock_return"],change),
                         change,at.isoformat(),idea["id"]))

    def queue(self, candidate: Candidate, payload: dict, dry_run: bool,
              phase: str, parent_id: str | None = None, quality=None,
              tier: str | None = None, sendable: bool = True) -> str:
        if phase not in {"POTENTIAL", "ACTIVE"}:
            raise ValueError("Alert phase must be POTENTIAL or ACTIVE")
        snap, option = candidate.snapshot, candidate.option
        ident = sha256(f"{snap.day}:{snap.symbol}:{option.symbol}:{snap.at.isoformat()}".encode()).hexdigest()[:16]
        thesis_id = parent_id or ident
        payload["embeds"][0]["footer"] = {"text": f"{phase.title()} idea • Research score, not probability • {ident}"}
        with self.db:
            self.db.execute("""INSERT INTO alerts
                (id, day, symbol, side, contract, at, entry_ask, payload, snapshot, delivery,
                 max_return, min_return, latest_return, last_quote, phase, parent_id, thesis_id,
                 setup_name, setup_trigger, expiry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ident, snap.day, snap.symbol, option.side, option.symbol, snap.at.isoformat(),
                 option.ask, json.dumps(payload), json.dumps(snap.to_dict()),
                 "dry_run" if dry_run else "internal" if phase == "POTENTIAL" or not sendable else "pending",
                 option.bid / option.ask - 1, option.bid / option.ask - 1,
                 option.bid / option.ask - 1, option.quote_time.isoformat(),
                 phase, parent_id, thesis_id,
                 candidate.setup.name if candidate.setup else None,
                 candidate.setup.trigger if candidate.setup else None,option.expiry.isoformat()))
            if tier is not None:
                self.db.execute("UPDATE alerts SET quality_tier=? WHERE id=?", (tier,ident))
            if quality is not None:
                self.db.execute("UPDATE alerts SET quality_tier=?,quality_policy=? WHERE id=?",
                                (tier or quality.tier,quality.policy_id,ident))
            self.db.execute("UPDATE quality_shadow SET alert_id=? WHERE candidate_id=?",(ident,ident))
        return ident

    def research_candidate(self, candidate: Candidate, reason: str) -> None:
        """Pin one excluded thesis for a 30-minute quote-path comparison."""
        snap, option = candidate.snapshot, candidate.option
        ident = sha256(f"{snap.day}:{snap.symbol}:{option.symbol}:{reason}".encode()).hexdigest()[:16]
        initial = option.bid / option.ask - 1
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO research_ideas
                (id,day,symbol,contract,at,expires_at,reason,entry_ask,snapshot,last_quote,
                 max_return,min_return,latest_return)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ident,snap.day,snap.symbol,option.symbol,snap.at.isoformat(),
                 (snap.at+timedelta(minutes=30)).isoformat(),reason,option.ask,
                 json.dumps(snap.to_dict()),option.quote_time.isoformat(),
                 initial,initial,initial))

    def track(self, snap: Snapshot, settings) -> list[dict]:
        milestones = []
        options = {o.symbol: o for o in snap.options}
        with self.db:
            # Observe shadows only on already collected chains. Do not silently
            # change discovery/coverage by pinning every suppressed candidate.
            for row in self.db.execute("""SELECT DISTINCT candidate_id,contract FROM quality_shadow
                    WHERE symbol=? AND at>=? AND at<=?""",
                    (snap.symbol,(snap.at-timedelta(minutes=30)).isoformat(),snap.at.isoformat())).fetchall():
                option=options.get(row['contract'])
                if option and quote_ok(option,snap,settings):
                    self.db.execute("INSERT OR IGNORE INTO quality_quotes VALUES(?,?,?,?,?)",
                                    (row['candidate_id'],snap.at.isoformat(),option.quote_time.isoformat(),option.bid,option.ask))
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
            for row in self.db.execute("SELECT * FROM research_ideas WHERE symbol=? AND closed=0", (snap.symbol,)).fetchall():
                if row["day"] != snap.day or snap.at > datetime.fromisoformat(row["expires_at"]):
                    self.db.execute("UPDATE research_ideas SET closed=1 WHERE id=?", (row["id"],))
                    continue
                option = options.get(row["contract"])
                if not option or not quote_ok(option, snap, settings) or option.quote_time <= datetime.fromisoformat(row["last_quote"]):
                    continue
                change = option.bid / row["entry_ask"] - 1
                self.db.execute("""UPDATE research_ideas SET max_return=?,min_return=?,latest_return=?,last_quote=?
                                   WHERE id=?""",(max(row["max_return"],change),min(row["min_return"],change),
                                                 change,option.quote_time.isoformat(),row["id"]))
        return milestones

    def active_options(self, day: str):
        result = {}
        for row in self.db.execute("SELECT symbol, contract, snapshot FROM alerts WHERE day=? AND closed=0", (day,)):
            snap = Snapshot.from_dict(json.loads(row["snapshot"]))
            option = next(o for o in snap.options if o.symbol == row["contract"])
            result.setdefault(row["symbol"], []).append(option)
        for row in self.db.execute("SELECT symbol,contract,snapshot FROM research_ideas WHERE day=? AND closed=0", (day,)):
            snap = Snapshot.from_dict(json.loads(row["snapshot"]))
            option = next(o for o in snap.options if o.symbol == row["contract"])
            if option.symbol not in {o.symbol for o in result.get(row["symbol"], ())}:
                result.setdefault(row["symbol"], []).append(option)
        return result

    def summary(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT id, day, symbol, contract, at, entry_ask,
            phase, parent_id, thesis_id,
            delivery, latest_return, max_return, min_return, last_quote, closed FROM alerts ORDER BY at""")]
