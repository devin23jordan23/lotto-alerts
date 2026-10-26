"""Read-only, causal stock-mover sweep over saved scanner snapshots.

This screens trigger rules without running the Hybrid option-led lane. It grades
the same MOVER-selected contract using fresh sampled bids, and also reports
stock follow-through. It does not model Discord send-time re-quotes or fills.

Usage: python3 scripts/sweep_fast_movers.py <snapshot_db> <output_json>
"""
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import product
from math import isfinite
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from lotto.models import ET
from lotto.store import read_snapshot


DAYS = ("2026-10-16", "2026-10-19", "2026-10-20", "2026-10-23")
GRID = [
    (lookback, pace, opening, base, False)
    for lookback in (2, 5, 10, 15)
    for pace in ((.10, .15, .20, .25) if lookback <= 5 else (.20, .25, .35, .50))
    for opening in (.25, .50, .75)
    for base in (False, True)
]
GRID.append((15, .25, .25, True, True))  # exact current fallback before 9:45


def ref_close(bars, minutes):
    cutoff = bars[-1].end - timedelta(minutes=minutes)
    for bar in reversed(bars):
        if bar.end <= cutoff:
            return bar.close
    return None


def base_break(bars, direction):
    if len(bars) < 2:
        return False
    extreme = lambda b: b.high if direction == 1 else -b.low
    running = extreme(bars[0])
    flags = []
    for bar in bars[1:]:
        value = extreme(bar)
        flags.append((bar.end, value > running))
        running = max(running, value)
    cutoff = bars[-1].end - timedelta(minutes=15)
    return flags[-1][1] and not any(flag for at, flag in flags[:-1] if at >= cutoff)


def selected_option(snap, direction):
    side = "CALL" if direction == 1 else "PUT"
    today = snap.at.astimezone(ET).date()
    pool = []
    for option in snap.options:
        if option.side != side or option.ask is None or option.bid is None:
            continue
        if not (.15 <= option.ask and 0 < option.bid < option.ask):
            continue
        if (option.expiry - today).days not in (0, 1):
            continue
        if not -.02 <= direction * (option.strike / snap.spot - 1) <= .08:
            continue
        if not 0 <= (snap.at - option.quote_time).total_seconds() <= 90:
            continue
        if abs((option.quote_time - snap.spot_time).total_seconds()) > 90:
            continue
        if (option.ask - option.bid) / option.ask > .35 and option.ask - option.bid > .05:
            continue
        pool.append(option)
    if not pool:
        return None
    nearest = min(option.expiry for option in pool)
    pool = [option for option in pool if option.expiry == nearest]

    def rank(option):
        delta = option.delta
        good = delta is not None and isfinite(delta) and 0 < abs(delta) <= 1
        fit = (abs(abs(delta) - .30) if good else
               abs(direction * (option.strike / snap.spot - 1) - .005) * 25)
        return round(fit, 3), (option.ask - option.bid) / option.ask

    return min(pool, key=rank)


def config_name(cfg):
    lookback, pace, opening, base, fallback = cfg
    return f"r{lookback}_{pace:g}_open{opening:g}_base{int(base)}_fallback{int(fallback)}"


def run(db_path):
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    events = {}
    membership = defaultdict(set)
    snapshots_read = 0
    started = time.monotonic()
    for day in DAYS:
        states = defaultdict(lambda: [None, False])
        spy_bars = None
        rows = source.execute("SELECT payload FROM snapshots WHERE at>=? AND at<? ORDER BY at",
                              (day + "T13:30", day + "T20:01"))
        for (payload,) in rows:
            snap = read_snapshot(payload)
            snapshots_read += 1
            if snap.symbol == "SPY" and snap.bars:
                spy_bars = snap.bars
            if not snap.bars or not snap.prior_atr or snap.prior_atr <= 0 or not snap.spot:
                continue
            last = snap.bars[-1]
            if not 0 <= (snap.at - last.end).total_seconds() <= 180:
                continue
            if not 0 <= (snap.at - snap.spot_time).total_seconds() <= 180:
                continue
            if last.end > snap.at or last.end.astimezone(ET).date() != snap.at.astimezone(ET).date():
                continue
            local = snap.at.astimezone(ET)
            if not (9 <= local.hour < 16) or (local.hour == 9 and local.minute < 31):
                continue
            option_cache = {}
            base_cache = {}
            ref_cache = {n: ref_close(snap.bars, n) for n in (2, 5, 10, 15)}
            spy_cache = {}
            for cfg in GRID:
                lookback, pace, opening, base, fallback = cfg
                ref = ref_cache[lookback]
                if ref is None and not fallback:
                    continue
                if ref is None:
                    ref = snap.bars[0].open
                if lookback not in spy_cache:
                    spy_ref = ref_close(spy_bars, lookback) if spy_bars else None
                    spy_cache[lookback] = ((spy_bars[-1].close / spy_ref - 1) if spy_ref
                                           and 0 <= (snap.at - spy_bars[-1].end).total_seconds() <= 180
                                           else None)
                for direction in (1, -1):
                    spy_return = spy_cache[lookback]
                    if spy_return is not None and direction * spy_return < 0:
                        continue
                    from_open = direction * (last.close - snap.bars[0].open) / snap.prior_atr
                    move = direction * (last.close - ref) / snap.prior_atr
                    if base and direction not in base_cache:
                        base_cache[direction] = base_break(snap.bars, direction)
                    condition = (move >= pace and from_open >= opening) or (
                        base and from_open >= max(.5, opening) and base_cache[direction])
                    key = (cfg, snap.symbol, direction)
                    state = states[key]
                    if not condition:
                        continue
                    if state[0] is not None and last.end - state[0] > timedelta(minutes=15):
                        state[1] = False
                    state[0] = last.end
                    if state[1]:
                        continue
                    if direction not in option_cache:
                        option_cache[direction] = selected_option(snap, direction)
                    option = option_cache[direction]
                    if option is None:
                        continue  # Stock trigger remains active and can price later.
                    state[1] = True
                    event_key = (day, snap.symbol, direction, snap.at.isoformat(), option.symbol)
                    membership[config_name(cfg)].add(event_key)
                    if event_key not in events:
                        intrinsic = max(0, direction * (snap.spot - option.strike))
                        events[event_key] = {
                            "day": day, "symbol": snap.symbol, "side": option.side,
                            "at": snap.at, "spot": snap.spot, "contract": option.symbol,
                            "ask": option.ask, "entry_bid": option.bid,
                            "entry_quote_time": option.quote_time, "last_quote_time": option.quote_time,
                            "intrinsic_gap": max(0, intrinsic - option.ask),
                            "stock_10": 0., "stock_15": 0., "stock_30": 0.,
                            "bid_10": option.bid / option.ask - 1,
                            "bid_15": option.bid / option.ask - 1,
                            "bid_30": option.bid / option.ask - 1,
                            "min_before40": 0., "hit40_at": None, "quotes": 0,
                        }
        print("trigger pass", day, "snapshots", snapshots_read, "events", len(events), flush=True)

    # One second pass grades every unique event, regardless of how many configs chose it.
    by_symbol = defaultdict(list)
    by_contract = defaultdict(list)
    for event in events.values():
        by_symbol[(event["day"], event["symbol"])].append(event)
        by_contract[(event["day"], event["symbol"], event["contract"])].append(event)
    for day in DAYS:
        rows = source.execute("SELECT payload FROM snapshots WHERE at>=? AND at<? ORDER BY at",
                              (day + "T13:30", day + "T20:01"))
        for (payload,) in rows:
            snap = read_snapshot(payload)
            symbol_events = by_symbol.get((day, snap.symbol), ())
            if not symbol_events:
                continue
            active = []
            for event in symbol_events:
                minutes = (snap.at - event["at"]).total_seconds() / 60
                if not 0 < minutes <= 30:
                    continue
                direction = 1 if event["side"] == "CALL" else -1
                favorable = direction * (snap.spot / event["spot"] - 1)
                for n in (10, 15, 30):
                    if minutes <= n:
                        event[f"stock_{n}"] = max(event[f"stock_{n}"], favorable)
                active.append(event)
            if not active:
                continue
            for option in snap.options:
                candidate_events = by_contract.get((day, snap.symbol, option.symbol), ())
                if not candidate_events or option.bid is None or option.bid <= 0:
                    continue
                for event in candidate_events:
                    minutes = (snap.at - event["at"]).total_seconds() / 60
                    if not 0 < minutes <= 30 or option.quote_time <= event["last_quote_time"]:
                        continue
                    if not 0 <= (snap.at - option.quote_time).total_seconds() <= 90:
                        continue
                    event["last_quote_time"] = option.quote_time
                    event["quotes"] += 1
                    value = option.bid / event["ask"] - 1
                    for n in (10, 15, 30):
                        if minutes <= n:
                            event[f"bid_{n}"] = max(event[f"bid_{n}"], value)
                    if event["hit40_at"] is None:
                        event["min_before40"] = min(event["min_before40"], value)
                        if value >= .40:
                            event["hit40_at"] = minutes
        print("grade pass", day, flush=True)
    source.close()

    summary = []
    for cfg in GRID:
        name = config_name(cfg)
        group = [events[key] for key in membership[name]]
        valid = [event for event in group if event["quotes"] and event["intrinsic_gap"] <= 0]
        hits = {n: sum(event[f"bid_{n}"] >= .40 for event in valid) for n in (10, 15, 30)}
        clean28 = sum(event["hit40_at"] is not None and event["min_before40"] > -.28 for event in valid)
        oct23 = [(event["symbol"], event["side"], event["at"].astimezone(ET).strftime("%H:%M"))
                 for event in group if event["day"] == "2026-10-23" and
                 event["at"].astimezone(ET).hour == 9 and event["at"].astimezone(ET).minute < 60]
        summary.append({
            "config": name, "lookback_minutes": cfg[0], "pace_atr": cfg[1],
            "from_open_atr": cfg[2], "base_break": cfg[3], "fallback_to_open": cfg[4],
            "alerts": len(group), "per_day": round(len(group) / len(DAYS), 2),
            "priced_valid": len(valid), "intrinsic_inconsistent": sum(e["intrinsic_gap"] > 0 for e in group),
            "bid40_10": hits[10], "bid40_15": hits[15], "bid40_30": hits[30],
            "clean28_30": clean28,
            "stock_up03_15": sum(e["stock_15"] >= .003 for e in group),
            "panw_crwd_net_by_10": {s: min((t for sym, side, t in oct23 if sym == s and side == "CALL"), default=None)
                                     for s in ("PANW", "CRWD", "NET")},
        })
    return {"days": DAYS, "snapshots_read": snapshots_read, "unique_events": len(events),
            "elapsed_seconds": round(time.monotonic() - started, 1), "summary": summary}


if __name__ == "__main__":
    result = run(sys.argv[1])
    with open(sys.argv[2], "w") as handle:
        json.dump(result, handle, indent=2)
    print("DONE", result["snapshots_read"], result["unique_events"], result["elapsed_seconds"], flush=True)
