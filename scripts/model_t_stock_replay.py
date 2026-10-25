"""Point-in-time stock-event coverage diagnostic for the local history archive.

Uses archived stock paths only. It cannot estimate option win rates or replace
the full four-session option-snapshot replay required before Model T ships.
"""

import argparse
import json
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


def forward_grade(rows, index, side, atr):
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
    return {"clean25": gain25 is not None and (stop15 is None or gain25 < stop15),
            "clean50": gain50 is not None and (stop25 is None or gain50 < stop25),
            "peak25": max(value for _, value in path) >= .25,
            "peak50": max(value for _, value in path) >= .5,
            "peak_atr": round(max(value for _, value in path), 3)}


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


def event_features(rows, index, levels):
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
        if not open_price:
            continue
        move_open = side * (row["spot"] - open_price) / atr
        prior_extreme = levels["high"][index] if side == 1 else levels["low"][index]
        fresh_extreme = side * (row["spot"] - prior_extreme) > 0
        last_extreme = levels["last_high"][index] if side == 1 else levels["last_low"][index]
        pause = (at - last_extreme).total_seconds() / 60 if last_extreme else 0
        if fresh_extreme and motion15 >= .25 and move_open >= .25:
            events.append(("PUSH", prior_extreme, name, motion15, move_open))
        if fresh_extreme and pause >= 15 and move_open >= .5:
            events.append(("BASE_BREAK", prior_extreme, name, motion15, move_open))
        level_rows = [("PRIOR_DAY", row.get("prior_high") if side == 1 else row.get("prior_low")),
                      ("PREMARKET", row.get("premarket_high") if side == 1 else row.get("premarket_low"))]
        if local.time().isoformat() >= "09:33:00" and len(first3) >= 3:
            level_rows.append(("ORB3", max(b["high"] for b in first3[:3]) if side == 1 else min(b["low"] for b in first3[:3])))
        if local.time().isoformat() >= "09:35:00" and len(first5) >= 5:
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
    result = {"definition": "point-in-time stock path events; forward stock movement, not option returns",
              "events": {}, "by_day": {}, "examples": []}
    totals = defaultdict(list)
    for (day, symbol), rows in history.items():
        seen = set()
        levels = group_levels(rows)
        for index, (at, row) in enumerate(rows):
            for kind, level, side_name, motion15, move_open in event_features(rows, index, levels):
                atr = row.get("prior_atr")
                identity = (side_name, kind, round(level / atr * 20))
                if identity in seen:
                    continue
                seen.add(identity)
                side = 1 if side_name == "CALL" else -1
                outcome = forward_grade(rows, index, side, atr)
                if outcome is None:
                    continue
                item = {"day": day, "symbol": symbol, "at": at.isoformat(),
                        "kind": kind, "side": side_name, "motion15_atr": round(motion15, 3),
                        "move_open_atr": round(move_open, 3), **outcome}
                totals[kind].append(item)
                result["by_day"].setdefault(day, []).append(item)
                if symbol in {"META", "AAPL", "AMZN", "MSFT", "SPY"} and len(result["examples"]) < 150:
                    result["examples"].append(item)
    def stats(items):
        n = len(items)
        return {"n": n,
                "clean25": round(sum(r["clean25"] for r in items) / n, 4) if n else None,
                "clean50": round(sum(r["clean50"] for r in items) / n, 4) if n else None,
                "peak25": round(sum(r["peak25"] for r in items) / n, 4) if n else None,
                "peak50": round(sum(r["peak50"] for r in items) / n, 4) if n else None}
    result["events"] = {kind: stats(items) for kind, items in totals.items()}
    result["by_day"] = {day: stats(items) for day, items in result["by_day"].items()}
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
