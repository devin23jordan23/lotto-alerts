"""Whole-universe option-chain coverage and early flow discovery.

Sweep observations promote a name for deeper evaluation; they never alert by
themselves. Contract volume is cumulative and does not identify the buyer.
"""
from collections import defaultdict
from datetime import datetime, timedelta


class ChainCoverage:
    def __init__(self, interval_minutes=5):
        self.interval = timedelta(minutes=interval_minutes)
        self.day = None
        self.last = {}
        self.attempted = {}
        self.previous = {}
        self.flow = {}
        self.rows = []

    def reset(self, now):
        day = now.date()
        if self.day != day:
            self.day = day
            self.last.clear()
            self.attempted.clear()
            self.previous.clear()
            self.flow.clear()
        self.rows = []

    def due(self, symbols, now, excluded=(), limit=24):
        excluded = set(excluded)
        eligible = (s for s in symbols if s not in excluded and
                    (s not in self.last or now-self.last[s] >= self.interval))
        return sorted(eligible, key=lambda s: (self.attempted.get(s, datetime.min.replace(tzinfo=now.tzinfo)), s))[:limit]

    def attempt(self, symbol, now):
        self.attempted[symbol] = now

    def observe(self, symbol, now, options, spot=None, promoted=False):
        prior = self.previous.get(symbol)
        groups = defaultdict(list)
        if prior and timedelta(seconds=30) <= now-prior[0] <= timedelta(minutes=9):
            older = prior[1]
            for option in options:
                previous_volume = older.get(option.symbol)
                if previous_volume is None or option.volume < previous_volume:
                    continue
                groups[(option.side, option.expiry)].append((option.strike, option.volume-previous_volume))
        best_side, best_cluster = None, 0
        for (side, _), strikes in groups.items():
            strikes.sort()
            for index in range(len(strikes)-2):
                window = strikes[index:index+3]
                if all(delta >= 100 for _, delta in window):
                    total = sum(delta for _, delta in window)
                    if total > best_cluster:
                        best_side, best_cluster = side, total
        self.last[symbol] = now
        self.previous[symbol] = (now, {o.symbol:o.volume for o in options})
        if best_cluster:
            self.flow[symbol] = (now, best_side, best_cluster)
        self.rows.append({"symbol":symbol, "at":now, "promoted":promoted,
                          "contracts":len(options), "spot":spot,
                          "flow_side":best_side, "flow_cluster":best_cluster})
        return best_cluster

    def priorities(self, now):
        return {s:min(30, 15+volume/300) for s,(at,_,volume) in self.flow.items()
                if now-at <= timedelta(minutes=9)}

    def count_fresh(self, symbols, now):
        return sum(s in self.last and now-self.last[s] <= self.interval for s in symbols)
