"""Verified additive score for LOTTO_ANDRE_WEIGHTS_MODE=1.

This is not a 100-point scale. Nia's hist-validated points sum to at most 38
before the −2 time penalty and the −4 inside-after-11 penalty. Do not
renormalize them. Keys kept at 0 are old terms that must not score.
Override any value with LOTTO_ANDRE_<NAME> (or LOTTO_ANDRE_W_<KEY> for a weight).
MIN_SCORE=0 is the 20–30 cards/day alternative; it is not the default.
"""
import os

WEIGHTS = {
    # Time, minutes after 09:30. See TIME_ENDS.
    "TIME_P1": 8,          # 09:30–10:00
    "TIME_P2": 5,          # 10:00–11:00
    "TIME_P3": 0,          # 11:00–13:00. Not a blocker.
    "TIME_P4": -2,         # after 13:00, including 13:30–15:00
    "TIME_P5": -2,         # after 16:00. Same penalty. No afternoon bonus.
    "PENALTY_INSIDE_AFTER11": 4,  # subtract when after 11:00 and still inside PM/PD

    "SPY_EFF": 8,          # SPY 30-minute efficiency >= 0.5, same side
    "SPY_D3": 4,           # SPY 3-day trend, same side
    "SPY_TREND": 0,        # old open comparison. Replaced by SPY_EFF / SPY_D3.
    "SPY_LEVEL": 0,        # SPY beyond its own PM/PD. Failed history.
    "SPY_15M": 0,          # verified alternative to D3. Not stacked.

    "QQQ_TREND": 0,
    "QQQ_LEVEL": 0,

    "LEVEL_PM": 0,
    "LEVEL_PD": 0,
    "LEVEL_N3": 0,
    "LEVEL_N5": 0,

    "SECTOR_ETF": 0,
    "PEERS": 0,            # optional live sleeve, off
    "LEADER": 0,

    "PACE_2": 4,           # pace >= 2
    "RVOL_3": 2,           # cumulative or local RVOL >= 3
    "BURST_PD": 1,         # burst only when it is a prior-day break
    "RVOL_15": 0,
    "RVOL_2": 0,

    "GAP_1ATR": 3,         # |gap| >= 1 ATR
    "EARN_D1": 3,          # earnings day-1, when a calendar says so
    "PM_RVOL": 0,

    "ENTRY": 0,
    "NAMED": 0,
    "PENALTY_CHASE": 0,    # not applied
    "PENALTY_STRETCH": 0,  # not applied
}

# Minutes after 09:30 where each time bucket ends.
TIME_ENDS = {
    "END_1": 30,     # 10:00
    "END_2": 90,     # 11:00
    "END_3": 210,    # 13:00
    "END_4": 390,    # 16:00
}

# Ace [DEF] thresholds. Floats are ATR fractions or returns unless noted.
THRESHOLDS = {
    "MIN_SCORE": 6,     # T>=6. Not 70. Env LOTTO_ANDRE_MIN_SCORE still overrides.
    "TOP_SCORE": 8,     # informational. Not an operating cut. Not 85.
    "RVOL_MIN_SESSIONS": 14,
    "RVOL_SESSIONS": 20,
    "RVOL_GATE": 1.5,
    "RVOL_TIER2": 2.0,
    "RVOL_TIER3": 3.0,
    "S29A_RVOL": 1.2,
    "S29B_RVOL": 2.0,
    "BURST_MULTIPLE": 3.0,
    "ROOM_ATR": 0.3,
    "ROOM_RVOL": 3.0,
    "EXHAUSTED_MOVE_ATR": 1.0,
    "EXHAUSTED_PIERCE_ATR": 0.25,
    "EXHAUSTED_BARS_2M": 10,       # last 20 minutes on 2-minute bars
    "SPY_CHASE_ATR": 0.5,
    "SPY_CHASE_MINUTES": 15,
    "PULLIN_BARS": 2,
    "REBREAK_RVOL": 1.5,
    "STRETCH_ATR": 0.5,
    "FLUSH_S29_ATR": 0.15,
    "FLUSH_REV_ATR": 0.5,
    "FLUSH_LEVEL_ATR": 0.15,       # how close the flush low must be to PML/PDL
    "S29_RANGE_POS": 0.35,
    "S29_SLOPE_ATR": 0.03,
    "S29_VWAP_FLAT_ATR": 0.05,  # a rise smaller than this still counts as a flat VWAP
    "S29_SLOPE_BARS": 8,
    "S29_TURN_MINUTES": 45,
    "S29_START_MINUTE": 10,        # 09:40
    "S29_END_MINUTE": 90,          # 11:00
    "VWAP_SHARE": 0.70,
    "PD_NEAR_ATR": 0.25,
    "UPPER_RANGE": 0.60,
    "TOUCH_ATR_2": 0.03,
    "TOUCH_ATR_5": 0.05,
    "TOUCH_ATR_10": 0.05,
    "INSIDE_2M": 5,                # 10 one-minute closes, ported onto 2-minute bars
    "OR5_END_MINUTE": 60,          # first hour, before 10:30
    "FADE_START_MINUTE": 30,       # 10:00
    "FADE_END_MINUTE": 90,         # 11:00
    "FADE_IMPULSE_ATR": 0.30,
    "LEADER_RS": 0.01,
    "PEER_BREADTH": 0.60,
    "PEER_MIN": 2,
    "ETF_NEAR_LOW": 0.25,
    "CATALYST_VS_SPY": 0.50,
    "CATALYST_VS_SECTOR": 0.30,
    "EMA_FADE_MINUTES": 5,
    "AFTER_11_MINUTE": 90,
    "HOURLY_2M": 30,
    "EXTRA_REQUESTS": 2,
    "TAPE_MAX_AGE": 180,
    # Flush-and-reclaim (the only open side trigger) and the trend-stack gate.
    "FLUSH_LEAVE_ATR": 0.15,
    "FLUSH_RECLAIM_BARS": 3,
    "FLUSH_START_MINUTE": 2,     # 09:32
    "FLUSH_END_MINUTE": 30,      # 10:00
    "FLUSH_DEPTH": 5,
    "TREND_DEPTH": 6,
    "STACK_MOVE_ATR": 0.80,
    "STACK_RS": 0.005,
    # "Big range" is named in the stack and was not given a cut. 1 ATR is the stand-in.
    "STACK_BIG_RANGE_ATR": 1.0,
    "STACK_RVOL": 1.5,
    "SPY_EFF_MIN": 0.5,
    "SPY_EFF_MINUTES": 30,
    "SPY_NON_DUMP": -0.005,
    "MIDDAY_START_MINUTE": 120,  # 11:30
    "MIDDAY_END_MINUTE": 180,    # 12:30
    "DAYS_DOWN": 3,
}


def get(name, default):
    """Read LOTTO_ANDRE_<NAME>. A blank or non-numeric value keeps the default."""
    raw = os.getenv("LOTTO_ANDRE_" + name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return type(default)(str(raw).strip())
    except (TypeError, ValueError):
        return default


def weight(key):
    return get("W_" + key, WEIGHTS[key])


def threshold(key):
    return get(key, THRESHOLDS[key])


def time_end(key):
    return get("TIME_" + key, TIME_ENDS[key])
