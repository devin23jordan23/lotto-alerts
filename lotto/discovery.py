"""Cheap whole-universe observations; bounded, sticky option-chain promotion."""
from collections import defaultdict
from datetime import timedelta
from math import isfinite


class Discovery:
    def __init__(self, capacity=12):
        if not 4 <= capacity <= 24:
            raise ValueError("LOTTO_CHAIN_CAPACITY must be between 4 and 24")
        self.capacity = capacity
        self.history = defaultdict(list)
        self.promoted = {}
        self.explore_leases = {}
        self.explored = set()
        self.day = None
        self.observations = []

    def update(self, quotes, now, universe, tracked=(), flow_priorities=None):
        day = now.date()
        if self.day != day:
            self.history.clear()
            self.promoted.clear()
            self.explore_leases.clear()
            self.explored.clear()
            self.day = day
        ranks = {}
        self.observations = []
        for symbol, q in quotes.items():
            price, opening, previous, volume = (q.get(k) for k in ("price", "open", "previous", "volume"))
            at = q.get("at")
            if (at is None or not 0 <= (now-at).total_seconds() <= 90
                    or any(v is None or not isfinite(v) or v <= 0 for v in (price, opening, previous))
                    or volume is None or not isfinite(volume) or volume < 0):
                continue
            history = self.history[symbol]
            old = [v for v in history if 300 <= (now-v["at"]).total_seconds() <= 390]
            ret5 = price/old[-1]["price"]-1 if old else None
            delta = volume-old[-1]["volume"] if old else None
            old_delta = old[-1].get("volume_5m") if old else None
            acceleration = delta/old_delta if delta is not None and delta >= 0 and old_delta and old_delta > 0 else None
            high, low = q.get("high"), q.get("low")
            excursion = max(abs(price/opening-1), abs(price/previous-1))
            intraday_range = (high-low)/opening if high and low else 0
            score = (100*(abs(ret5 or 0)*3 + excursion + intraday_range*.5)
                     + min(3, acceleration or 0) + (flow_priorities or {}).get(symbol, 0))
            record = {**q, "symbol":symbol, "observed_at":now, "return_5m":ret5,
                      "volume_5m":delta, "volume_acceleration":acceleration, "priority":score}
            if not history or at > history[-1]["at"]:
                history.append(record)
            self.history[symbol] = [v for v in history if now-v["at"] <= timedelta(minutes=12)]
            if symbol in universe:
                ranks[symbol] = score
                self.observations.append(record)
        # Keep windows intact long enough for 20-minute option persistence research.
        # Reserve a quarter of chain slots for names the price ranking has not
        # reached today. They get the same 25-minute observation window.
        explore_capacity = max(1, self.capacity//4)
        core_capacity = self.capacity-explore_capacity
        held = {s:since for s,since in self.promoted.items() if s in ranks and now-since < timedelta(minutes=25)}
        selected = sorted((s for s in held if s not in self.explore_leases), key=lambda s:-ranks[s])[:core_capacity]
        for symbol in sorted(ranks, key=lambda s:(-ranks[s], s)):
            if symbol not in selected and len(selected) < core_capacity:
                selected.append(symbol)
        # A genuinely stronger newcomer may displace at most one low-priority lease.
        newcomers = [s for s in ranks if s not in selected]
        replaceable = [s for s in selected if s not in tracked]
        if newcomers and replaceable:
            best, worst = max(newcomers, key=ranks.get), min(replaceable, key=ranks.get)
            if ranks[best] > max(2, ranks[worst]*2):
                selected[selected.index(worst)] = best
        exploring = [s for s in self.explore_leases if s in held and s not in selected][:explore_capacity]
        for symbol in sorted(ranks):
            if len(exploring) >= explore_capacity:
                break
            if symbol not in selected and symbol not in exploring and symbol not in self.explored:
                exploring.append(symbol)
                self.explored.add(symbol)
        if len(exploring) < explore_capacity:
            self.explored = set(exploring)
            for symbol in sorted(ranks):
                if len(exploring) >= explore_capacity:
                    break
                if symbol not in selected and symbol not in exploring:
                    exploring.append(symbol)
                    self.explored.add(symbol)
        selected += exploring
        self.explore_leases = {s:self.promoted.get(s,now) if s in held else now for s in exploring}
        self.promoted = {s:self.promoted.get(s, now) if s in held else now for s in selected}
        # Previously alerted contracts always retain outcome tracking; bounded by daily alert cap.
        selected = sorted(set(selected) | set(tracked))
        for row in self.observations:
            row["promoted"] = row["symbol"] in selected
        return selected

    def return_5m(self, symbol, now):
        history = self.history.get(symbol, [])
        if not history or history[-1]["return_5m"] is None or not 0 <= (now-history[-1]["at"]).total_seconds() <= 90:
            return None, None
        return history[-1]["return_5m"], history[-1]["at"]
