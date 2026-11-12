"""Untested defaults for LOTTO_ANDRE_WEIGHTS_MODE=1.

Every value is Ace's [DEF] or the weight table Andre locked on Nov 12.
Nothing here has been fit on a replay. Nia can replace a value without a
code change by setting LOTTO_ANDRE_<NAME> (same type as the default).
Weights live in WEIGHTS. Thresholds live in THRESHOLDS. Time windows live
in TIME_ENDS (minutes after 09:30) plus the TIME_P* weights.
"""
import os

# Positive buckets sum to 100 before the named-setup bonus. The bonus is
# +5 and the total is capped at 100. Penalties are subtracted after that.
WEIGHTS = {
    "LEVEL_PM": 15,       # beyond PMH (calls) / PML (puts)
    "LEVEL_PD": 10,       # beyond PDH / PDL
    "LEVEL_N3": 5,        # holding beyond a 3-day or longer range
    "LEVEL_N5": 5,        # 5-day range, or an hourly high/low, or the daily level
    "SPY_TREND": 8,       # SPY trending the same side
    "SPY_LEVEL": 4,       # SPY beyond its own premarket or prior-day level
    "QQQ_TREND": 2,       # QQQ trending the same side (with QQQ_LEVEL sums to 3)
    "QQQ_LEVEL": 1,       # QQQ beyond its own premarket or prior-day level
    "SECTOR_ETF": 7,      # sector ETF same side and past VWAP or a level
    "PEERS": 5,           # at least 60% of peers moving the same way
    "LEADER": 3,          # stock leading its group
    "RVOL_15": 5,         # time-of-day RVOL in [1.5, 2)
    "RVOL_2": 10,         # RVOL in [2, 3)
    "RVOL_3": 15,         # RVOL >= 3
    "ENTRY": 10,          # level break on volume, or EMA touch-then-reclaim
    "NAMED": 5,           # one of Ace's named setups is the trigger
    "TIME_P1": 10,        # 09:30–10:30
    "TIME_P2": 6,         # 10:30–11:00
    "TIME_P3": 0,         # 11:00–13:30 lunch. Retune; not a tested edge.
    "TIME_P4": 6,         # 13:30–15:00 second window
    "TIME_P5": 0,         # after 15:00
    "PENALTY_CHASE": 15,  # exhausted approach, no pull-in yet
    "PENALTY_STRETCH": 10,  # more than 0.5 ATR from the active 9 EMA
}

# Minutes after 09:30 where each time bucket ends. 13:30 is 240, 15:00 is 330.
TIME_ENDS = {
    "END_1": 60,    # 10:30
    "END_2": 90,    # 11:00
    "END_3": 240,   # 13:30
    "END_4": 330,   # 15:00
}

# Ace [DEF] thresholds. Floats are ATR fractions or returns unless noted.
THRESHOLDS = {
    "MIN_SCORE": 70,
    "TOP_SCORE": 85,
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
