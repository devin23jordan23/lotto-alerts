from dataclasses import dataclass
from math import isfinite

STRATEGY_VERSION = "phase2-stock-led-v1-2026-10-25"


@dataclass(frozen=True)
class Settings:
    # Research defaults, not fitted probabilities or proven thresholds.
    min_score: float = 70
    hybrid: int = 0
    soft_blocks: int = 0
    repeat_penalty: int = 10
    tier_a: float = 91
    tier_b: float = 81
    send_tiers: str = "AB"
    send_requote: int = 0
    min_pace_rvol: float = 2.0
    min_local_rvol: float = 1.2
    min_local_acceleration: float = 1.2
    min_range_atr: float = .2
    min_option_acceleration: float = 1.3
    min_cluster: int = 2
    min_strike_volume_5m: int = 75
    confirmation_minutes: int = 1  # Two distinct completed one-minute observations.
    # Zero disables count limits. Same-leg deduplication and quality gates remain.
    max_alerts_per_day: int = 0
    max_alerts_per_ticker: int = 0
    max_alerts_per_cycle: int = 0
    rearm_minutes: int = 3
    max_quote_age_seconds: int = 90
    min_ask: float = 0.10
    max_ask: float = 2.50
    max_scaled_ask: float = 7.50
    premium_atr_fraction: float = .25
    max_spread_fraction: float = 0.15
    max_spread_dollars: float = 0.15
    min_delta: float = 0.10
    max_delta: float = 0.55
    max_dte: int = 1
    entry_cutoff_minutes: int = 15
    max_otm_atr: float = 1.0
    max_building_distance_atr: float = .06
    max_entry_extension_atr: float = .25
    min_opening_bars: int = 3
    active_quality_gate: int = 1  # Keep excluded ideas in candidate/decision history.

    # Stock-led MOVER lane (off by default). See lotto/mover.py.
    mover_enabled: int = 0
    mover_r15_atr: float = .25
    mover_open_atr: float = .25
    mover_gap_minutes: int = 15
    mover_base_break: int = 1
    mover_base_minutes: int = 15
    mover_base_open_atr: float = .50
    # KEEP gate: hold legacy ACTIVE ideas as research when the stock is not moving.
    keep_gate: int = 0
    keep_r15_atr: float = .15
    keep_open_atr: float = .40

    def __post_init__(self):
        if any(not isfinite(value) for value in vars(self).values() if isinstance(value, (int, float))):
            raise ValueError("Settings must be finite")
        if (self.hybrid not in (0, 1) or self.soft_blocks not in (0, 1)
                or self.send_requote not in (0, 1) or self.repeat_penalty < 0
                or self.send_tiers not in ("A", "AB", "ABC")
                or not 0 < self.tier_b < self.tier_a <= 100):
            raise ValueError("Invalid hybrid or tier setting")
        if self.hybrid and (self.min_score > 60 or not self.soft_blocks):
            raise ValueError("Hybrid requires LOTTO_MIN_SCORE <= 60 and LOTTO_SOFT_BLOCKS=1")
        if not 0 < self.min_score <= 100 or min(self.min_pace_rvol,self.min_local_rvol,self.min_local_acceleration,self.min_range_atr,self.min_option_acceleration) <= 0:
            raise ValueError("Invalid score or RVOL threshold")
        for name in ("confirmation_minutes", "rearm_minutes",
                     "max_quote_age_seconds", "min_cluster", "min_strike_volume_5m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_alerts_per_day", "max_alerts_per_ticker", "max_alerts_per_cycle"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.active_quality_gate not in (0, 1):
            raise ValueError("active_quality_gate must be 0 or 1")
        if not 0 < self.min_ask <= self.max_ask or not 0 < self.min_delta <= self.max_delta <= 1:
            raise ValueError("Invalid contract limits")
        if not 0 <= self.max_dte <= 1:
            raise ValueError("Invalid maximum expiration")
        if (not self.max_ask <= self.max_scaled_ask or self.premium_atr_fraction <= 0
                or not 0 < self.max_building_distance_atr <= self.max_entry_extension_atr
                or self.max_otm_atr <= 0 or not 3 <= self.min_opening_bars <= 11
                or not 0 < self.entry_cutoff_minutes < 390
                or not 0 < self.max_spread_fraction < 1 or self.max_spread_dollars <= 0):
            raise ValueError("Invalid phase-one quality limits")
