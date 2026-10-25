"""Fast stock-led ideas from the whole-universe quote sweep.

This path deliberately does not depend on option-chain promotion, Greeks,
historical volume profiles, or a completed stock-bar download. It describes a
stock move, never an option fill or a predicted option return.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from hashlib import sha256
from math import isfinite

from .models import ET

INDEX_ETFS = {"SPY", "QQQ", "IWM"}


@dataclass(frozen=True)
class StockIdea:
    symbol: str
    day: str
    at: datetime
    side: str
    price: float
    trigger: float
    invalidation: float
    setup: str
    tier: str
    metrics: dict

    @property
    def id(self) -> str:
        raw = f"stock:{self.day}:{self.symbol}:{self.side}:{self.at.isoformat()}"
        return sha256(raw.encode()).hexdigest()[:16]


def stock_payload(idea: StockIdea) -> dict:
    direction = "CALLS" if idea.side == "CALL" else "PUTS"
    age = round(idea.metrics["quote_age_seconds"])
    return {"username": "Lotto Scanner", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": f"{idea.tier} · ⚡ STOCK-LED {idea.setup.replace('_', ' ')} · {idea.symbol} {direction} WATCH",
        "description": (f"**{idea.symbol} {direction} · stock ${idea.price:.2f}**\n"
                        f"Break ${idea.trigger:.2f} · Stock invalidation ${idea.invalidation:.2f}\n"
                        f"From open {idea.metrics['move_from_open']*100:+.2f}% · "
                        f"Recent {idea.metrics['recent_return']*100:+.2f}% · "
                        f"Quote {age}s old\n"
                        "Stock setup only. No option contract or entry price validated; "
                        "check the live chain and spread before trading."),
        "color": 0x2ECC71 if idea.side == "CALL" else 0xE74C3C,
        "timestamp": idea.at.isoformat(),
        "footer": {"text": f"Stock-led idea • {idea.id}"},
    }]}


class StockFirst:
    def __init__(self, store, enabled: bool = False, mode: str = "shadow", max_quote_age: int = 90):
        if mode not in {"shadow", "send"}:
            raise ValueError("LOTTO_STOCK_FIRST_MODE must be shadow or send")
        self.store = store
        self.enabled = enabled
        self.mode = mode
        self.max_quote_age = max_quote_age
        self.history = defaultdict(list)
        self.last_extreme = {}

    def process(self, observations: list[dict], dry_run: bool = True) -> list[StockIdea]:
        if not self.enabled:
            return []
        ideas = []
        for row in observations:
            idea = self._evaluate(row)
            if idea is None:
                continue
            last = self.store.last_stock_idea(idea.day, idea.symbol, idea.side)
            if last is not None and not self._new_leg(idea, last):
                self.store.stock_decision(idea, "same stock price leg")
                continue
            self.store.queue_stock_idea(idea, stock_payload(idea), dry_run, self.mode)
            ideas.append(idea)
        return ideas

    def _evaluate(self, row: dict) -> StockIdea | None:
        at, quote_time = row.get("observed_at"), row.get("at")
        if not isinstance(at, datetime) or not isinstance(quote_time, datetime):
            return None
        if at.tzinfo is None or quote_time.tzinfo is None:
            return None
        local = at.astimezone(ET)
        if (local.weekday() >= 5 or not time(9, 30) <= local.time() < time(15, 45)
                or not 0 <= (at - quote_time).total_seconds() <= self.max_quote_age):
            return None
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            return None
        values = [row.get(k) for k in ("price", "open", "previous", "volume")]
        if any(not isinstance(v, (int, float)) or not isfinite(v) for v in values):
            return None
        price, opening, previous, volume = values
        high = row.get("high")
        low = row.get("low")
        if not isinstance(high, (int,float)) or not isfinite(high) or high <= 0:
            high = max(price, opening)
        if not isinstance(low, (int,float)) or not isfinite(low) or low <= 0:
            low = min(price, opening)
        high, low = max(high, price, opening), min(low, price, opening)
        row = {**row, "high":high, "low":low}
        if min(price, opening, previous, high, low) <= 0 or volume < 0:
            return None
        key = (local.date().isoformat(), symbol)
        history = self.history[key]
        # The same provider quote must not create a second observation merely
        # because the polling loop ran again.
        if history and quote_time <= history[-1]["at"]:
            return None
        age_minutes = (local.hour*60 + local.minute) - 570
        lookback = 5 if age_minutes < 15 else 15
        earlier = [h for h in history if (lookback-2)*60 <= (at-h["observed_at"]).total_seconds() <= (lookback+3)*60]
        prior = min(earlier, key=lambda h: abs((at-h["observed_at"]).total_seconds()-lookback*60)) if earlier else None
        recent = [h for h in history if at-h["observed_at"] <= timedelta(minutes=10)]
        history.append(row)
        self.history[key] = [h for h in history if at-h["observed_at"] <= timedelta(minutes=35)]
        self.store.track_stock_ideas(row)
        if prior is None or len(recent) < 3:
            return None
        opening_window = local.time() < time(9, 45)
        etf = symbol in INDEX_ETFS
        min_open = .0025 if etf else (.0045 if opening_window else .007)
        min_recent = .0012 if etf else .0025
        old_high = max(h["high"] for h in recent)
        old_low = min(h["low"] for h in recent)
        local_high = max(h["price"] for h in recent[-6:])
        local_low = min(h["price"] for h in recent[-6:])
        compression = (max(h["price"] for h in recent[-6:])
                       - min(h["price"] for h in recent[-6:]))/price <= .0045
        path = recent[-5:] + [row]
        travel = sum(abs(b["price"]-a["price"]) for a, b in zip(path, path[1:]))
        efficiency = abs(price-path[0]["price"])/travel if travel else 0
        atr = row.get("prior_atr")
        atr = atr if isinstance(atr, (int, float)) and isfinite(atr) and atr > 0 else None
        # Only a fresh, directional extension qualifies. An inflated historical
        # score on a flat or retracing stock cannot fire this path.
        for direction, side, day_trigger, local_trigger, invalidation, near_extreme in (
            (1, "CALL", old_high, local_high, min(h["low"] for h in recent[-5:]), price >= high*.998),
            (-1, "PUT", old_low, local_low, max(h["high"] for h in recent[-5:]), price <= low*1.002),
        ):
            move_open = direction*(price/opening-1)
            fast = direction*(price/prior["price"]-1)
            move_open_dollars = direction*(price-opening)
            recent_dollars = direction*(price-prior["price"])
            day_break = direction*(price-day_trigger)/price >= .0001 and near_extreme
            local_break = (direction*(price-local_trigger)/price >= .0001
                           and move_open >= max(min_open, .0055 if not etf else .003)
                           and fast >= (.0015 if not etf else .001)
                           and compression
                           and (price >= high*.995 if direction == 1 else price <= low*1.005))
            extreme_key = (key[0], symbol, side)
            last_extreme = self.last_extreme.get(extreme_key)
            if day_break:
                self.last_extreme[extreme_key] = at
            paused_extreme = (bool(atr and day_break and not opening_window and last_extreme
                                   and at-last_extreme >= timedelta(minutes=15)
                                   and move_open_dollars >= .5*atr))
            atr_fast = bool(atr and day_break and move_open_dollars >= .25*atr
                            and recent_dollars >= .25*atr)
            fallback_fast = bool(not atr and move_open >= min_open
                                 and fast >= min_recent and day_break)
            # The October 19 replay showed percent-only local breaks on flat
            # names with <0.1 ATR of recent movement. Keep local structure,
            # but require an actual ATR-sized directional push alongside it.
            local_motion = (move_open_dollars >= (.2 if etf else .4)*atr
                            and recent_dollars >= (.15 if etf else .15)*atr) if atr else (
                            move_open >= min_open and fast >= (.001 if etf else .0015))
            qualified = atr_fast or paused_extreme or fallback_fast or (local_break and local_motion)
            if (not qualified or efficiency < (.35 if paused_extreme else .55 if day_break else .35)
                    or direction*(price-invalidation) <= 0):
                continue
            trigger = day_trigger if day_break else local_trigger
            setup = ("OPENING_DRIVE" if opening_window and day_break else
                     "BASE_BREAK" if paused_extreme else "LOCAL_RANGE_BREAK" if local_break else "INTRADAY_BREAK")
            tier = "A" if atr and move_open_dollars >= .5*atr and recent_dollars >= .25*atr and efficiency >= .7 else "B"
            return StockIdea(symbol, key[0], at, side, price, trigger, invalidation, setup, tier,
                             {"move_from_open": move_open, "recent_return": fast,
                              "move_from_open_atr": move_open_dollars/atr if atr else None,
                              "recent_move_atr": recent_dollars/atr if atr else None,
                              "prior_atr": atr,
                              "break_margin": direction*(price-trigger)/price,
                              "efficiency": efficiency, "compressed_range": int(compression), "tier": tier,
                              "quote_age_seconds": (at-quote_time).total_seconds(),
                              "prior_close": previous, "day_high": high, "day_low": low})
        return None

    def _new_leg(self, idea: StockIdea, last) -> bool:
        direction = 1 if idea.side == "CALL" else -1
        old_price = last["stock_price"]
        atr = idea.metrics.get("prior_atr")
        threshold = .15*atr if atr and atr > 0 else .003*idea.price
        if direction*(idea.price-old_price) < threshold:
            return False
        # Distinct structure can re-alert immediately. A same-setup continuation
        # must show a retest first; no fixed thirty-minute cooldown.
        return idea.setup != last["setup"] or last["min_stock_return"] <= -.0015
