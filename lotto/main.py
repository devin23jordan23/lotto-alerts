import argparse
import fcntl
import json
import logging
import os
import signal
import time
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .demo import demo_frames
from .discord import deliver_pending
from .engine import Engine
from .models import ET, Snapshot
from .schwab import Schwab
from .store import Store
from .nightly import maybe_nightly, run_nightly
from .health import check
from .audit import audit_store, diagnose_symbols
from .forensics import export_case

LOG = logging.getLogger(__name__)

DEFAULT_UNIVERSE = (
    "SPY,QQQ,IWM,DIA,SMH,SOXX,XLK,XLF,XLE,GLD,USO,SLV,MTUM,AAPL,MSFT,NVDA,TSLA,META,AMZN,GOOGL,NFLX,"
    "AVGO,AMD,PLTR,COIN,HOOD,MSTR,MU,TSM,ARM,QCOM,MRVL,INTC,AMAT,LRCX,KLAC,ASML,SNDK,SMCI,DELL,VRT,"
    "ANET,NBIS,AAOI,NOW,ORCL,CRM,ADBE,SNOW,DDOG,NET,CRWD,PANW,ZS,OKTA,TEAM,MDB,SHOP,BE,VST,CEG,NRG,"
    "OKLO,FSLR,ENPH,PYPL,SOFI,AFRM,UPST,RBLX,UBER,ABNB,JPM,BAC,MS,GS,C,SCHW,V,MA,AXP,WMT,TGT,COST,"
    "MCD,CMG,LULU,NKE,SBUX,HD,LOW,LLY,UNH,MRNA,REGN,ISRG,BA,LMT,CAT,DE,XOM,CVX"
)


def env_settings() -> Settings:
    defaults = Settings()
    updates = {}
    for field in fields(defaults):
        raw = os.getenv("LOTTO_" + field.name.upper())
        if raw is not None:
            updates[field.name] = type(getattr(defaults, field.name))(raw)
    return replace(defaults, **updates)


def replay_frames(path):
    with open(path) as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                yield [Snapshot.from_dict(s) for s in (value if isinstance(value, list) else [value])]


def main():
    parser = argparse.ArgumentParser(description="Selective potential options runner alerts")
    parser.add_argument("mode", choices=("demo", "replay", "live", "report", "nightly", "check"), nargs="?", default="demo")
    parser.add_argument("--input", help="Replay JSONL: one snapshot array per scan cycle")
    parser.add_argument("--db", help="SQLite state path")
    parser.add_argument("--day", help="Session date for nightly EOD analysis (YYYY-MM-DD)")
    parser.add_argument("--once", action="store_true", help="Poll one live cycle, retaining warmup requirements")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "check":
        symbols=sorted({s.strip().upper() for s in os.getenv("LOTTO_UNIVERSE",DEFAULT_UNIVERSE).split(',') if s.strip()})
        result=check(Schwab(str(data_dir)),symbols,data_dir)
        print(json.dumps(result,indent=2))
        raise SystemExit(0 if result['ready'] else 1)
    database = args.db or str(data_dir / ("live.sqlite3" if args.mode in {"live", "report", "nightly"} else f"{args.mode}.sqlite3"))
    if args.mode == "report":
        print(json.dumps(Store(database).summary(), indent=2))
        return
    if args.mode == "nightly":
        store = Store(database)
        day = args.day or (datetime.now(ET).date() - timedelta(days=1)).isoformat()
        report_path = run_nightly(store,day,data_dir)
        print(json.dumps({"day":day,"report":str(report_path)},indent=2))
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    lock = open(database + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another worker already owns this database; use one Railway replica")
    store = Store(database)
    live_delivery = args.mode == "live" and os.getenv("LOTTO_SEND_ALERTS", "false").lower() == "true"
    webhook = os.getenv("DISCORD_LOTTO_WEBHOOK", "")
    if live_delivery and not webhook:
        raise SystemExit("DISCORD_LOTTO_WEBHOOK is required when LOTTO_SEND_ALERTS=true")
    engine = Engine(store, env_settings(), dry_run=not live_delivery)
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
    symbols = sorted({s.strip().upper() for s in configured_universe.split(",") if s.strip()})
    if not symbols:
        raise SystemExit("LOTTO_UNIVERSE must contain at least one symbol")
    poll_seconds = max(30, int(os.getenv("LOTTO_POLL_SECONDS", "60")))
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("Lotto worker started: %d symbols, delivery=%s, daily cap=%d", len(symbols), live_delivery, engine.settings.max_alerts_per_day)
    previous_day=store.db.execute("SELECT day FROM discovery ORDER BY day DESC LIMIT 1").fetchone()
    if previous_day and previous_day["day"] < datetime.now(ET).date().isoformat():
        try:
            LOG.info("Prior-session audit: %s",json.dumps(audit_store(store,previous_day["day"],engine.settings)))
        except Exception as exc:
            LOG.warning("Prior-session audit unavailable (%s)",type(exc).__name__)
    try:
        LOG.info("Startup preflight: %s", json.dumps(check(client,symbols,data_dir)))
    except Exception as exc:
        LOG.error("Startup preflight failed (%s); live loop will retry market-data connectivity",type(exc).__name__)
    diagnose = [s.strip().upper() for s in os.getenv("LOTTO_DIAGNOSE_SYMBOLS", "").split(",") if s.strip()]
    if diagnose:
        try:
            LOG.info("Targeted session diagnostic: %s", json.dumps(
                diagnose_symbols(store, datetime.now(ET).date().isoformat(), diagnose, engine.settings)))
        except Exception as exc:
            LOG.warning("Targeted session diagnostic unavailable (%s)", type(exc).__name__)
    forensic_request = os.getenv("LOTTO_FORENSIC_EXPORT", "")
    if forensic_request:
        try:
            case = json.loads(forensic_request)
            # Only archived sessions, outside live market hours. Export is read-only
            # against the trading database and contains market observations only.
            now = datetime.now(ET)
            session = client.session(now)
            if date.fromisoformat(case["day"]) >= now.date() or (session and session[0] <= now < session[1]):
                raise ValueError("Forensic exports require an archived day outside live market hours")
            packed, parts = export_case(store, **case)
            directory = data_dir / "forensics"
            directory.mkdir(exist_ok=True)
            (directory / f"{case['day']}-{case['symbol']}.json.gz").write_bytes(packed)
            for part in parts:
                LOG.info("Forensic export: %s", json.dumps(part, separators=(",", ":")))
                time.sleep(.04)
            LOG.info("Forensic export complete: %d parts", len(parts))
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
                LOG.info("Potential setup %s: %s", alert["id"], alert["payload"]["embeds"][0]["description"])
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
            LOG.info("Cycle complete: %d symbols observed, %d potential alerts", len(frame), len(alerts))
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
        if args.once:
            break
        wait = max(1, poll_seconds - (time.monotonic() - began))
        deadline = time.monotonic() + wait
        while not stop and time.monotonic() < deadline:
            time.sleep(max(0, min(1, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
