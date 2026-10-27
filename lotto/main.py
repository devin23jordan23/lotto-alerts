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

from .config import Settings, STRATEGY_VERSION, model_v_settings
from .demo import demo_frames
from .discord import deliver_pending, deliver_stock_pending
from .engine import Engine
from .models import ET, Snapshot
from .schwab import Schwab
from .store import Store
from .stock_first import StockFirst
from .nightly import maybe_nightly, run_nightly
from .health import check
from .audit import audit_store, diagnose_symbols, audit_coil_burst_hypothesis
from .forensics import export_case, export_window_closed
from .operations import HealthMonitor

LOG = logging.getLogger(__name__)

DEFAULT_UNIVERSE = (
    "SPX,SPY,QQQ,IWM,DIA,SMH,SOXX,XLK,XLF,XLE,GLD,USO,SLV,MTUM,AAPL,MSFT,NVDA,TSLA,META,AMZN,GOOGL,NFLX,"
    "AVGO,AMD,PLTR,COIN,HOOD,MSTR,MU,TSM,ARM,QCOM,MRVL,INTC,AMAT,LRCX,KLAC,ASML,SNDK,SMCI,DELL,VRT,"
    "ANET,NBIS,AAOI,SPCX,CSCO,HPE,NOW,ORCL,CRM,ADBE,SNOW,DDOG,NET,CRWD,PANW,ZS,OKTA,TEAM,MDB,SHOP,BE,VST,CEG,NRG,"
    "OKLO,FSLR,ENPH,PYPL,SOFI,AFRM,UPST,RBLX,UBER,ABNB,JPM,BAC,MS,GS,C,SCHW,V,MA,AXP,WMT,TGT,COST,"
    "MCD,CMG,LULU,NKE,SBUX,HD,LOW,LLY,UNH,MRNA,REGN,ISRG,BA,LMT,CAT,DE,XOM,CVX"
)

REQUIRED_UNIVERSE = {"CSCO", "HPE", "SPX", "SPCX"}
# Runner names outside the default universe. Quoted always, chain-scanned only
# while in play. Nia's year list plus the Oct 26 names that had no chain.
DEFAULT_STANDBY = (
    "IREN", "CRWV", "LITE", "TTMI", "HUT", "CORZ", "DKNG", "RIVN", "LUNR", "HIMS",
    "MARA", "SOXL", "GME", "EWZ", "TLT", "RIOT", "XLI", "TEM", "STX", "IBIT", "U",
)


def universe_symbols(raw: str) -> list[str]:
    symbols = {s.strip().upper() for s in raw.split(",") if s.strip()}
    return sorted(symbols | REQUIRED_UNIVERSE) if symbols else []


def excluded_symbols() -> set[str]:
    """LOTTO_EXCLUDE_SYMBOLS. Comma-separated, case-insensitive, default empty."""
    raw = os.getenv("LOTTO_EXCLUDE_SYMBOLS", "")
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def apply_exclusions(symbols, standby=None, continuous=None):
    """Drop excluded names after every universe source has been combined.

    Covers the core list, standby (including UOA names), EXTRA_UNIVERSE names
    already merged into the core, and the continuous / coverage-plan list.
    """
    blocked = excluded_symbols()

    def keep(name) -> bool:
        return str(name).strip().upper() not in blocked

    symbols = [name for name in symbols if keep(name)]
    if standby is not None:
        standby = [name for name in standby if keep(name)]
    if continuous is not None:
        continuous = {name for name in continuous if keep(name)}
    return symbols, standby, continuous, blocked


def _env_on(name: str, default: str = "off") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "on", "true", "yes"}


def standby_symbols() -> list[str]:
    """Runner names. Empty unless LOTTO_STANDBY_NAMES is on.

    When the switch is on, the named list is quoted and chain-scanned only
    while in play. Those symbols are scored on the handoff A bar.
    """
    if not _env_on("LOTTO_STANDBY_NAMES", "off"):
        return []
    raw = os.getenv("LOTTO_STANDBY_UNIVERSE")
    if raw is None:
        named = list(DEFAULT_STANDBY)
    else:
        named = [s.strip().upper() for s in raw.split(",") if s.strip()]
    include_uoa = os.getenv("LOTTO_STANDBY_INCLUDE_UOA", "1").strip().lower() not in {"0", "false", "no"}
    extra = []
    if include_uoa:
        for key in ("UOA_CORE_UNIVERSE", "UOA_IN_PLAY", "UOA_PREMARKET_WATCHLIST"):
            extra.extend(s.strip().upper() for s in os.getenv(key, "").split(",") if s.strip())
    return sorted(set(named) | set(extra))


# Floors belong to the preset. A copied .env must not put the old rounded
# values back on top of S11 or S12.
PWIN_FIELDS = (
    "pwin_normal_0dte", "pwin_normal_1dte",
    "pwin_quintile_0dte", "pwin_quintile_1dte",
    "pwin_decile", "pwin_decile_1dte",
)


def atr_window_status(settings) -> int:
    """One startup line naming the ATR window Model V scores and posts on."""
    days = settings.atr_days
    if days == 14:
        LOG.warning("ATR window: 14 days. prior_atr is the 14-day true-range average.")
    else:
        LOG.warning(
            "ATR window: 21 days. prior_atr is the mean of the prior daily true ranges, today excluded. "
            "Fewer than 15 true ranges leaves prior_atr empty; the 14-day average is not substituted. "
            "LOTTO_ATR_DAYS=14 restores that average."
        )
    return days


def _setting_text(value) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def profile_status(settings) -> str:
    """The active profile and the lines it actually posts on. No cap is applied."""
    LOG.warning(
        "Profile: %s post>=%s bypass>=%s min_positive_features>=%s bolt_features>=%s",
        settings.profile, _setting_text(settings.post_threshold),
        _setting_text(settings.bypass_threshold), settings.min_positive_features,
        settings.strong_feature_min,
    )
    return settings.profile


def fallback_rule_status(settings) -> str:
    """One startup line. RVOL, push, and room are points, not gates."""
    mode = settings.fallback_rule
    LOG.warning(
        "Fallback rule: %s post>=%s bypass>=%s atr=21 contract=roll spread<=%s%% bid>%s opt_vol>=%s",
        mode, _setting_text(settings.post_threshold), _setting_text(settings.bypass_threshold),
        _setting_text(settings.fb_max_spread_pct), _setting_text(settings.blocker_bid),
        _setting_text(settings.blocker_option_volume_5m),
    )
    return mode


def strong_score_status(settings) -> int:
    """⚡ needs the profile post line and the positive-feature count."""
    LOG.warning(
        "Strong score: ⚡ on cards with score>=%s and >= %s positive features",
        _setting_text(settings.post_threshold), settings.strong_feature_min,
    )
    return settings.strong_feature_min


def weights_file_status(settings) -> bool:
    """Loud when the shipped weights book is not on disk. Cards will not post."""
    path = Path(settings.weights_path)
    if path.is_file():
        LOG.warning("Weights file: %s", path.name)
        return True
    LOG.error(
        "WEIGHTS FILE MISSING: %s. No card will post until this file is present.",
        path,
    )
    return False


def freshness_status(settings) -> None:
    """Ages are logged. A stale quote is a flag and a point penalty, not a gate."""
    LOG.warning(
        "Freshness: quote_age_s bar_age_s min_since_trigger logged; stale quote >%ss is a flag and -%s, not a gate",
        _setting_text(settings.stale_quote_seconds), _setting_text(settings.stale_quote_penalty),
    )


def handoff_fallback_status(enabled: bool, requested_mode: str | None) -> str:
    """Whether stock-led option cards are built.

    LOTTO_STOCK_FIRST_MODE is not read by the handoff lane. Shadow does not
    hold those option cards. Turning stock-first off does. Stock-only cards
    never post in either state.
    """
    mode = (requested_mode or "shadow").strip().lower() or "shadow"
    if enabled:
        LOG.warning(
            "Handoff fallback state: posting. LOTTO_STOCK_FIRST_ENABLED=true builds stock-led option cards. "
            "LOTTO_STOCK_FIRST_MODE=%s is not read by that lane. Stock-only cards never post.",
            mode,
        )
        return "posting"
    LOG.warning(
        "Handoff fallback state: shadow. LOTTO_STOCK_FIRST_ENABLED=false disables the handoff lane, "
        "so stock-led option cards will not post. Set LOTTO_STOCK_FIRST_ENABLED=true. "
        "Stock-only cards never post in any mode.",
    )
    return "shadow"


def env_settings() -> Settings:
    model = os.getenv("LOTTO_MODEL", "V").strip().upper()
    if model not in {"V", "S"}:
        raise SystemExit("LOTTO_MODEL must be V or S")
    preset = os.getenv("LOTTO_PRESET", "S12").strip().upper()
    base = (model_v_settings(preset=preset) if model == "V"
            else Settings(model="S", min_score=70, send_requote=0, hybrid=0, soft_blocks=0))
    updates = {}
    for field in fields(base):
        raw = os.getenv("LOTTO_" + field.name.upper())
        if raw is not None and field.name not in {"model", "preset", *PWIN_FIELDS}:
            if field.name in {
                "standby_names", "spread_legacy", "day_volume_swap", "optb",
                "gate_alignment", "gate_s12_bar", "alignment_pack",
                "broad_rows_side_aware", "cooldown_reset_on_level_break",
                "sector_peer_side_aware", "signed_volume",
            }:
                updates[field.name] = 1 if raw.strip().lower() in {"1", "on", "true", "yes"} else 0
            elif field.name == "spread_bands":
                updates[field.name] = tuple(
                    float(piece) for piece in raw.replace(" ", "").split(",") if piece)
            elif field.name == "spread_stock_bars":
                parsed = []
                for piece in raw.split(","):
                    piece = piece.strip()
                    if not piece:
                        continue
                    rvol, push, setups = piece.split("/")
                    parsed.append((float(rvol), float(push), int(float(setups))))
                updates[field.name] = tuple(parsed)
            else:
                updates[field.name] = type(getattr(base, field.name))(raw)
    if "score_mode" in updates:
        updates["score_mode"] = str(updates["score_mode"]).lower()
    if "fallback_rule" in updates:
        updates["fallback_rule"] = str(updates["fallback_rule"]).strip().lower()
    # SHADOW_PROFILE is the operator switch. LOTTO_SHADOW_PROFILE is the same field.
    if os.getenv("SHADOW_PROFILE") is not None:
        updates["shadow_profile"] = os.getenv("SHADOW_PROFILE", "").strip()
    elif "shadow_profile" in updates:
        updates["shadow_profile"] = str(updates["shadow_profile"]).strip()
    updates["model"] = model
    if model == "V":
        updates["preset"] = preset
        if updates.get("min_score", base.min_score) > 60:
            updates["min_score"] = 60
        if updates.get("score_mode", base.score_mode) == "combined" and "preset_note" not in updates:
            updates["preset_note"] = (
                "Rollback: one combined score, bars 40/61, stock share cap 0.40, "
                "plus the exceptional stock_override path."
            )
        for name in PWIN_FIELDS:
            raw = os.getenv("LOTTO_" + name.upper())
            if raw is None:
                continue
            preset_value = getattr(base, name)
            try:
                supplied = float(raw)
            except ValueError:
                supplied = None
            if supplied != preset_value:
                LOG.warning(
                    "Ignoring LOTTO_%s=%s; LOTTO_PRESET=%s uses %s",
                    name.upper(), raw, base.preset, preset_value,
                )
    if model == "V":
        from .weights import active_profile, load_weights
        requested = str(updates.get("profile") or base.profile or "conservative")
        overridden = [
            name for name in ("LOTTO_POST_THRESHOLD", "LOTTO_BYPASS_THRESHOLD", "LOTTO_MIN_POSITIVE_FEATURES")
            if os.getenv(name) is not None
        ]
        try:
            chosen = active_profile(load_weights(updates.get("weights_path")), requested)
            updates.update(chosen)
            if overridden:
                LOG.warning(
                    "LOTTO_PROFILE=%s overrides %s. Set LOTTO_PROFILE to change the line.",
                    chosen["profile"], ", ".join(overridden),
                )
        except (OSError, ValueError) as exc:
            LOG.error(
                "Weights book failed to load (%s); conservative defaults are in use "
                "(post>=92 bypass>=40 min_positive_features>=0 bolt>=10)",
                type(exc).__name__,
            )
            updates.update({
                "profile": "conservative",
                "post_threshold": 92,
                "bypass_threshold": 40,
                "min_positive_features": 0,
                "strong_feature_min": 10,
            })
    if int(updates.get("alignment_pack", getattr(base, "alignment_pack", 0)) or 0):
        from .alignment import PACK
        for name, proposed in PACK.items():
            if os.getenv("LOTTO_" + name.upper()) is None:
                updates[name] = proposed
    return replace(base, **updates)


def replay_frames(path):
    with open(path) as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                yield [Snapshot.from_dict(s) for s in (value if isinstance(value, list) else [value])]


def main():
    parser = argparse.ArgumentParser(description="Potential watches and active options ideas")
    parser.add_argument("mode", choices=("demo", "replay", "live", "report", "nightly", "check", "feedback"), nargs="?", default="demo")
    parser.add_argument("--input", help="Replay JSONL: one snapshot array per scan cycle")
    parser.add_argument("--db", help="SQLite state path")
    parser.add_argument("--day", help="Session date for nightly EOD analysis (YYYY-MM-DD)")
    parser.add_argument("--once", action="store_true", help="Poll one live cycle, retaining warmup requirements")
    parser.add_argument("--alert-id", help="Alert ID from the Discord footer for feedback")
    parser.add_argument("--outcome", choices=("worked","failed","mixed"))
    parser.add_argument("--notes", default="", help="Trader feedback; separate from quote-derived outcomes")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "check":
        symbols, _, _, _ = apply_exclusions(universe_symbols(os.getenv("LOTTO_UNIVERSE") or DEFAULT_UNIVERSE))
        result=check(Schwab(str(data_dir)),symbols,data_dir)
        print(json.dumps(result,indent=2))
        raise SystemExit(0 if result['ready'] else 1)
    database = args.db or str(data_dir / ("live.sqlite3" if args.mode in {"live", "report", "nightly", "feedback"} else f"{args.mode}.sqlite3"))
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
    engine.fallback_log = data_dir / "fallback-rule.jsonl"
    # Stock cards stay internal even if LOTTO_STOCK_FIRST_MODE=send.
    stock_first = StockFirst(store,
        enabled=os.getenv("LOTTO_STOCK_FIRST_ENABLED", "true").lower() == "true",
        mode="shadow",
        max_quote_age=engine.settings.max_quote_age_seconds)
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
    standby = [s for s in standby_symbols() if s not in symbols]
    from .volume_trend import (
        ensure_etf_typicals, install_extra_universe, is_volume_trend, pin_deep_names, volume_trend_symbols,
    )
    if is_volume_trend(engine.settings.profile):
        symbols = volume_trend_symbols(symbols)
        standby = [s for s in standby if s not in symbols]
        pin_deep_names(client.discovery)
        symbols, _loaded, _budget = install_extra_universe(symbols, client, engine.settings)
        ensure_etf_typicals(client, datetime.now(timezone.utc))
    symbols, standby, continuous, blocked = apply_exclusions(
        symbols, standby, client.discovery.always_deep)
    client.discovery.always_deep = continuous
    engine.runner_names = frozenset(standby)
    LOG.info("Excluded symbols: %s core=%d", ",".join(sorted(blocked)), len(symbols))
    if not symbols:
        raise SystemExit("LOTTO_UNIVERSE must contain at least one symbol")
    poll_seconds = max(30, int(os.getenv("LOTTO_POLL_SECONDS", "60")))
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("Lotto worker started: %d symbols, delivery=%s, alert count limits=%s, post cooldown=%s, alignment gate=%s, s12 bar gate=%s",
             len(symbols), live_delivery,
             "disabled" if not any((engine.settings.max_alerts_per_day, engine.settings.max_alerts_per_ticker,
                                     engine.settings.max_alerts_per_cycle)) else "configured",
             "off" if engine.settings.cooldown_minutes <= 0 else f"{engine.settings.cooldown_minutes}m per ticker side",
             "on" if engine.settings.gate_alignment else "off",
             "on" if engine.settings.gate_s12_bar else "off")
    LOG.info("Strategy %s settings: %s",STRATEGY_VERSION,json.dumps(asdict(engine.settings),sort_keys=True))
    LOG.info('Quality policy: mode=%s policy=%s secondary_delivery=%s disabled=%s',
             engine.quality.mode,engine.quality.policy,
             'configured' if os.getenv('DISCORD_LOTTO_SECONDARY_WEBHOOK') else 'internal',
             ','.join(sorted(engine.quality.disabled_rules)) or 'none')
    handoff_fallback_status(stock_first.enabled, os.getenv("LOTTO_STOCK_FIRST_MODE"))
    profile_status(engine.settings)
    fallback_rule_status(engine.settings)
    strong_score_status(engine.settings)
    freshness_status(engine.settings)
    weights_file_status(engine.settings)
    atr_window_status(engine.settings)
    from .model_v import scan_budget
    LOG.info("Model %s score_mode=%s preset=%s standby_names=%s standby=%s",
             engine.settings.model, engine.settings.score_mode, engine.settings.preset,
             "on" if engine.settings.standby_names else "off", ",".join(standby) or "none")
    LOG.info("Scan budget: %s", json.dumps(scan_budget(
        len(symbols), len(standby), engine.settings.standby_scan_minutes,
        engine.settings.quote_batch, client.min_request_interval,
        scans_per_cycle=engine.settings.standby_scans_per_cycle)))
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
            def on_discovery(rows):
                ideas = stock_first.process(rows, dry_run=not live_delivery)
                for idea in ideas:
                    LOG.info("Stock-first %s %s %s at %.2f (logged, not posted)", idea.symbol, idea.side, idea.setup, idea.price)
                if engine.settings.model == "V":
                    engine.remember_stock_events(ideas)
                    return engine.handoff_symbols()
                return []
            frame = client.poll(sorted(set(symbols) | set(standby) | set(tracked)), tracked,
                                engine.settings.max_dte, on_discovery=on_discovery, standby=standby,
                                in_play_settings=engine.settings)
            store.record_discovery(client.discovery.observations)
            store.record_chain_coverage(client.coverage.rows)
            # Poll latency cannot turn an old candidate into a current alert.
            now = datetime.now(timezone.utc)
            frame = [s for s in frame if 0 <= (now - s.at).total_seconds() <= 90]
            alerts = engine.process(frame)
            for alert in alerts:
                LOG.info("%s setup %s: %s", alert["phase"], alert["id"], alert["payload"]["embeds"][0]["description"])
            if live_delivery:
                deliver_pending(store, webhook,
                                quoter=client.send_quote if engine.settings.send_requote else None)
                deliver_stock_pending(store, webhook)
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
            for symbol in diagnose:
                row = store.db.execute(
                    "SELECT at,state,reason,score FROM decisions WHERE symbol=? AND at>=? ORDER BY at DESC LIMIT 1",
                    (symbol, (now-timedelta(seconds=90)).isoformat())).fetchone()
                if row:
                    LOG.info('Live decision %s: %s', symbol, json.dumps(dict(row)))
            maybe_nightly(store,client,symbols,data_dir,now)
            if is_volume_trend(engine.settings.profile): ensure_etf_typicals(client, now)
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
        if args.once:
            break
        wait = max(1, poll_seconds - (time.monotonic() - began))
        deadline = time.monotonic() + wait
        while not stop and time.monotonic() < deadline:
            time.sleep(max(0, min(1, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
