"""Stock-led MOVER lane and the stock-movement KEEP gate.

The legacy lane is contract-first: no busy, tight 0DTE strike cluster means the
stock is never even scored (PANW/NET/CRWD on 2026-10-23). This lane is the
reverse: it watches the stock, alerts when the stock itself is moving with the
market, and picks the contract last. The old contract-first liquidity
gates (75-contract clusters, 15% AND $0.15 spread, gamma, etc.) do not apply; the lane only requires
one tradable contract (fresh two-sided quote, ask >= $0.15, spread <= 35%), the same population the
Oct 24 study graded. No tradable contract -> logged, not posted.

Triggers (either one), tested on 2026-10-16/19/20/23 live data (read-only study,
LOTTO-MOVERS-REVERSE-ENGINEER-2026-10-24); SPY's last 15 minutes must not be against the side:
  PUSH:       stock moved >= 0.25 ATR in the last 15 minutes AND is >= 0.25 ATR from the open.
  BASE BREAK: the last bar makes a new session high (calls) / low (puts) after >= 15 minutes
              without one, AND the stock is >= 0.50 ATR from the open. This is the grind-day
              re-entry: a stock trending all day in steps (META 10/16, CRWD 10/23) rarely moves
              0.25 ATR in 15 minutes but keeps basing and breaking to new extremes.
Role: a BYPASS for strong stock moves the option-led lane (Hybrid A/B or Model S) misses.
If the option-led lane already alerted the same side during the move, this lane stays silent.
Thresholds are env-tunable (LOTTO_MOVER_R15_ATR, LOTTO_MOVER_OPEN_ATR, LOTTO_MOVER_BASE_BREAK,
LOTTO_MOVER_BASE_OPEN_ATR) and are set from the replay, targeting only PANW/CRWD/NET-class moves.
Re-alert rule: the trigger condition is checked every minute. One alert when it turns on;
while it stays on, or comes back within 15 minutes of the last minute it was on, it is the
same move and does not re-alert. It is not a time cooldown and not a 'stock stopped' test.
All inputs are completed bars at or before snap.at.
"""
from datetime import datetime, time, timedelta
from math import isfinite

from .features import Candidate, quote_ok
from .models import ET
from .patterns import Setup


def _ref_close(bars, minutes):
    """Close of the last completed bar at least `minutes` before the latest bar (time based, gap safe)."""
    cutoff = bars[-1].end - timedelta(minutes=minutes)
    for bar in reversed(bars):
        if bar.end <= cutoff:
            return bar.close
    return None


def stock_moves(bars, atr, direction):
    """(15-minute move, move from the open) in ATR units, signed with the side."""
    last = bars[-1].close
    ref15 = _ref_close(bars, 15)
    if ref15 is None:
        ref15 = bars[0].open
    return direction * (last - ref15) / atr, direction * (last - bars[0].open) / atr


def base_break(bars, direction, base_minutes):
    """True when the last bar sets a new session extreme with the side and none of the bars
    in the previous `base_minutes` did (a break out of a base, not every tick of a run)."""
    if len(bars) < 2:
        return False
    def extreme(b):
        return b.high if direction == 1 else -b.low
    running, new_flags = extreme(bars[0]), [False]
    for b in bars[1:]:
        new_flags.append(extreme(b) > running)
        running = max(running, extreme(b))
    if not new_flags[-1]:
        return False
    cutoff = bars[-1].end - timedelta(minutes=base_minutes)
    return not any(flag for b, flag in zip(bars[:-1], new_flags[:-1]) if b.end >= cutoff)


def spy_move15(engine, snap):
    """SPY percent change over its last 15 completed minutes, or None when unavailable."""
    bars = engine.spy_bars.get(snap.day)
    if not bars:
        return None
    if not 0 <= (snap.at - bars[-1].end).total_seconds() <= 180:
        return None
    ref = _ref_close(bars, 15)
    if ref is None or ref <= 0:
        return None
    return (bars[-1].close / ref - 1) * 100


def _valid_stock(snap):
    atr = snap.prior_atr
    if not snap.bars or not atr or not isfinite(atr) or atr <= 0 or not snap.spot or snap.spot <= 0:
        return False
    local = snap.at.astimezone(ET)
    if local.weekday() >= 5 or not time(9, 31) <= local.time() < time(16):
        return False
    last = snap.bars[-1]
    if any(b.end > snap.at or b.end.astimezone(ET).date() != local.date()
           or not b.valid() for b in snap.bars):
        return False
    if any(b.end <= a.end for a, b in zip(snap.bars, snap.bars[1:])):
        return False
    # Tolerate a late bar or quote (Schwab throttling) instead of dropping the name for the minute.
    if not 0 <= (snap.at - last.end).total_seconds() <= 180 or not 0 <= (snap.at - snap.spot_time).total_seconds() <= 180:
        return False
    return last.end.astimezone(ET).date() == local.date()


def pick_contract(snap, direction, settings):
    """Best contract for the side, or None. Same tradable population the Oct 24 mover study graded:
    ask >= $0.15, two-sided, fresh quote, spread <= 35% of ask (or <= $0.05). Among those, the
    nearest expiry and the contract closest to 0.30 delta (moneyness proxy when delta is missing).
    No tradable contract -> no card (the move is logged as a research decision, never posted unpriced)."""
    today = snap.at.astimezone(ET).date()
    side = "CALL" if direction == 1 else "PUT"
    pool = [o for o in snap.options
            if o.side == side and o.ask and o.ask >= .15 and o.bid is not None and o.bid > 0 and o.bid < o.ask
            and 0 <= (o.expiry - today).days <= settings.max_dte]
    if not pool:
        return None
    pool = [o for o in pool if -.02 <= direction * (o.strike / snap.spot - 1) <= .08]
    if not pool:
        return None
    pool = [o for o in pool if quote_ok(o, snap, settings)
            and ((o.ask - o.bid) / o.ask <= .35 or o.ask - o.bid <= .05)]
    if not pool:
        return None
    # An untradable same-day chain must not hide a tradable next-day expiry.
    nearest = min(o.expiry for o in pool)
    pool = [o for o in pool if o.expiry == nearest]

    def rank(o):
        good_delta = o.delta is not None and isfinite(o.delta) and 0 < abs(o.delta) <= 1
        fit = (abs(abs(o.delta) - .30) if good_delta
               else abs(direction * (o.strike / snap.spot - 1) - .005) * 25)
        return (round(fit, 3), (o.ask - o.bid) / o.ask)
    return min(pool, key=rank)


def mover_candidates(engine, snap, settings):
    """Zero, one or two (call/put) stock-led candidates for this snapshot."""
    if not _valid_stock(snap):
        return []
    local = snap.at.astimezone(ET)
    close = snap.session_end or local.replace(hour=16, minute=0, second=0, microsecond=0)
    if close - snap.at <= timedelta(minutes=settings.entry_cutoff_minutes):
        return []
    atr, last = snap.prior_atr, snap.bars[-1]
    spy15 = spy_move15(engine, snap)
    out = []
    for direction in (1, -1):
        r15, mfo = stock_moves(snap.bars, atr, direction)
        key = (snap.day, snap.symbol, direction)
        if spy15 is not None and direction * spy15 < 0:
            continue
        push = r15 >= settings.mover_r15_atr and mfo >= settings.mover_open_atr
        based = (settings.mover_base_break and mfo >= settings.mover_base_open_atr
                 and base_break(snap.bars, direction, settings.mover_base_minutes))
        if not (push or based):
            continue
        previous = engine.mover_last.get(key)
        engine.mover_last[key] = last.end
        sent = getattr(engine, "mover_sent", None)
        if sent is None:
            sent = engine.mover_sent = {}
        starts = getattr(engine, "mover_start", None)
        if starts is None:
            starts = engine.mover_start = {}
        if previous is None or last.end - previous >= timedelta(minutes=settings.mover_gap_minutes):
            sent[key] = False          # a new move starts
            starts[key] = last.end
        if sent.get(key):
            alert_id = getattr(engine, "mover_alert_id", {}).get(key)
            if alert_id is not None:
                row = engine.store.db.execute("SELECT delivery FROM alerts WHERE id=?", (alert_id,)).fetchone()
                if row is not None and row["delivery"] in ("internal", "failed", "expired"):
                    sent[key] = False  # An undelivered idea cannot consume the move.
            if sent.get(key):
                continue               # same move already alerted: one alert per move
        # Bypass lane only: if the option-led lane already alerted this side during this move,
        # the move is covered and the MOVER lane stays silent.
        side_name = "CALL" if direction == 1 else "PUT"
        prior = engine.store.db.execute("""SELECT id, at FROM alerts
            WHERE day=? AND symbol=? AND side=? AND phase='ACTIVE'
              AND quality_tier!='C' AND delivery NOT IN ('internal','failed','expired')
            ORDER BY at DESC LIMIT 1""", (snap.day, snap.symbol, side_name)).fetchone()
        if prior is not None and datetime.fromisoformat(prior["at"]) >= starts[key] - timedelta(minutes=settings.mover_gap_minutes):
            sent[key] = True
            engine.mover_alert_id[key] = prior["id"]
            engine.store.decision(snap, "MOVER", "mover: move already alerted by the option-led lane")
            continue
        option = pick_contract(snap, direction, settings)
        if option is None:
            engine.store.decision(snap, "MOVER", "mover: no tradable contract (fresh, two-sided, ask>=0.15, spread<=35%) - not posted")
            continue
        recent = snap.bars[-15:]
        invalidation = min(b.low for b in recent) if direction == 1 else max(b.high for b in recent)
        score = round(60 + 25 * min(1.0, r15 / .75) + 15 * min(1.0, max(0.0, mfo) / 1.5), 2)
        spread = (option.ask - option.bid) / option.ask
        metrics = {"lane": "MOVER", "mover_trigger": "PUSH" if push else "BASE_BREAK", "mover_r15_atr": r15, "move_from_open_atr": mfo, "mover_spy15_pct": spy15,
                   "selected_spread_fraction": spread, "mover_quote_fresh": int(quote_ok(option, snap, settings))}
        market = f"{spy15:+.2f}%" if spy15 is not None else "unknown"
        reasons = [f"Stock-led mover ({'15m push' if push else 'base break to new session extreme'}): {r15:+.2f} ATR in 15m, {mfo:+.2f} ATR from open, SPY 15m {market}"]
        out.append(Candidate(snap, option, score, {"mover": score}, metrics, reasons, True,
                             state="IGNITION", setup=Setup("MOVER", snap.spot, invalidation)))
    return out


def keep_reason(engine, candidate, settings):
    """None to send; otherwise why a legacy ACTIVE idea is held as research (stock not moving)."""
    snap = candidate.snapshot
    atr = snap.prior_atr
    if not snap.bars or not atr or not isfinite(atr) or atr <= 0:
        return None
    direction = 1 if candidate.option.side == "CALL" else -1
    r15, mfo = stock_moves(snap.bars, atr, direction)
    spy15 = spy_move15(engine, snap)
    candidate.metrics.update({"keep_r15_atr": r15, "keep_spy15_pct": spy15})
    moving = r15 >= settings.keep_r15_atr or mfo >= settings.keep_open_atr
    spy_ok = spy15 is not None and direction * spy15 >= 0
    if moving and spy_ok:
        return None
    why = [] if moving else [f"stock not moving ({r15:+.2f} ATR/15m, {mfo:+.2f} ATR from open)"]
    if not spy_ok:
        why.append("SPY 15m unavailable" if spy15 is None else f"SPY 15m against ({spy15:+.2f}%)")
    return "research only: " + "; ".join(why)


def decorate(payload, candidate):
    embed = payload["embeds"][0]
    m = candidate.metrics
    spread = m.get("selected_spread_fraction") or 0
    embed["title"] = "🚀 STOCK MOVER · ACTIVE TRADE IDEA"
    flags = [f"spread {spread:.0%}"]
    trig = "15m push" if m.get("mover_trigger") == "PUSH" else "base break to new session " + ("high" if candidate.option.side == "CALL" else "low")
    spy = m['mover_spy15_pct']
    market = f"{spy:+.2f}%" if spy is not None else "unknown"
    embed["description"] += (f"\nStock-led ({trig}): {m['mover_r15_atr']:+.2f} ATR in 15m · {m['move_from_open_atr']:+.2f} ATR from open"
                             f" · SPY 15m {market} · " + " · ".join(flags))
