"""Read-only historical check of stock motion at posted option cards."""
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "outputs/lotto-full-history-entry-audit-2026-10-18/audit.sqlite3"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lotto.models import Bar, Snapshot
from lotto.stock_thesis import stock_motion_gate, stock_thesis


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    paths = defaultdict(list)
    for row in db.execute("SELECT day,symbol,at,row FROM stock_paths ORDER BY at"):
        data = json.loads(row["row"])
        if data.get("bar") and data.get("prior_atr"):
            paths[(row["day"], row["symbol"])].append((datetime.fromisoformat(row["at"]), data))
    quotes = defaultdict(list)
    for row in db.execute("SELECT day,contract,at,bid FROM quote_paths ORDER BY at"):
        quotes[(row["day"], row["contract"])].append((datetime.fromisoformat(row["at"]), row["bid"]))
    setups = {}
    for row in db.execute("SELECT day,symbol,contract,at,features FROM candidate_evaluations"):
        setup = (json.loads(row["features"]).get("setup") or {}).get("name")
        setups[(row["day"], row["symbol"], row["contract"], row["at"])] = setup
    groups = defaultdict(lambda: [0, 0, 0])
    theses = defaultdict(lambda: [0, 0, 0])
    motion_groups = defaultdict(lambda: [0, 0, 0])
    combined_groups = defaultdict(lambda: [0, 0, 0])
    cases = []
    for row in db.execute("SELECT day,symbol,contract,at,row FROM alerts WHERE delivery='sent'"):
        alert = json.loads(row["row"])
        direction = 1 if alert.get("side") == "CALL" else -1
        at = datetime.fromisoformat(row["at"])
        series = paths.get((row["day"], row["symbol"]), [])
        times = [x[0] for x in series]
        ix = bisect.bisect_right(times, at) - 1
        if ix < 3 or (at-times[ix]).total_seconds() > 180:
            continue
        current = series[ix][1]
        opening = series[0][1]["bar"]["open"]
        atr = current["prior_atr"]
        from_open = direction * (current["bar"]["close"] - opening) / atr
        recent = direction * (current["bar"]["close"] - series[max(0, ix-3)][1]["bar"]["close"]) / atr
        # A first pass on motion thresholds; this does not model setup or execution.
        key = (from_open >= .20, recent >= .10)
        q = quotes.get((row["day"], row["contract"]), [])
        peak15 = max((bid for t, bid in q if at < t <= at+timedelta(minutes=15)), default=None)
        won40 = peak15 is not None and peak15 >= 1.4 * alert["entry_ask"]
        won100 = peak15 is not None and peak15 >= 2 * alert["entry_ask"]
        cases.append((from_open, recent, won40, won100))
        groups[key][0] += 1
        groups[key][1] += won40
        groups[key][2] += won100
        bar_by_end = {}
        for _, observed in series[:ix + 1]:
            saved = observed.get("bar")
            if saved:
                bar_by_end[saved["end"]] = saved
        bars = tuple(Bar(datetime.fromisoformat(b["end"]), b["open"], b["high"],
                         b["low"], b["close"], b["volume"],
                         b.get("expected_cumulative_volume"))
                     for b in sorted(bar_by_end.values(), key=lambda item: item["end"]))
        if bars:
            snap = Snapshot(row["symbol"], at, current["spot"],
                            datetime.fromisoformat(current["spot_time"]),
                            current["prior_close"], bars, (), prior_atr=atr,
                            prior_high=current.get("prior_high"),
                            prior_low=current.get("prior_low"),
                            premarket_high=current.get("premarket_high"),
                            premarket_low=current.get("premarket_low"))
            evidence, _ = stock_thesis(snap, alert["side"])
            theses[bool(evidence)][0] += 1
            theses[bool(evidence)][1] += won40
            theses[bool(evidence)][2] += won100
            motion_ok, _ = stock_motion_gate(snap, alert["side"])
            motion_groups[motion_ok][0] += 1
            motion_groups[motion_ok][1] += won40
            motion_groups[motion_ok][2] += won100
            setup_name = setups.get((row["day"], row["symbol"], row["contract"], row["at"]))
            combined_ok = motion_ok and setup_name not in {
                None, "REVERSAL", "INTRADAY_REVERSAL", "STOCK_OVERRIDE"}
            combined_groups[combined_ok][0] += 1
            combined_groups[combined_ok][1] += won40
            combined_groups[combined_ok][2] += won100
    for key, values in sorted(groups.items()):
        print(key, values)
    print("thesis gate [alerts, +40% in 15m, +100% in 15m]")
    for key, values in sorted(theses.items()):
        print(key, values)
    print("motion gate [alerts, +40% in 15m, +100% in 15m]")
    for key, values in sorted(motion_groups.items()):
        print(key, values)
    print("motion + setup gate [alerts, +40% in 15m, +100% in 15m]")
    for key, values in sorted(combined_groups.items()):
        print(key, values)
    print("flat-only suppression: held [alerts, +40% in 15m, +100% in 15m]")
    for opening in (.05, .10, .15, .20):
        for latest in (.02, .05, .10):
            held = [row for row in cases if row[0] < opening and row[1] < latest]
            print(opening, latest, [len(held), sum(r[2] for r in held), sum(r[3] for r in held)])


if __name__ == "__main__":
    main()
