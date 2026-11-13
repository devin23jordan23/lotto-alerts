"""Point-in-time stock-event coverage diagnostic for the local history archive.

Uses archived stock paths only. It cannot estimate option win rates or replace
the full four-session option-snapshot replay required before Model T ships.
"""

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def read_history(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for day, symbol, at, raw in con.execute(
            "SELECT day,symbol,at,row FROM stock_paths ORDER BY day,symbol,at"):
        yield day, symbol, datetime.fromisoformat(at), json.loads(raw)


def forward_grade(rows, index, side, atr, intraday_unit=None):
    start = rows[index][0]
    entry = rows[index][1]["spot"]
    path = []
    for t, r in rows[index + 1:]:
        if t > start + timedelta(minutes=30):
            break
        path.append((t, side * (r["spot"] - entry) / atr))
    if not path:
        return None
    gain25 = next((t for t, value in path if value >= .25), None)
    stop15 = next((t for t, value in path if value <= -.15), None)
    gain50 = next((t for t, value in path if value >= .5), None)
    stop25 = next((t for t, value in path if value <= -.25), None)
    stop50 = next((t for t, value in path if value <= -.5), None)
    gain_intra = stop_intra = None
    if intraday_unit and intraday_unit > 0:
        gain_intra = next((t for t, value in path if value >= intraday_unit / atr), None)
        stop_intra = next((t for t, value in path if value <= -intraday_unit / atr), None)
    return {"clean25": gain25 is not None and (stop15 is None or gain25 < stop15),
            "clean50": gain50 is not None and (stop25 is None or gain50 < stop25),
            "clean25_symmetric": gain25 is not None and (stop25 is None or gain25 < stop25),
            "clean50_symmetric": gain50 is not None and (stop50 is None or gain50 < stop50),
            "clean_intraday_symmetric": (gain_intra is not None and
                (stop_intra is None or gain_intra < stop_intra)) if intraday_unit else None,
            "peak25": max(value for _, value in path) >= .25,
            "peak50": max(value for _, value in path) >= .5,
            "peak_atr": round(max(value for _, value in path), 3)}


def prior_intraday_unit(rows, index):
    """Thirty-minute price-volatility unit from prior observed one-minute changes only."""
    prior = rows[max(0, index - 30):index]
    changes = [r["spot"] - previous["spot"]
               for (t0, previous), (t1, r) in zip(prior, prior[1:])
               if 0 < (t1 - t0).total_seconds() <= 120]
    if len(changes) < 10:
        return None
    return math.sqrt(sum(change * change for change in changes) / len(changes) * 30)


def group_levels(rows):
    first3, first5 = [], []
    session_open = None
    prefix_high, prefix_low, last_high, last_low = [], [], [], []
    high = low = high_time = low_time = None
    for at, row in rows:
        bar = row.get("bar")
        if bar:
            local_time = at.astimezone(ET).time().isoformat()
            if session_open is None:
                session_open = bar["open"]
            if local_time < "09:34:00" and len(first3) < 3:
                first3.append(bar)
            if local_time < "09:36:00" and len(first5) < 5:
                first5.append(bar)
        spot = row["spot"]
        prefix_high.append(high)
        prefix_low.append(low)
        last_high.append(high_time)
        last_low.append(low_time)
        if high is None or spot >= high:
            high, high_time = spot, at
        if low is None or spot <= low:
            low, low_time = spot, at
    return {"first3": first3, "first5": first5, "open": session_open,
            "high": prefix_high, "low": prefix_low,
            "last_high": last_high, "last_low": last_low}


def event_features(rows, index, levels, opening_covered):
    at, row = rows[index]
    prior = rows[index - 1][1] if index else None
    if prior is None or not row.get("spot") or not prior.get("spot"):
        return []
    atr = row.get("prior_atr")
    if not isinstance(atr, (int, float)) or atr <= 0:
        return []
    local = at.astimezone(ET)
    if not (9 <= local.hour <= 15) or local.time().isoformat() < "09:31:00":
        return []
    recent = [(t, r) for t, r in rows[max(0, index - 30):index]
              if at - timedelta(minutes=18) <= t < at and r.get("spot")]
    if not recent:
        return []
    older = min(recent, key=lambda p: abs((at - p[0]).total_seconds() - 900))
    first3, first5 = levels["first3"], levels["first5"]
    events = []
    for side, name in ((1, "CALL"), (-1, "PUT")):
        motion15 = side * (row["spot"] - older[1]["spot"]) / atr
        # The first completed regular-session bar's open is the only
        # same-session opening value available in this archive.
        open_price = levels["open"]
        if opening_covered and not open_price:
            continue
        move_open = side * (row["spot"] - open_price) / atr if opening_covered else None
        prior_extreme = levels["high"][index] if side == 1 else levels["low"][index]
        fresh_extreme = side * (row["spot"] - prior_extreme) > 0
        last_extreme = levels["last_high"][index] if side == 1 else levels["last_low"][index]
        pause = (at - last_extreme).total_seconds() / 60 if last_extreme else 0
        if opening_covered and fresh_extreme and motion15 >= .25 and move_open >= .25:
            events.append(("PUSH", prior_extreme, name, motion15, move_open))
        if opening_covered and fresh_extreme and pause >= 15 and move_open >= .5:
            events.append(("BASE_BREAK", prior_extreme, name, motion15, move_open))
        level_rows = [("PRIOR_DAY", row.get("prior_high") if side == 1 else row.get("prior_low")),
                      ("PREMARKET", row.get("premarket_high") if side == 1 else row.get("premarket_low"))]
        if opening_covered and local.time().isoformat() >= "09:34:00" and len(first3) >= 3:
            level_rows.append(("ORB3", max(b["high"] for b in first3[:3]) if side == 1 else min(b["low"] for b in first3[:3])))
        if opening_covered and local.time().isoformat() >= "09:36:00" and len(first5) >= 5:
            level_rows.append(("ORB5", max(b["high"] for b in first5[:5]) if side == 1 else min(b["low"] for b in first5[:5])))
        for kind, level in level_rows:
            if level and side * (prior["spot"] - level) <= 0 < side * (row["spot"] - level):
                events.append((kind, level, name, motion15, move_open))
    return events


def run(db):
    history = defaultdict(list)
    for day, symbol, at, row in read_history(db):
        quote_at = row.get("spot_time")
        if not quote_at or not row.get("spot"):
            continue
        age = (at - datetime.fromisoformat(quote_at)).total_seconds()
        if 0 <= age <= 90:
            history[(day, symbol)].append((at, row))
    result = {"definition": "tracked-name stock path events only; forward stock movement, not option returns; late-start symbol-days excluded from session-open/range/ORB events but eligible for prior-day and premarket level crosses",
              "events": {}, "by_day": {}, "examples": [], "coverage": {"tracked_name_days": len(history), "late_start_excluded": []}}
    totals = defaultdict(list)
    controls = defaultdict(list)
    for (day, symbol), rows in history.items():
        opening_covered = rows[0][0].astimezone(ET).time().isoformat() <= "09:32:00"
        if not opening_covered:
            result["coverage"]["late_start_excluded"].append({"day": day, "symbol": symbol,
                "first_at": rows[0][0].isoformat()})
        seen = set()
        levels = group_levels(rows)
        for index, (at, row) in enumerate(rows):
            for kind, level, side_name, motion15, move_open in event_features(rows, index, levels, opening_covered):
                atr = row.get("prior_atr")
                identity = (side_name, kind, round(level / atr * 20))
                if identity in seen:
                    continue
                seen.add(identity)
                side = 1 if side_name == "CALL" else -1
                outcome = forward_grade(rows, index, side, atr, prior_intraday_unit(rows, index))
                if outcome is None:
                    continue
                # A matched random minute on the same name/day/side and in the
                # same 30-minute clock bucket; exclude the event's +/-5m area.
                bucket = at.astimezone(ET).hour * 2 + at.astimezone(ET).minute // 30
                choices = [j for j, (control_at, control_row) in enumerate(rows)
                           if control_at.astimezone(ET).hour * 2 + control_at.astimezone(ET).minute // 30 == bucket
                           and abs((control_at - at).total_seconds()) > 300
                           and isinstance(control_row.get("prior_atr"), (int, float))
                           and control_row["prior_atr"] > 0]
                if choices:
                    digest = hashlib.sha256(f"{day}:{symbol}:{side_name}:{kind}:{at.isoformat()}".encode()).digest()
                    control_index = choices[int.from_bytes(digest[:8], "big") % len(choices)]
                    control_atr = rows[control_index][1]["prior_atr"]
                    control_outcome = forward_grade(rows, control_index, side, control_atr,
                                                    prior_intraday_unit(rows, control_index))
                    if control_outcome:
                        controls[kind].append(control_outcome)
                high, low = levels["high"][index], levels["low"][index]
                range_position = (side * (row["spot"] - low) / (high - low) if side == 1 else
                                  side * (row["spot"] - high) / (high - low)) if high and low and high > low else None
                metrics = (row.get("metrics") or {}).get(str(side)) or {}
                signed_market = row.get("market_return_5m")
                if isinstance(signed_market, (int, float)):
                    signed_market *= side
                item = {"day": day, "symbol": symbol, "at": at.isoformat(),
                        "kind": kind, "side": side_name, "motion15_atr": round(motion15, 3),
                        "move_open_atr": round(move_open, 3) if move_open is not None else None,
                        "break_distance_atr": round(side * (row["spot"] - level) / atr, 3),
                        "range_position": round(range_position, 3) if opening_covered and range_position is not None else None,
                        "vwap_distance_atr": metrics.get("distance_from_vwap_atr"),
                        "local_rvol": metrics.get("local_rvol_5m"),
                        "market_5m_side": signed_market,
                        "day_range_atr": round((high - low) / atr, 3) if opening_covered and high and low else None,
                        **outcome}
                totals[kind].append(item)
                result["by_day"].setdefault(day, []).append(item)
                if symbol in {"META", "AAPL", "AMZN", "MSFT", "SPY"} and len(result["examples"]) < 150:
                    result["examples"].append(item)
    def stats(items):
        n = len(items)
        output = {"n": n}
        for field in ("clean25", "clean50", "clean25_symmetric", "clean50_symmetric",
                      "clean_intraday_symmetric", "peak25", "peak50"):
            values = [r[field] for r in items if r[field] is not None]
            output[field] = round(sum(values) / len(values), 4) if values else None
            if field == "clean_intraday_symmetric":
                output["intraday_n"] = len(values)
        return output
    result["events"] = {kind: stats(items) for kind, items in totals.items()}
    result["matched_random_minutes"] = {kind: stats(items) for kind, items in controls.items()}
    result["coverage"]["early_start_name_days"] = len(history) - len(result["coverage"]["late_start_excluded"])
    result["by_day"] = {day: stats(items) for day, items in result["by_day"].items()}
    conditions = {
        "range_edge_0_9": lambda r: r["range_position"] is not None and r["range_position"] >= .9,
        "vwap_with_side": lambda r: isinstance(r["vwap_distance_atr"], (int, float)) and r["vwap_distance_atr"] >= 0,
        "rvol_0_8": lambda r: isinstance(r["local_rvol"], (int, float)) and r["local_rvol"] >= .8,
        "market_with_side": lambda r: isinstance(r["market_5m_side"], (int, float)) and r["market_5m_side"] >= 0,
        "motion15_0_25": lambda r: r["motion15_atr"] >= .25,
        "move_open_0_4": lambda r: r["move_open_atr"] is not None and r["move_open_atr"] >= .4,
    }
    result["splits"] = {}
    all_items = [item for items in totals.values() for item in items]
    for name, condition in conditions.items():
        yes = [item for item in all_items if condition(item)]
        no = [item for item in all_items if not condition(item)]
        result["splits"][name] = {"yes": stats(yes), "no_or_unknown": stats(no)}
    for name, condition in {
        "range_edge_and_vwap": lambda r: conditions["range_edge_0_9"](r) and conditions["vwap_with_side"](r),
        "range_edge_and_market": lambda r: conditions["range_edge_0_9"](r) and conditions["market_with_side"](r),
        "range_edge_and_motion": lambda r: conditions["range_edge_0_9"](r) and conditions["motion15_0_25"](r),
    }.items():
        result["splits"][name] = {"yes": stats([r for r in all_items if condition(r)]),
                                   "no_or_unknown": stats([r for r in all_items if not condition(r)])}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = json.dumps(run(args.db), indent=2)
    if args.out:
        args.out.write_text(result + "\n")
    else:
        print(result)
