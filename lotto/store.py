import json
import sqlite3
import zlib
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from .features import Candidate, quote_ok
from .config import Settings, STRATEGY_VERSION, recorded_settings
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
                parent_id TEXT, thesis_id TEXT, session_extreme REAL);
            CREATE TABLE IF NOT EXISTS finalb_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
                live_decision TEXT NOT NULL, live_block_reasons TEXT NOT NULL,
                finalb_decision TEXT NOT NULL, finalb_block_reasons TEXT NOT NULL,
                live_score REAL, finalb_score REAL, rule_flags TEXT NOT NULL,
                cooldown_state TEXT NOT NULL, contract TEXT NOT NULL,
                rearm_atr_010 INTEGER, rearm_atr_015 INTEGER,
                rearm_atr_dist REAL, rearm_elapsed_min REAL, rearm_in_window INTEGER);
            CREATE INDEX IF NOT EXISTS finalb_shadow_ts ON finalb_shadow(ts);
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
            CREATE TABLE IF NOT EXISTS chain_coverage (
                day TEXT, symbol TEXT, at TEXT, promoted INTEGER, contracts INTEGER,
                spot REAL, flow_side TEXT, flow_cluster INTEGER,
                PRIMARY KEY(symbol, at));
            CREATE INDEX IF NOT EXISTS chain_coverage_day ON chain_coverage(day, symbol);
            CREATE TABLE IF NOT EXISTS health_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT NOT NULL, reason TEXT NOT NULL,
                notified_at TEXT, recovered_at TEXT, last_notified_at TEXT);
            CREATE TABLE IF NOT EXISTS fix_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, contract TEXT NOT NULL,
                live_decision TEXT, setup TEXT,
                lm_ext_atr REAL, lm_hb INTEGER, lm_fresh_lvl INTEGER, lm_rv2 REAL, lm_block INTEGER,
                mkt5 REAL, mkt10 REAL, mkt15 REAL, mkt_state TEXT, mg_block INTEGER,
                fix_flags TEXT);
            CREATE INDEX IF NOT EXISTS fix_shadow_ts ON fix_shadow(ts);
            CREATE TABLE IF NOT EXISTS stock_first_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, setup TEXT,
                trigger REAL, contract TEXT, entry_bid REAL, entry_ask REAL,
                lm_ext_atr REAL, mkt_state TEXT,
                cap_blocked INTEGER, cooldown_blocked INTEGER, no_contract INTEGER,
                fix_flags TEXT);
            CREATE INDEX IF NOT EXISTS stock_first_shadow_ts ON stock_first_shadow(ts);
            CREATE TABLE IF NOT EXISTS l2_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, mode TEXT NOT NULL, ticker TEXT NOT NULL,
                side TEXT, setup TEXT, contract TEXT, live_at TEXT,
                hot_rank INTEGER, would_eval_at TEXT);
            CREATE INDEX IF NOT EXISTS l2_shadow_ts ON l2_shadow(ts);
            CREATE TABLE IF NOT EXISTS candidate_evaluations (
                day TEXT, symbol TEXT, at TEXT, contract TEXT, strategy TEXT,
                qualifying INTEGER, score REAL, features TEXT,
                PRIMARY KEY(symbol,at,contract,strategy));
            CREATE INDEX IF NOT EXISTS candidate_evaluations_day ON candidate_evaluations(day);
            CREATE TABLE IF NOT EXISTS user_feedback (
                alert_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, notes TEXT NOT NULL,
                recorded_at TEXT NOT NULL);
        """)
        # Railway volumes retain older databases. Do not recast historical alerts
        # as active trades when the phase distinction did not exist yet.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(alerts)")}
        shadow_columns = {row[1] for row in self.db.execute("PRAGMA table_info(finalb_shadow)")}
        health_columns = {row[1] for row in self.db.execute("PRAGMA table_info(health_incidents)")}
        alert_columns = columns
        with self.db:
            if "phase" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN phase TEXT NOT NULL DEFAULT 'LEGACY'")
            if "parent_id" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN parent_id TEXT")
            if "thesis_id" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN thesis_id TEXT")
            if "session_extreme" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN session_extreme REAL")
            # Nullable. Existing rows and flag-off writes stay NULL; no second row per candidate.
            if "rearm_atr_010" not in shadow_columns:
                self.db.execute("ALTER TABLE finalb_shadow ADD COLUMN rearm_atr_010 INTEGER")
            if "rearm_atr_015" not in shadow_columns:
                self.db.execute("ALTER TABLE finalb_shadow ADD COLUMN rearm_atr_015 INTEGER")
            if "rearm_atr_dist" not in shadow_columns:
                self.db.execute("ALTER TABLE finalb_shadow ADD COLUMN rearm_atr_dist REAL")
            if "rearm_elapsed_min" not in shadow_columns:
                self.db.execute("ALTER TABLE finalb_shadow ADD COLUMN rearm_elapsed_min REAL")
            if "rearm_in_window" not in shadow_columns:
                self.db.execute("ALTER TABLE finalb_shadow ADD COLUMN rearm_in_window INTEGER")
            if "last_notified_at" not in health_columns:
                self.db.execute("ALTER TABLE health_incidents ADD COLUMN last_notified_at TEXT")
            for name, kind in (
                ("break_bar_close", "TEXT"), ("qual_bar_close", "TEXT"), ("first_qual_at", "TEXT"),
                ("eval_start", "TEXT"), ("snapshot_bar_close", "TEXT"), ("card_created", "TEXT"),
                ("delivered_at", "TEXT"), ("setup_trigger", "REAL"), ("delivery_attempts", "INTEGER"),
            ):
                if name not in alert_columns:
                    self.db.execute(f"ALTER TABLE alerts ADD COLUMN {name} {kind}")

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

    def decision(self, snap: Snapshot, state: str, reason: str, candidate: Candidate | None = None, latency=None):
        features = {"strategy": STRATEGY_VERSION, "parts": candidate.parts, "metrics": candidate.metrics,
                    "score_change": candidate.score_change,
                    "setup": asdict(candidate.setup) if candidate.setup else None,
                    "side": candidate.option.side, "contract": candidate.option.symbol,
                    "spot": snap.spot, "entry_ask": candidate.option.ask,
                    "blockers": candidate.blockers} if candidate else {}
        if latency:
            for key in ("eval_start", "snapshot_bar_close", "would_qualify_seeded"):
                if key in latency and latency[key] is not None:
                    features[key] = latency[key]
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                            (snap.symbol, snap.at.isoformat(), state, reason,
                             candidate.score if candidate else None, json.dumps(features)))

    def record_candidates(self, snap, candidates, settings):
        """Preserve both directions/expiries, including groups that lose ranking."""
        with self.db:
            for candidate in candidates:
                features = {"parts":candidate.parts, "metrics":candidate.metrics,
                            "setup":asdict(candidate.setup) if candidate.setup else None,
                            "side":candidate.option.side, "expiry":candidate.option.expiry.isoformat(),
                            "entry_ask":candidate.option.ask, "entry_bid":candidate.option.bid,
                            "spot":snap.spot, "blockers":candidate.blockers,
                            "settings":recorded_settings(settings)}
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

    def last_alert(self, day: str, symbol: str):
        return self.db.execute("SELECT * FROM alerts WHERE day=? AND symbol=? ORDER BY at DESC LIMIT 1", (day, symbol)).fetchone()

    def already_alerted(self, day: str, contract: str) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE day=? AND contract=?", (day, contract)).fetchone() is not None

    def record_finalb_shadow(self, rows):
        if not rows:
            return
        with self.db:
            self.db.executemany("""INSERT INTO finalb_shadow
                (ts, ticker, side, live_decision, live_block_reasons, finalb_decision,
                 finalb_block_reasons, live_score, finalb_score, rule_flags, cooldown_state, contract,
                 rearm_atr_010, rearm_atr_015, rearm_atr_dist, rearm_elapsed_min, rearm_in_window)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(r["ts"], r["ticker"], r["side"], r["live_decision"], r["live_block_reasons"],
                  r["finalb_decision"], r["finalb_block_reasons"], r["live_score"], r["finalb_score"],
                  r["rule_flags"], r["cooldown_state"], r["contract"],
                  r.get("rearm_atr_010"), r.get("rearm_atr_015"),
                  r.get("rearm_atr_dist"), r.get("rearm_elapsed_min"), r.get("rearm_in_window")) for r in rows])

    def record_fix_shadow(self, rows):
        if not rows:
            return
        with self.db:
            self.db.executemany("""INSERT INTO fix_shadow
                (ts, ticker, side, contract, live_decision, setup, lm_ext_atr, lm_hb, lm_fresh_lvl,
                 lm_rv2, lm_block, mkt5, mkt10, mkt15, mkt_state, mg_block, fix_flags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(r["ts"], r["ticker"], r["side"], r["contract"], r.get("live_decision"), r.get("setup"),
                  r.get("lm_ext_atr"), r.get("lm_hb"), r.get("lm_fresh_lvl"), r.get("lm_rv2"), r.get("lm_block"),
                  r.get("mkt5"), r.get("mkt10"), r.get("mkt15"), r.get("mkt_state"), r.get("mg_block"),
                  r.get("fix_flags")) for r in rows])

    def record_stock_first(self, rows):
        if not rows:
            return
        with self.db:
            self.db.executemany("""INSERT INTO stock_first_shadow
                (ts, ticker, side, setup, trigger, contract, entry_bid, entry_ask, lm_ext_atr, mkt_state,
                 cap_blocked, cooldown_blocked, no_contract, fix_flags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(r["ts"], r["ticker"], r["side"], r.get("setup"), r.get("trigger"), r.get("contract"),
                  r.get("entry_bid"), r.get("entry_ask"), r.get("lm_ext_atr"), r.get("mkt_state"),
                  r.get("cap_blocked"), r.get("cooldown_blocked"), r.get("no_contract"), r.get("fix_flags"))
                 for r in rows])

    def record_finalb_discovery(self, report):
        """One RANK row per scored name. Discovery is not a card, so side is RANK."""
        import json
        top_live = set(report["top_live"])
        top_finalb = set(report["top_finalb"])
        at = report["at"].isoformat()
        rows = []
        for item in report["rows"]:
            rows.append({
                "ts": at,
                "ticker": item["symbol"],
                "side": "RANK",
                "live_decision": f"rank {item['live_rank']}",
                "live_block_reasons": "[]",
                "finalb_decision": f"rank {item['finalb_rank']}",
                "finalb_block_reasons": "[]",
                "live_score": item["live_score"],
                "finalb_score": item["finalb_score"],
                "rule_flags": json.dumps({
                    "kind": "discovery",
                    "live_rank": item["live_rank"],
                    "finalb_rank": item["finalb_rank"],
                    "rank_delta": item["delta"],
                    "in_live_top": item["symbol"] in top_live,
                    "in_finalb_top": item["symbol"] in top_finalb,
                }),
                "cooldown_state": "{}",
                "contract": "",
            })
        self.record_finalb_shadow(rows)

    def record_shadow_card(self, row):
        with self.db:
            self.db.execute("""INSERT INTO l2_shadow
                (ts, mode, ticker, side, setup, contract, live_at, hot_rank, would_eval_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (row["ts"], row["mode"], row["ticker"], row.get("side"), row.get("setup"),
                 row.get("contract"), row.get("live_at"), row.get("hot_rank"), row.get("would_eval_at")))

    def queue(self, candidate: Candidate, payload: dict, dry_run: bool,
              phase: str, parent_id: str | None = None, session_extreme: float | None = None,
              latency=None) -> str:
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
                 session_extreme)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ident, snap.day, snap.symbol, option.side, option.symbol, snap.at.isoformat(),
                 option.ask, json.dumps(payload), json.dumps(snap.to_dict()),
                 "dry_run" if dry_run else "internal" if phase == "POTENTIAL" else "pending",
                 option.bid / option.ask - 1, option.bid / option.ask - 1,
                 option.bid / option.ask - 1, option.quote_time.isoformat(),
                 phase, parent_id, thesis_id, session_extreme))
            if latency:
                self.db.execute("""UPDATE alerts SET break_bar_close=?, qual_bar_close=?, first_qual_at=?,
                    eval_start=?, snapshot_bar_close=?, card_created=?, setup_trigger=? WHERE id=?""",
                    (latency.get("break_bar_close"), latency.get("qual_bar_close"), latency.get("first_qual_at"),
                     latency.get("eval_start"), latency.get("snapshot_bar_close"), latency.get("card_created"),
                     latency.get("setup_trigger"), ident))
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
            phase, parent_id, thesis_id,
            delivery, latest_return, max_return, min_return, last_quote, closed FROM alerts ORDER BY at""")]
