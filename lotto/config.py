from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Research defaults, not fitted probabilities or proven thresholds.
    min_score: float = 72
    min_pace_rvol: float = 2.0
    min_local_rvol: float = 1.5
    min_local_acceleration: float = 1.2
    min_range_atr: float = .2
    min_option_acceleration: float = 1.5
    min_cluster: int = 3
    min_strike_volume_5m: int = 100
    confirmation_minutes: int = 3
    max_alerts_per_day: int = 5
    max_alerts_per_ticker: int = 2
    max_alerts_per_cycle: int = 1
    ticker_cooldown_minutes: int = 45
    rearm_minutes: int = 3
    max_quote_age_seconds: int = 90
    min_ask: float = 0.15
    max_ask: float = 2.50
    max_spread_fraction: float = 0.15
    max_spread_dollars: float = 0.15
    min_delta: float = 0.12
    max_delta: float = 0.55
    max_dte: int = 7
    entry_cutoff_minutes: int = 30

    def __post_init__(self):
        if not 0 < self.min_score <= 100 or min(self.min_pace_rvol,self.min_local_rvol,self.min_local_acceleration,self.min_range_atr) <= 0:
            raise ValueError("Invalid score or RVOL threshold")
        for name in ("confirmation_minutes", "max_alerts_per_day", "max_alerts_per_ticker",
                     "max_alerts_per_cycle", "ticker_cooldown_minutes", "rearm_minutes",
                     "max_quote_age_seconds", "min_cluster", "min_strike_volume_5m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.min_ask <= self.max_ask or not 0 < self.min_delta <= self.max_delta <= 1:
            raise ValueError("Invalid contract limits")
