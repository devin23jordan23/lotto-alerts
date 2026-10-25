"""Read-only stop-first audit of Model T's proposed filters on saved alerts.

This is an *alert cohort* diagnostic, not a replay of newly discovered stock
events. It deliberately reports every +100% path lost by each proposed veto.
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def dt(value):
    return datetime.fromisoformat(value)


def finite_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def grade(quotes, at, ask, stop):
    seen = set()
    ordered = []
    for observed_at, quote_time, bid in quotes:
        if not bid or bid <= 0 or quote_time in seen:
            continue
        seen.add(quote_time)
        observed, quoted = dt(observed_at), dt(quote_time)
        if at <= observed <= at + timedelta(minutes=30) and 0 <= (observed - quoted).total_seconds() <= 90:
            ordered.append((observed, bid / ask - 1))
    ordered.sort()
    if not ordered:
        return None
    first_target = next((time for time, gain in ordered if gain >= .4), None)
    first_stop = next((time for time, gain in ordered if gain <= -stop), None)
    return {
        "clean": first_target is not None and (first_stop is None or first_target < first_stop),
        "peak40": any(gain >= .4 for _, gain in ordered),
        "peak100": any(gain >= 1 for _, gain in ordered),
        "stop_first": first_stop is not None and (first_target is None or first_stop <= first_target),
        "peak": max(gain for _, gain in ordered),
        "n_quotes": len(ordered),
    }


def flags(row):
    metrics = row["metrics"]
    setup = row["setup"]
    rv = finite_number(metrics.get("local_rvol_5m"))
    trend = finite_number(metrics.get("move_from_open_atr"))
    pace = finite_number(metrics.get("pace_persistence"))
    vwap = finite_number(metrics.get("distance_from_vwap_atr"))
    flow = metrics.get("options_persistent_20m")
    return {
        "intraday_reversal": setup == "INTRADAY_REVERSAL",
        "one_dte": row["dte"] == 1,
        "against_open": trend is not None and trend < 0 and setup != "REVERSAL",
        "rvol_below_0_8": rv is not None and rv < .8,
        "pace_climax": pace is not None and pace > 1.015,
        "tier_a_proposal": trend is not None and trend >= .4 and vwap is not None and vwap >= .3 and rv is not None and rv >= .8,
        "tier_b_proposal": trend is not None and 0 <= trend < .4 and vwap is not None and vwap >= .3 and rv is not None and rv >= .8,
        "sustained_flow": flow in (1, True),
    }


def aggregate(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "clean28": round(sum(r["grade28"]["clean"] for r in rows) / n, 4),
        "clean35": round(sum(r["grade35"]["clean"] for r in rows) / n, 4),
        "peak40": round(sum(r["grade28"]["peak40"] for r in rows) / n, 4),
        "peak100": round(sum(r["grade28"]["peak100"] for r in rows) / n, 4),
    }


def run(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    all_rows = []
    missing_candidate = missing_path = 0
    alerts = con.execute("""SELECT day,symbol,contract,at,row FROM alerts
                            WHERE phase='ACTIVE' AND delivery='sent'
                              AND day BETWEEN '2026-10-02' AND '2026-10-16'
                            ORDER BY day,at""")
    for alert in alerts:
        saved = json.loads(alert["row"])
        ask = saved.get("entry_ask")
        if not ask or ask <= 0:
            continue
        candidate = con.execute("""SELECT features FROM candidate_evaluations
            WHERE symbol=? AND at=? AND contract=? LIMIT 1""",
            (alert["symbol"], alert["at"], alert["contract"])).fetchone()
        if candidate is None:
            missing_candidate += 1
            continue
        features = json.loads(candidate["features"])
        at = dt(alert["at"])
        path = con.execute("""SELECT at,quote_time,bid FROM quote_paths
            WHERE contract=? AND day=? AND at>=? AND at<=? ORDER BY at""",
            (alert["contract"], alert["day"], alert["at"],
             (at + timedelta(minutes=30)).isoformat())).fetchall()
        g28 = grade(path, at, ask, .28)
        g35 = grade(path, at, ask, .35)
        if g28 is None or g35 is None:
            missing_path += 1
            continue
        expiry = dt(saved["entry_option"]["expiry"]).date()
        day = dt(alert["day"]).date()
        setup = (features.get("setup") or {}).get("name")
        row = {"day": alert["day"], "symbol": alert["symbol"], "contract": alert["contract"],
               "at": alert["at"], "setup": setup,
               "metrics": features.get("metrics") or {}, "dte": (expiry - day).days,
               "grade28": g28, "grade35": g35}
        row["flags"] = flags(row)
        all_rows.append(row)
    summary = {"definition": "sent ACTIVE alerts, ask at alert, fresh sampled bids in next 30m",
               "n": len(all_rows), "missing_candidate": missing_candidate,
               "missing_quote_path": missing_path,
               "all": aggregate(all_rows), "by_day": {}, "by_half": {}, "filters": {}}
    for day in sorted({r["day"] for r in all_rows}):
        summary["by_day"][day] = aggregate([r for r in all_rows if r["day"] == day])
    halves = {"H1": [r for r in all_rows if r["day"] <= "2026-10-08"],
              "H2": [r for r in all_rows if r["day"] >= "2026-10-09"]}
    for half, rows in halves.items():
        summary["by_half"][half] = aggregate(rows)
    for name in ("intraday_reversal", "one_dte", "against_open", "rvol_below_0_8", "pace_climax"):
        selected = [r for r in all_rows if r["flags"][name]]
        kept = [r for r in all_rows if not r["flags"][name]]
        summary["filters"][name] = {
            "removed": aggregate(selected), "kept": aggregate(kept),
            "removed_winners": [{"day": r["day"], "symbol": r["symbol"], "at": r["at"],
                                 "contract": r["contract"], "clean28": r["grade28"]["clean"],
                                 "peak_pct": round(100*r["grade28"]["peak"])}
                                for r in selected if r["grade28"]["peak100"]],
            "by_half": {half: {"removed": aggregate([r for r in rows if r["flags"][name]]),
                               "kept": aggregate([r for r in rows if not r["flags"][name]])}
                        for half, rows in halves.items()},
        }
    for name, components in {
        "all_spec_vetoes": ("intraday_reversal", "one_dte", "against_open", "rvol_below_0_8", "pace_climax"),
        "without_pace": ("intraday_reversal", "one_dte", "against_open", "rvol_below_0_8"),
        "reversal_and_against_open": ("intraday_reversal", "against_open"),
    }.items():
        removed = [r for r in all_rows if any(r["flags"][component] for component in components)]
        kept = [r for r in all_rows if not any(r["flags"][component] for component in components)]
        summary["filters"][name] = {
            "components": components,
            "removed": aggregate(removed), "kept": aggregate(kept),
            "removed_plus100": sum(r["grade28"]["peak100"] for r in removed),
            "removed_clean28": sum(r["grade28"]["clean"] for r in removed),
            "by_half": {half: {
                "removed": aggregate([r for r in rows if any(r["flags"][c] for c in components)]),
                "kept": aggregate([r for r in rows if not any(r["flags"][c] for c in components)]),
            } for half, rows in halves.items()},
        }
    for tier in ("tier_a_proposal", "tier_b_proposal"):
        summary[tier] = {half: aggregate([r for r in rows if r["flags"][tier]]) for half, rows in halves.items()}
        summary[tier + "_with_flow"] = {half: aggregate([r for r in rows if r["flags"][tier] and r["flags"]["sustained_flow"]]) for half, rows in halves.items()}
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = run(args.db)
    output = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(output + "\n")
    else:
        print(output)
