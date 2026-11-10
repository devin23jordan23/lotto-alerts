"""Outage-resilience flags (desk fix 2026-11-10). Every flag is an integer env var; 0 = current behavior.

LOTTO_GAP_BRIDGE_MODE            1: option-flow windows and 5-minute context/peer returns may span one
                                    feed-wide outage gap (every symbol silent) instead of waiting for a
                                    fresh contiguous 4-10 min of post-gap data. 2: also bridge a single
                                    symbol's own gap (chain-rotation re-promotion). Default 0.
LOTTO_GAP_BRIDGE_MAX_SECONDS     longest gap that may be bridged (default 900).
LOTTO_GAP_CONFIRM_MODE           1: on the first observation after a feed-wide outage gap (2: any gap), two completed 1m bars closing on
                                    the trade side of the setup trigger count as the persistence check.
LOTTO_TOKEN_RESILIENCE_MODE      1: broker token refreshed early, retried with short backoff, and the cached
                                    token reused until its real expiry if the broker blips.
LOTTO_TOKEN_REFRESH_LEAD_SECONDS refresh this long before the token's real expiry (default 60).
LOTTO_FAILED_CYCLE_RETRY_SECONDS 0 keeps the normal poll interval after a failed cycle; >0 retries sooner.
LOTTO_HEALTH_FAST_MODE           1: outage notice once failures have lasted LOTTO_HEALTH_ALERT_SECONDS
                                    (default 45) instead of after three failed cycles (~3 min).
LOTTO_HEALTH_WEBHOOK             optional separate ops webhook (e.g. Hawk); default = the lotto webhook.
"""
import os
from datetime import timedelta


def flag(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def gap_bridge_seconds() -> int:
    """0 when bridging is off; otherwise the largest bridgeable gap in seconds."""
    if flag("LOTTO_GAP_BRIDGE_MODE") not in (1, 2):
        return 0
    return max(150, flag("LOTTO_GAP_BRIDGE_MAX_SECONDS", 900))


# Feed-wide outages seen by this process: (last observation before, first observation after).
_feed = {"last": None, "outages": []}


def note_feed_time(at) -> None:
    """Called for every snapshot in arrival order; records gaps where no symbol was observed."""
    last = _feed["last"]
    if last is not None and (at - last).total_seconds() > 150:
        _feed["outages"] = [o for o in _feed["outages"] if (at - o[1]).total_seconds() < 3600] + [(last, at)]
    if last is None or at > last:
        _feed["last"] = at


def gap_is_bridgeable(before, after, mode_flag="LOTTO_GAP_BRIDGE_MODE") -> bool:
    """Mode 2: any gap. Mode 1: only when a feed-wide outage explains (nearly all of) the symbol's gap."""
    mode = flag(mode_flag)
    if mode == 2:
        return True
    if mode != 1:
        return False
    span = (after - before).total_seconds()
    slack = timedelta(seconds=5)
    return any(o1 >= before - slack and o2 <= after + slack and (o2 - o1).total_seconds() >= span - 180
               for o1, o2 in _feed["outages"])


def reset_feed_state() -> None:
    _feed["last"], _feed["outages"] = None, []
