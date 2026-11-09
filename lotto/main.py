import argparse
import fcntl
import json
import logging
import os
import signal
import time
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings, STRATEGY_VERSION
from .demo import demo_frames
from .discord import deliver_pending
from .engine import Engine
from .models import ET, Snapshot
from .schwab import Schwab
from .store import Store
from .nightly import maybe_nightly, run_nightly
from .day_export import maybe_day_export
from .health import check
from .audit import audit_store, diagnose_symbols, audit_coil_burst_hypothesis
from .forensics import export_case, export_window_closed
from .operations import HealthMonitor
from .persistence import (
    PersistenceError, assess_session, backup_database, connect_readonly, file_identity,
    list_session_days, live_start_blockers, require_existing_database, resolve_database_path,
)
from .exit_replay import build_specs, replay_day, write_replay

LOG = logging.getLogger(__name__)

DEFAULT_UNIVERSE = (
    "SPX,SPY,QQQ,IWM,DIA,SMH,SOXX,XLK,XLF,XLE,GLD,USO,SLV,MTUM,AAPL,MSFT,NVDA,TSLA,META,AMZN,GOOGL,NFLX,"
    "AVGO,AMD,PLTR,COIN,HOOD,MSTR,MU,TSM,ARM,QCOM,MRVL,INTC,AMAT,LRCX,KLAC,ASML,SNDK,SMCI,DELL,VRT,"
    "ANET,NBIS,AAOI,SPCX,CSCO,HPE,NOW,ORCL,CRM,ADBE,SNOW,DDOG,NET,CRWD,PANW,ZS,OKTA,TEAM,MDB,SHOP,BE,VST,CEG,NRG,"
    "OKLO,FSLR,ENPH,PYPL,SOFI,AFRM,UPST,RBLX,UBER,ABNB,JPM,BAC,MS,GS,C,SCHW,V,MA,AXP,WMT,TGT,COST,"
    "MCD,CMG,LULU,NKE,SBUX,HD,LOW,LLY,UNH,MRNA,REGN,ISRG,BA,LMT,CAT,DE,XOM,CVX"
)

REQUIRED_UNIVERSE = {"CSCO", "HPE", "SPX", "SPCX"}


def universe_symbols(raw: str) -> list[str]:
    symbols = {s.strip().upper() for s in raw.split(",") if s.strip()}
    return sorted(symbols | REQUIRED_UNIVERSE) if symbols else []


def env_settings() -> Settings:
    defaults = Settings()
    updates = {}
    for field in fields(defaults):
        raw = os.getenv("LOTTO_" + field.name.upper())
        if raw is not None:
            updates[field.name] = type(getattr(defaults, field.name))(raw)
    return replace(defaults, **updates)


def _print_session(database, day, allow_partial):
    """Print coverage for one explicit session date. Exit 2 unless it is a full day."""
    connection = connect_readonly(database)
    try:
        days = list_session_days(connection)
        if not day:
            print(json.dumps({
                "error": "Pass --day YYYY-MM-DD. session does not choose yesterday or the newest file.",
                "captured_session_days": days,
            }, indent=2))
            raise SystemExit(2)
        assessment = assess_session(connection, day, db_path=database, now=datetime.now(ET))
    finally:
        connection.close()
    print(json.dumps(assessment, indent=2))
    if not assessment["grade_as_full_day"] and not allow_partial:
        raise SystemExit(2)


def _run_nightly_cli(args, database, data_dir):
    """Grade one explicit day. Incomplete captures need --allow-partial and stay marked partial."""
    connection = connect_readonly(database)
    try:
        days = list_session_days(connection)
        if not args.day:
            print(json.dumps({
                "error": "Nightly needs an explicit --day. Refusing to default to yesterday or the newest file.",
                "captured_session_days": days,
                "grader_boundary": (
                    "The stop-or-+40% card grader is not in this repository. "
                    "After session exits 0, point that grader at this same --db and --day."
                ),
            }, indent=2))
            raise SystemExit(2)
        assessment = assess_session(connection, args.day, db_path=database)
    finally:
        connection.close()
    if not assessment["grade_as_full_day"] and not args.allow_partial:
        print(json.dumps({
            "error": "Session is not a full day. Pass --allow-partial to write a report that stays marked partial.",
            "assessment": assessment,
        }, indent=2))
        raise SystemExit(2)
    store = Store(database)
    report_path = run_nightly(store, args.day, data_dir)
    print(json.dumps({
        "day": args.day, "report": str(report_path), "full_day_grade": assessment["grade_as_full_day"],
    }, indent=2))


def _floats(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part.strip()]


def _run_exit_replay(args, database):
    if not args.day:
        print(json.dumps({
            "error": "exit-replay needs an explicit --day. It does not choose yesterday or the newest file.",
            "live_stops_changed": False,
        }, indent=2))
        raise SystemExit(2)
    report = replay_day(database, args.day, build_specs(_floats(args.stops), _floats(args.targets), _floats(args.time_stops)))
    print(json.dumps(report, indent=2))
    if not report["full_day_grade"] and not args.allow_partial:
        raise SystemExit(2)
    if args.output:
        write_replay(report, args.output)


def _log_open_session(store, database):
    days = list_session_days(store.db)
    if not days:
        LOG.warning("Database %s has no captured sessions", database)
        return
    assessment = assess_session(store.db, days[-1], db_path=database, now=datetime.now(ET))
    if assessment["status"] in {"complete", "in_progress"}:
        LOG.info("Latest captured session %s is %s", days[-1], assessment["status"])
        return
    LOG.warning(
        "Latest captured session %s is %s (%s). New rows append. Do not grade it as a full day.",
        days[-1], assessment["status"], ",".join(assessment["defects"]),
    )


def replay_frames(path):
    with open(path) as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                yield [Snapshot.from_dict(s) for s in (value if isinstance(value, list) else [value])]


def main():
    parser = argparse.ArgumentParser(description="Potential watches and active options ideas")
    parser.add_argument("mode", choices=("demo", "replay", "live", "report", "nightly", "check", "feedback",
                                         "session", "backup", "migrate", "exit-replay"), nargs="?", default="demo")
    parser.add_argument("--input", help="Replay JSONL: one snapshot array per scan cycle")
    parser.add_argument("--db", help="SQLite state path. Does not create a replacement for an existing live file")
    parser.add_argument("--day", help="Explicit America/New_York session date (YYYY-MM-DD)")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Write a nightly report even when the captured session is incomplete")
    parser.add_argument("--dest", help="Backup destination. Must not already exist")
    parser.add_argument("--stops", default="-0.35", help="Comma-separated option-bid stop returns, each negative")
    parser.add_argument("--targets", default="0.40,1.0", help="Comma-separated option-bid target returns")
    parser.add_argument("--time-stops", default="", help="Comma-separated time stops in minutes")
    parser.add_argument("--output", help="Write exit-replay JSON here. Refuses to replace an existing file")
    parser.add_argument("--once", action="store_true", help="Poll one live cycle, retaining warmup requirements")
    parser.add_argument("--alert-id", help="Alert ID from the Discord footer for feedback")
    parser.add_argument("--outcome", choices=("worked","failed","mixed"))
    parser.add_argument("--notes", default="", help="Trader feedback; separate from quote-derived outcomes")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    # On Railway, creating DATA_DIR here hides a volume that failed to mount.
    if os.getenv("RAILWAY_SERVICE_ID"):
        if not data_dir.is_dir():
            LOG.error("DATA_DIR %s is not a directory. Not creating it on Railway", data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
    database = resolve_database_path(args.mode, args.db)
    if args.mode == "check":
        symbols=universe_symbols(os.getenv("LOTTO_UNIVERSE") or DEFAULT_UNIVERSE)
        result=check(Schwab(str(data_dir)),symbols,data_dir,database)
        print(json.dumps(result,indent=2))
        raise SystemExit(0 if result['ready'] else 1)
    try:
        if args.mode in {"report", "nightly", "feedback", "session", "backup", "migrate", "exit-replay"}:
            require_existing_database(database)
        if args.mode == "session":
            _print_session(database, args.day, args.allow_partial)
            return
        if args.mode == "backup":
            if not args.dest:
                parser.error("backup requires --dest")
            print(json.dumps(backup_database(database, args.dest), indent=2))
            return
        if args.mode == "migrate":
            before = file_identity(database)
            store = Store(database)
            after = file_identity(database)
            print(json.dumps({
                "schema_version": store.db.execute("PRAGMA user_version").fetchone()[0],
                "snapshot_count": store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                "before": before, "after": after,
                "inode_unchanged": before.get("inode") == after.get("inode"),
                "deleted_rows": False,
            }, indent=2))
            return
        if args.mode == "nightly":
            _run_nightly_cli(args, database, data_dir)
            return
        if args.mode == "exit-replay":
            try:
                _run_exit_replay(args, database)
            except (ValueError, FileExistsError) as exc:
                raise SystemExit(str(exc))
            return
    except PersistenceError as exc:
        raise SystemExit(str(exc))
    if args.mode == "feedback":
        if not args.alert_id or not args.outcome:
            parser.error("feedback requires --alert-id and --outcome")
        store = Store(database)
        store.add_feedback(args.alert_id,args.outcome,args.notes)
        print(json.dumps({"alert_id":args.alert_id,"outcome":args.outcome,"recorded":True}))
        return
    if args.mode == "report":
        print(json.dumps(Store(database).summary(), indent=2))
        return
    if args.mode == "live":
        blockers = live_start_blockers(database, datetime.now(ET))
        if blockers:
            raise SystemExit("Refusing to start live. " + " ".join(blockers))
    if not os.getenv("RAILWAY_SERVICE_ID"):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    existed_before_open = Path(database).is_file() and Path(database).stat().st_size > 0
    lock = open(database + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another worker already owns this database; use one Railway replica")
    store = Store(database)
    if args.mode == "live":
        store.note_start(
            "live", existed=existed_before_open,
            note="opened existing database" if existed_before_open else "created a new database outside the regular session",
        )
        _log_open_session(store, database)
    live_delivery = args.mode == "live" and os.getenv("LOTTO_SEND_ALERTS", "false").lower() == "true"
    webhook = os.getenv("DISCORD_LOTTO_WEBHOOK", "")
    if live_delivery and not webhook:
        raise SystemExit("DISCORD_LOTTO_WEBHOOK is required when LOTTO_SEND_ALERTS=true")
    engine = Engine(store, env_settings(), dry_run=not live_delivery)
    health_monitor = HealthMonitor(store, webhook)
    if args.mode in {"demo", "replay"}:
        if args.mode == "replay" and not args.input:
            parser.error("replay requires --input")
        for frame in demo_frames() if args.mode == "demo" else replay_frames(args.input):
            for alert in engine.process(frame):
                print(json.dumps(alert))
        print(json.dumps({"mode": args.mode, "synthetic": args.mode == "demo", "alerts": store.summary()}, indent=2))
        return
    client = Schwab(str(data_dir))
    configured_universe = os.getenv("LOTTO_UNIVERSE")
    if not configured_universe:
        # Keep a compatible fallback for deployments that already carry the
        # unusual-options scanner's universe variables.
        configured_universe = ",".join(filter(None, (os.getenv("UOA_CORE_UNIVERSE", ""), os.getenv("UOA_IN_PLAY", "")))) or DEFAULT_UNIVERSE
    symbols = universe_symbols(configured_universe)
    if not symbols:
        raise SystemExit("LOTTO_UNIVERSE must contain at least one symbol")
    poll_seconds = max(30, int(os.getenv("LOTTO_POLL_SECONDS", "60")))
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("Lotto worker started: %d symbols, delivery=%s, alert count limits=%s", len(symbols), live_delivery,
             "disabled" if not any((engine.settings.max_alerts_per_day, engine.settings.max_alerts_per_ticker,
                                     engine.settings.max_alerts_per_cycle)) else "configured")
    LOG.info("Strategy %s settings: %s",STRATEGY_VERSION,json.dumps(asdict(engine.settings),sort_keys=True))
    if engine.settings.far_otm_time_mode == 2:
        LOG.warning("LOTTO_FAR_OTM_TIME_MODE=2 blocks far 0DTE strikes. The 0.8 cutoff was not independently re-validated in this build.")
    if engine.settings.dm_atr_basis_mode == 2:
        LOG.warning("LOTTO_DM_ATR_BASIS_MODE=2 blocks on high-low ATR. Live blocking stays on true range unless this override is set.")
    LOG.info("Coverage plan: %d deep slots; continuous=%s; full-universe sweep target=%d minutes",
             client.discovery.capacity,",".join(sorted(client.discovery.always_deep)),client.coverage.interval.seconds//60)
    local_start = datetime.now(ET)
    market_hours = (local_start.weekday() < 5
                    and (9, 30) <= (local_start.hour, local_start.minute) < (16, 0))
    if market_hours:
        LOG.info("Startup diagnostics deferred during market hours; entering live scan loop")
    previous_day = None if market_hours else store.db.execute(
        "SELECT day FROM discovery ORDER BY day DESC LIMIT 1").fetchone()
    if previous_day and previous_day["day"] < local_start.date().isoformat():
        try:
            LOG.info("Prior-session audit: %s",json.dumps(audit_store(store,previous_day["day"],engine.settings)))
        except Exception as exc:
            LOG.warning("Prior-session audit unavailable (%s)",type(exc).__name__)
    if not market_hours:
        try:
            LOG.info("Startup preflight: %s", json.dumps(check(client,symbols,data_dir)))
        except Exception as exc:
            LOG.error("Startup preflight failed (%s); live loop will retry market-data connectivity",type(exc).__name__)
    diagnose = [s.strip().upper() for s in os.getenv("LOTTO_DIAGNOSE_SYMBOLS", "").split(",") if s.strip()]
    if diagnose and not market_hours:
        try:
            LOG.info("Targeted session diagnostic: %s", json.dumps(
                diagnose_symbols(store, datetime.now(ET).date().isoformat(), diagnose, engine.settings)))
        except Exception as exc:
            LOG.warning("Targeted session diagnostic unavailable (%s)", type(exc).__name__)
    hypothesis_day = os.getenv("LOTTO_RULE_AUDIT_DAY", "")
    if hypothesis_day and not market_hours:
        try:
            LOG.info("Coil burst hypothesis audit: %s", json.dumps(
                audit_coil_burst_hypothesis(store, hypothesis_day, data_dir)))
        except Exception as exc:
            LOG.warning("Coil burst hypothesis audit unavailable (%s)", type(exc).__name__)
    forensic_request = os.getenv("LOTTO_FORENSIC_EXPORT", "")
    if forensic_request and not market_hours:
        try:
            requested = json.loads(forensic_request)
            cases = requested if isinstance(requested, list) else [requested]
            if not 1 <= len(cases) <= 10 or not all(isinstance(case, dict) for case in cases):
                raise ValueError("Forensic export accepts one to ten saved cases")
            # Only archived sessions, outside live market hours. Export is read-only
            # against the trading database and contains market observations only.
            now = datetime.now(ET)
            session = client.session(now)
            directory = data_dir / "forensics"
            directory.mkdir(exist_ok=True)
            for case in cases:
                if not export_window_closed(case["day"], now, session):
                    raise ValueError("Forensic exports require a completed session outside live market hours")
                packed, parts = export_case(store, **case)
                (directory / f"{case['day']}-{case['symbol']}.json.gz").write_bytes(packed)
                for part in parts:
                    LOG.info("Forensic export: %s", json.dumps(part, separators=(",", ":")))
                    time.sleep(.04)
                LOG.info("Forensic export complete: %s %d parts", part["case"], len(parts))
        except Exception as exc:
            LOG.warning("Forensic export unavailable (%s)", type(exc).__name__)
    while not stop:
        began = time.monotonic()
        try:
            today = datetime.now(ET).date().isoformat()
            tracked = store.active_options(today)
            frame = client.poll(sorted(set(symbols) | set(tracked)), tracked, engine.settings.max_dte)
            store.record_discovery(client.discovery.observations)
            store.record_chain_coverage(client.coverage.rows)
            # Poll latency cannot turn an old candidate into a current alert.
            now = datetime.now(timezone.utc)
            frame = [s for s in frame if 0 <= (now - s.at).total_seconds() <= 90]
            alerts = engine.process(frame)
            for alert in alerts:
                LOG.info("%s setup %s: %s", alert["phase"], alert["id"], alert["payload"]["embeds"][0]["description"])
            if live_delivery:
                deliver_pending(store, webhook)
            with store.db:
                # Expire intraday trackers after the session even if no further quotes arrive.
                store.db.execute("UPDATE alerts SET closed=1 WHERE day < ?", (today,))
                session = client.session(now)
                if session and now >= session[1]:
                    store.db.execute("UPDATE alerts SET closed=1 WHERE day=?", (today,))
            # Released candidates stay on disk; retaining every name's full chains
            # in RAM all day can exhaust a small Railway worker.
            engine.history = {k: v for k, v in engine.history.items()
                              if k[0] == today and v and now-v[-1].at <= timedelta(minutes=30)}
            LOG.info("Cycle complete: %d symbols observed, %d alerts (%d active, %d potential)",
                     len(frame), len(alerts), sum(a["phase"] == "ACTIVE" for a in alerts),
                     sum(a["phase"] == "POTENTIAL" for a in alerts))
            if live_delivery:
                try:
                    if health_monitor.successful_cycle(now, len(frame)):
                        LOG.info("Operational recovery notice delivered")
                except Exception as exc:
                    LOG.warning("Operational recovery notice unavailable (%s)",type(exc).__name__)
            # Useful reasons must be visible in deployment logs, including a zero-alert day.
            counts = store.db.execute("SELECT reason,COUNT(*) AS n FROM decisions WHERE at>=? GROUP BY reason ORDER BY n DESC LIMIT 5",
                                     ((now-timedelta(minutes=5)).isoformat(),)).fetchall()
            if counts:
                LOG.info("Recent decision reasons: %s", "; ".join(f"{r['n']}× {r['reason']}" for r in counts))
            maybe_nightly(store,client,symbols,data_dir,now)
            client.warm_universe(symbols, now)
        except Exception as exc:
            detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
            LOG.error("Cycle failed (%s); retaining state for retry", detail)
            if live_delivery:
                reason = ("Schwab token broker unavailable" if "Schwab token broker" in detail
                          else "market-data cycle unavailable")
                try:
                    if health_monitor.failed_cycle(datetime.now(timezone.utc), reason):
                        LOG.info("Operational outage notice delivered")
                except Exception as notice_exc:
                    LOG.warning("Operational outage notice unavailable (%s)",type(notice_exc).__name__)
        # Flag 0 returns before any database or network work. Failures stay off the scan loop.
        maybe_day_export(engine.settings, database, datetime.now(timezone.utc))
        if args.once:
            break
        wait = max(1, poll_seconds - (time.monotonic() - began))
        deadline = time.monotonic() + wait
        while not stop and time.monotonic() < deadline:
            time.sleep(max(0, min(1, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
