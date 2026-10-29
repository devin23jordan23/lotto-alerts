"""Read-only historical audit of setup confirmation on earlier sent alerts.

The local forensic export ends October 16 and predates Model V.  This script
tests whether a proposed condition would have held those posted alerts.  It
cannot estimate a current-model hit rate or count newly captured movers.
"""
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lotto.models import Bar, Snapshot
from lotto.patterns import Setup
from lotto.setup_confirmation import confirm_setup


DATABASE = (Path(__file__).resolve().parents[2] / "outputs" /
            "lotto-full-history-entry-audit-2026-10-18" / "audit.sqlite3")


def replay(database=DATABASE):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    stock = defaultdict(list)
    quotes = defaultdict(list)
    setups = {}
    for row in connection.execute("SELECT day,symbol,at,row FROM stock_paths ORDER BY at"):
        data = json.loads(row["row"])
        if data.get("bar"):
            stock[(row["day"], row["symbol"])].append((datetime.fromisoformat(row["at"]), data))
    for row in connection.execute("SELECT day,contract,at,bid FROM quote_paths ORDER BY at"):
        quotes[(row["day"], row["contract"])].append((datetime.fromisoformat(row["at"]), row["bid"]))
    for row in connection.execute("SELECT day,symbol,at,contract,features FROM candidate_evaluations"):
        data = json.loads(row["features"])
        setups[(row["day"], row["symbol"], row["at"], row["contract"])] = data.get("setup")
    groups = defaultdict(lambda: {"alerts": 0, "observed_5m": 0, "hit_5m": 0,
                                  "observed_10m": 0, "hit_10m": 0,
                                  "observed_15m": 0, "hit_15m": 0})
    reasons = defaultdict(lambda: [0, 0])
    families = defaultdict(lambda: [0, 0, 0, 0])
    omitted = defaultdict(int)
    for row in connection.execute("""SELECT day,symbol,contract,at,row FROM alerts
                                   WHERE delivery='sent' AND phase IN ('ACTIVE','LEGACY')"""):
        at = datetime.fromisoformat(row["at"])
        alert = json.loads(row["row"])
        series = stock.get((row["day"], row["symbol"]), ())
        times = [item[0] for item in series]
        index = bisect.bisect_right(times, at) - 1
        if index < 2 or (at - times[index]).total_seconds() > 180:
            omitted["stock_path_missing"] += 1
            continue
        current = series[index][1]
        setup_data = setups.get((row["day"], row["symbol"], row["at"], row["contract"]))
        if not setup_data:
            omitted["setup_missing"] += 1
            continue
        bars_by_end = {}
        for _, observed in series[:index + 1]:
            bar = observed.get("bar")
            if bar:
                bars_by_end[bar["end"]] = bar
        bars = tuple(Bar(datetime.fromisoformat(bar["end"]), bar["open"], bar["high"],
                         bar["low"], bar["close"], bar["volume"],
                         bar.get("expected_cumulative_volume"))
                     for bar in sorted(bars_by_end.values(), key=lambda item: item["end"]))
        if len(bars) < 3:
            omitted["bars_missing"] += 1
            continue
        snap = Snapshot(row["symbol"], at, current["spot"],
                        datetime.fromisoformat(current["spot_time"]), current["prior_close"],
                        bars, (), prior_atr=current.get("prior_atr"),
                        prior_high=current.get("prior_high"), prior_low=current.get("prior_low"),
                        premarket_high=current.get("premarket_high"),
                        premarket_low=current.get("premarket_low"))
        setup = Setup(**setup_data)
        confirmation = confirm_setup(snap, alert["side"], setup)
        group = groups["confirmed" if confirmation.ready else "held"]
        group["alerts"] += 1
        bid15 = [bid for time, bid in quotes.get((row["day"], row["contract"]), ())
                 if at < time <= at + timedelta(minutes=15)]
        won15 = bool(bid15 and max(bid15) >= 1.4 * alert["entry_ask"])
        reasons[(confirmation.state, confirmation.reason)][0] += 1
        reasons[(confirmation.state, confirmation.reason)][1] += won15
        family = families[setup.name]
        family[0] += 1
        family[1] += won15
        family[2] += confirmation.ready
        family[3] += bool(confirmation.ready and won15)
        for minutes in (5, 10, 15):
            bids = [bid for time, bid in quotes.get((row["day"], row["contract"]), ())
                    if at < time <= at + timedelta(minutes=minutes)]
            if bids:
                group[f"observed_{minutes}m"] += 1
                group[f"hit_{minutes}m"] += max(bids) >= 1.4 * alert["entry_ask"]
    connection.close()
    return dict(groups), dict(omitted), dict(reasons), dict(families)


if __name__ == "__main__":
    groups, omitted, reasons, families = replay()
    print(json.dumps({"period": "2026-09-25 through 2026-10-16; pre-Model-V",
                      "groups": groups, "omitted": omitted,
                      "reasons": {"/".join(k): v for k, v in reasons.items()},
                      "families": families}, indent=2))
