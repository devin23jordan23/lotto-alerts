"""Bar-close timing and options-history seeds. Modes default off.

0 leaves the live loop alone. 1 logs what the aligned cycle would have done.
2 evaluates the hot set before the universe sweep. Persistence is unchanged.
Request spacing never drops below the live 0.55s gap.
"""
from datetime import datetime, timedelta

# Live Schwab.get sleeps until 0.55s have passed since the previous request.
LIVE_REQUEST_GAP = 0.55


def next_aligned_start(now: datetime, delay_s: int) -> datetime:
    """The next minute boundary plus delay_s. An exact hit stays on that minute."""
    minute = now.replace(second=0, microsecond=0)
    target = minute + timedelta(seconds=delay_s)
    if now > target:
        target += timedelta(minutes=1)
    return target


def cycle_wait_seconds(now, began_mono, now_mono, cycle_seconds, mode, delay_s) -> float:
    """Mode 2 waits until the aligned minute, capped by the poll budget.

    Any other mode keeps today's wait: the rest of the poll interval, at least 1s.
    """
    remaining = cycle_seconds - (now_mono - began_mono)
    if mode != 2:
        return max(1.0, remaining)
    if remaining <= 0:
        return 0.0
    align = max(0.0, (next_aligned_start(now, delay_s) - now).total_seconds())
    return min(align, remaining)


def request_gap(mode: int, rps: float) -> float:
    """Never faster than today's 0.55s. A lower rps only widens the gap."""
    if mode == 0 or not rps or rps <= 0:
        return LIVE_REQUEST_GAP
    return max(LIVE_REQUEST_GAP, 1.0 / rps)


def hot_names(qualified_since, recent_cards, ignitions, always_deep, cap: int) -> list[str]:
    """In-play names, in the spec's priority, capped.

    qualified_since keys are (day, symbol, ...). recent_cards and ignitions are
    symbol lists already limited to the last 15 minutes and the last two bars.
    """
    ordered = []

    def add(name):
        if name and name not in ordered:
            ordered.append(name)

    for key in qualified_since:
        add(key[1] if isinstance(key, tuple) and len(key) > 1 else key)
        if len(ordered) >= cap:
            return ordered[:cap]
    for name in recent_cards:
        add(name)
        if len(ordered) >= cap:
            return ordered[:cap]
    for name in ignitions:
        add(name)
        if len(ordered) >= cap:
            return ordered[:cap]
    for name in always_deep:
        add(name)
        if len(ordered) >= cap:
            break
    return ordered[:cap]


def fetch_plan(selected, hot, mode: int, cap: int) -> list[str]:
    """Deep-name order around the universe sweep.

    The quote batch stays first. Mode 2 pulls the hot subset of `selected`
    ahead of the sweep and does not add names, so the request count is unchanged.
    """
    names = list(selected)
    if mode != 2 or cap <= 0:
        return ["quotes", "sweep", *names]
    chosen = []
    for name in hot or []:
        if name in names and name not in chosen:
            chosen.append(name)
        if len(chosen) >= cap:
            break
    rest = [name for name in names if name not in chosen]
    return ["quotes", *chosen, "sweep", *rest]


def break_bar_close(bars, trigger, side: str):
    """Close time of the earliest bar in the current run of closes beyond the trigger."""
    if trigger is None or not bars:
        return None
    direction = 1 if side == "CALL" else -1
    earliest = None
    for bar in reversed(bars):
        if direction * (bar.close - trigger) > 0:
            earliest = bar
        else:
            break
    return None if earliest is None else earliest.end


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def latency_report(rows) -> dict:
    """Median and p90 for the stages that have both endpoints. Empty stays tiny."""
    stages = {
        "break_to_card": [],
        "eval_to_card": [],
        "card_to_delivered": [],
    }
    for row in rows:
        card = _parse(row.get("card_created") or row.get("at"))
        start = _parse(row.get("break_bar_close"))
        eval_at = _parse(row.get("eval_start"))
        delivered = _parse(row.get("delivered_at"))
        if start and card:
            stages["break_to_card"].append((card - start).total_seconds())
        if eval_at and card:
            stages["eval_to_card"].append((card - eval_at).total_seconds())
        if card and delivered:
            stages["card_to_delivered"].append((delivered - card).total_seconds())
    summary = {"samples": len(rows)}
    for name, values in stages.items():
        summary[name] = {
            "n": len(values),
            "median": _percentile(values, 0.5),
            "p90": _percentile(values, 0.9),
        }
    return summary


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
