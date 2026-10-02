from dataclasses import dataclass
from math import isfinite

STRATEGY_VERSION = "phase1-2026-10-02"


@dataclass(frozen=True)
class Settings:
    # Research defaults, not fitted probabilities or proven thresholds.
    min_score: float = 70
    min_pace_rvol: float = 2.0
    min_local_rvol: float = 1.2
    min_local_acceleration: float = 1.2
    min_range_atr: float = .2
    min_option_acceleration: float = 1.3
    min_cluster: int = 2
    min_strike_volume_5m: int = 75
    confirmation_minutes: int = 1  # Two distinct completed one-minute observations.
    max_alerts_per_day: int = 8
    max_alerts_per_ticker: int = 2
    max_alerts_per_cycle: int = 2
    ticker_cooldown_minutes: int = 30
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
    min_opening_bars: int = 5

    def __post_init__(self):
        if any(not isfinite(value) for value in vars(self).values()):
            raise ValueError("Settings must be finite")
        if not 0 < self.min_score <= 100 or min(self.min_pace_rvol,self.min_local_rvol,self.min_local_acceleration,self.min_range_atr,self.min_option_acceleration) <= 0:
            raise ValueError("Invalid score or RVOL threshold")
        for name in ("confirmation_minutes", "max_alerts_per_day", "max_alerts_per_ticker",
                     "max_alerts_per_cycle", "ticker_cooldown_minutes", "rearm_minutes",
                     "max_quote_age_seconds", "min_cluster", "min_strike_volume_5m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.min_ask <= self.max_ask or not 0 < self.min_delta <= self.max_delta <= 1:
            raise ValueError("Invalid contract limits")
        if not 0 <= self.max_dte <= 1:
            raise ValueError("Invalid maximum expiration")
        if (not self.max_ask <= self.max_scaled_ask or self.premium_atr_fraction <= 0
                or not 0 < self.max_building_distance_atr <= self.max_entry_extension_atr
                or self.max_otm_atr <= 0 or not 5 <= self.min_opening_bars <= 11
                or not 0 < self.entry_cutoff_minutes < 390
                or not 0 < self.max_spread_fraction < 1 or self.max_spread_dollars <= 0):
            raise ValueError("Invalid phase-one quality limits")
