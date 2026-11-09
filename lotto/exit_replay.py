"""Offline sampled-bid exit replay. This does not change a live stop or post.

Walk quotes in time order. A later bid cannot change an exit already taken.
A jump across the stop during a quote hole is flagged and is not treated as a
fill at the stop. The path is censored when neither stop, target, nor time
stop is observed. This is not an out-of-sample result.
"""
import json
from datetime import datetime, timedelta
from math import isfinite

from .config import Settings
from .features import quote_ok
from .models import ET
from .persistence import assess_session, connect_readonly, require_existing_database, table_exists
from .store import read_snapshot

EXIT_REPLAY_VERSION = "sampled-bid-2026-11-09"
_NOTE = "Sampled bids only. A stop is not a known fill. No live stop was changed."


def build_specs(stops, targets, time_stops) -> list[dict]:
    """Stop/target pairs, then each time stop with the same stops and no target."""
    if not stops:
        stops = [None]
    if not targets:
        targets = [None]
    specs = [{"stop_return": stop, "target_return": target, "time_stop_minutes": None}
             for stop in stops for target in targets]
    for stop in stops:
        for minutes in time_stops:
            specs.append({"stop_return": stop, "target_return": None, "time_stop_minutes": minutes})
    for spec in specs:
        _check_spec(spec["stop_return"], spec["target_return"], spec["time_stop_minutes"])
    return specs


def _check_spec(stop, target, time_stop):
    if stop is not None and not (isfinite(stop) and stop < 0):
        raise ValueError("stop_return must be negative")
    if target is not None and not (isfinite(target) and target > 0):
        raise ValueError("target_return must be positive")
    if time_stop is not None and not (isfinite(time_stop) and time_stop > 0):
        raise ValueError("time_stop_minutes must be positive")


def replay_exit(entry_ask, entry_at, quotes, *, stop_return=-0.35, target_return=None,
                time_stop_minutes=None, max_gap_seconds=120, session_close=None,
                wide_spread_fraction=0.15) -> dict:
    _check_spec(stop_return, target_return, time_stop_minutes)
    base = {
        "version": EXIT_REPLAY_VERSION, "outcome": "unusable", "exit_at": None, "exit_bid": None,
        "exit_return": None, "quotes_used": 0, "flags": [], "max_gap_seconds_observed": 0,
        "stop_return": stop_return, "target_return": target_return, "time_stop_minutes": time_stop_minutes,
        "note": _NOTE,
    }
    if entry_ask is None or not isfinite(entry_ask) or entry_ask <= 0 or getattr(entry_at, "tzinfo", None) is None:
        base["flags"] = ["bad_entry"]
        return base
    flags, seen = set(), 0
    previous_at, previous_return, max_gap = None, None, 0.0
    outcome = exit_at = exit_bid = exit_return = None
    for at, bid, ask in sorted(quotes, key=lambda item: item[0]):
        if getattr(at, "tzinfo", None) is None:
            flags.add("naive_timestamp")
            continue
        if at < entry_at:
            continue
        if bid is None or not isinstance(bid, (int, float)) or not isfinite(bid) or bid < 0:
            flags.add("missing_bid")
            continue
        if ask is None or not isinstance(ask, (int, float)) or not isfinite(ask) or ask <= 0:
            flags.add("missing_ask")
            ask = None
        elif bid > ask:
            flags.add("crossed_market")
            continue
        elif (ask - bid) / ask > wide_spread_fraction:
            flags.add("wide_spread")
        anchor = entry_at if previous_at is None else previous_at
        prior_return = 0.0 if previous_return is None else previous_return
        gap = (at - anchor).total_seconds()
        max_gap = max(max_gap, gap)
        ret = bid / entry_ask - 1
        if gap > max_gap_seconds:
            flags.add("quote_gap")
            if stop_return is not None and prior_return > stop_return >= ret:
                flags.add("stop_crossed_in_gap")
            if target_return is not None and prior_return < target_return <= ret:
                flags.add("target_crossed_in_gap")
        seen += 1
        if stop_return is not None and ret <= stop_return:
            outcome, exit_at, exit_bid, exit_return = "stop", at, bid, ret
            break
        if target_return is not None and ret >= target_return:
            outcome, exit_at, exit_bid, exit_return = "target", at, bid, ret
            break
        if time_stop_minutes is not None and (at - entry_at).total_seconds() >= time_stop_minutes * 60:
            outcome, exit_at, exit_bid, exit_return = "time_stop", at, bid, ret
            break
        previous_at, previous_return = at, ret
    if outcome is None:
        outcome = "censored"
        flags.add("path_censored")
        if session_close is not None and (previous_at is None or previous_at < session_close - timedelta(minutes=1)):
            flags.add("censored_before_close")
    base.update({
        "outcome": outcome,
        "exit_at": exit_at.isoformat() if exit_at is not None else None,
        "exit_bid": exit_bid,
        "exit_return": exit_return,
        "quotes_used": seen,
        "flags": sorted(flags),
        "max_gap_seconds_observed": max_gap,
    })
    return base


def _contract_quotes(connection, symbol, contract, start, end, settings):
    quotes, rejected = [], 0
    if not table_exists(connection, "snapshots"):
        return quotes, rejected
    rows = connection.execute(
        "SELECT payload FROM snapshots WHERE symbol=? AND at>=? AND at<? ORDER BY at",
        (symbol, start, end),
    )
    for row in rows:
        snap = read_snapshot(row[0])
        option = next((item for item in snap.options if item.symbol == contract), None)
        if option is None:
            continue
        if not quote_ok(option, snap, settings):
            rejected += 1
            continue
        quotes.append((option.quote_time, option.bid, option.ask))
    return quotes, rejected


def replay_day(database: str, day: str, specs: list[dict], *, max_gap_seconds=120) -> dict:
    """Read one dated database and replay every stored alert. Does not write the file."""
    require_existing_database(database)
    settings = Settings()
    close = datetime.fromisoformat(day).replace(hour=16, tzinfo=ET)
    end = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    connection = connect_readonly(database)
    try:
        assessment = assess_session(connection, day, db_path=database)
        alerts = []
        if table_exists(connection, "alerts"):
            rows = connection.execute("SELECT * FROM alerts WHERE day=? ORDER BY at", (day,)).fetchall()
            for row in rows:
                entry_at = datetime.fromisoformat(row["at"])
                quotes, rejected = _contract_quotes(
                    connection, row["symbol"], row["contract"], row["at"], end, settings)
                runs = [replay_exit(row["entry_ask"], entry_at, quotes, session_close=close,
                                    max_gap_seconds=max_gap_seconds, **spec) for spec in specs]
                alerts.append({
                    "id": row["id"], "symbol": row["symbol"], "contract": row["contract"],
                    "side": row["side"], "entry_ask": row["entry_ask"], "entry_at": row["at"],
                    "quotes_rejected": rejected, "specs": runs,
                })
    finally:
        connection.close()
    return {
        "version": EXIT_REPLAY_VERSION,
        "day": day,
        "session": assessment,
        "full_day_grade": assessment["grade_as_full_day"],
        "live_stops_changed": False,
        "note": "Offline sampled-bid replay. Not an out-of-sample validation and not a live stop change.",
        "alerts": alerts,
    }


def write_replay(report: dict, dest: str) -> None:
    from pathlib import Path
    target = Path(dest)
    if target.exists():
        raise FileExistsError(f"Refusing to replace existing replay output {dest}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2))
