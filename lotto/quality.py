"""Versioned, point-in-time policy. No future outcomes participate in routing."""
from dataclasses import dataclass
from datetime import time
from math import isfinite
import os

from .models import ET

S_RULES = frozenset(('WF', 'LATE', 'OPEN', 'EXHQ', 'W1DTE', 'PREM'))
# Model S2: S plus four stop-first-tested demotions (MODEL-U spec section 10).
S2_RULES = frozenset(('REV', 'DTE1', 'AGAINST_OPEN', 'LOWRVOL'))
S2_RESEARCH = frozenset(('WF', 'LATE')) | S2_RULES
STACK8_RULES = frozenset(('WF', 'LATE', 'OPEN', 'EXHQ', 'IREV', 'REVRED', 'LRSPY', 'CONTSTALL'))
DESCRIPTIONS = {
    'WF': 'Weak modeled contract response and recent stock progress',
    'LATE': 'Late entry after a one-ATR directional move',
    'OPEN': 'Opening-window setup',
    'EXHQ': 'Elevated volume without strong price acceleration',
    'W1DTE': 'Next-day contract outside a level reclaim',
    'PREM': 'Higher premium without trend confirmation',
    'REV': 'Reversal setup (directional lane only)',
    'DTE1': 'Next-day contract',
    'AGAINST_OPEN': 'Option side against the move from the open',
    'LOWRVOL': 'Local five-minute RVOL below 0.8',
}


def scalar(metrics, key):
    value = metrics.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


@dataclass(frozen=True)
class QualityConfig:
    mode: str = 'off'
    policy: str = 'S'
    shadow_extra: str = 'stack8'
    disabled_rules: frozenset[str] = frozenset()

    def __post_init__(self):
        if self.mode not in ('off', 'shadow', 'enforce'):
            raise ValueError('LOTTO_QUALITY_MODE must be off, shadow, or enforce')
        if self.policy not in ('S', 'S2'):
            raise ValueError('LOTTO_QUALITY_POLICY must be S or S2; Stack 8 is comparison-only')
        if self.shadow_extra not in ('', 'stack8'):
            raise ValueError('LOTTO_QUALITY_SHADOW_EXTRA must be stack8 or empty')
        if self.disabled_rules - (S_RULES | S2_RULES | STACK8_RULES):
            raise ValueError('LOTTO_QUALITY_DISABLED_RULES contains an unknown rule')

    @classmethod
    def from_env(cls):
        return cls((os.getenv('LOTTO_QUALITY_MODE') or 'off').strip().lower(),
                   (os.getenv('LOTTO_QUALITY_POLICY') or 'S').strip().upper(),
                   os.getenv('LOTTO_QUALITY_SHADOW_EXTRA', 'stack8').strip().lower(),
                   frozenset(s.strip().upper() for s in os.getenv('LOTTO_QUALITY_DISABLED_RULES', '').split(',') if s.strip()))


@dataclass(frozen=True)
class QualityDecision:
    policy_id: str
    tier: str
    reasons: tuple[str, ...]
    labels: tuple[str, ...]
    trend_escape: bool


def evaluate(candidate, policy='S', disabled=frozenset()):
    if policy not in ('S', 'S2', 'stack8'):
        raise ValueError('Unknown quality policy')
    m, snap, opt = candidate.metrics, candidate.snapshot, candidate.option
    setup = candidate.setup.name if candidate.setup else ''
    local = snap.at.astimezone(ET)
    ret, response = scalar(m, 'return_5m_directional'), scalar(m, 'response_proxy')
    extension = scalar(m, 'move_from_open_atr')
    rv, accel = scalar(m, 'local_rvol_5m'), scalar(m, 'price_acceleration')
    escape = all((value := scalar(m, k)) is not None and value >= threshold for k, threshold in (
        ('return_5m_directional', .001), ('move_from_open_atr', .25), ('efficiency', .45),
        ('relative_market_5m', 0), ('local_rvol_5m', .8)))
    rules = []
    if not m.get('opening_expansion') and response is not None and ret is not None and response < .5 and ret < .004:
        rules.append('WF')
    if local.hour >= 14 and extension is not None and extension >= 1:
        rules.append('LATE')
    # Live S permits opening alerts from 09:35; preserve the original window
    # in Stack 8 so its historical comparison does not change silently.
    opening_end = time(9, 35) if policy == 'S' else time(10)
    if time(9, 30) <= local.time() < opening_end:
        rules.append('OPEN')
    if rv is not None and accel is not None and rv > 2.16036 and accel < .00039389:
        rules.append('EXHQ')
    if (opt.expiry - local.date()).days == 1 and candidate.setup is not None and setup != 'LEVEL_RECLAIM':
        rules.append('W1DTE')
    ask = scalar({'ask': opt.ask}, 'ask')
    if ask is not None and ask > 1.5 and not escape:
        rules.append('PREM')
    labels = []
    if setup == 'INTRADAY_REVERSAL':
        rules.append('IREV'); labels.append('Intraday reversal')
    if setup == 'REVERSAL' and extension is not None and extension < 0:
        rules.append('REVRED'); labels.append('Reversal against move from open')
    if setup == 'CONTINUATION' and accel is not None and accel < .0005:
        rules.append('CONTSTALL'); labels.append('Continuation losing acceleration')
    market = scalar({'market': snap.market_return_5m}, 'market')
    fresh = (snap.at - snap.market_time).total_seconds() if snap.market_time is not None else None
    if market is not None and fresh is not None and 0 <= fresh <= 90:
        direction = 1 if opt.side == 'CALL' else -1
        label = snap.market_label or 'Market'
        labels.append(f'{label} {"aligned" if direction*market > 0 else "opposing" if direction*market < 0 else "flat"}')
        if setup == 'LEVEL_RECLAIM' and direction*market < 0:
            rules.append('LRSPY')  # Historical rule name; benchmark is explicitly labeled.
    if policy == 'S2':
        if setup in ('INTRADAY_REVERSAL', 'REVERSAL'):
            rules.append('REV')
        if (opt.expiry - local.date()).days == 1:
            rules.append('DTE1')
        if extension is not None and extension < 0 and setup not in ('INTRADAY_REVERSAL', 'REVERSAL'):
            rules.append('AGAINST_OPEN')
        if rv is not None and rv < .8:
            rules.append('LOWRVOL')
    allowed = (S_RULES | S2_RULES) if policy == 'S2' else S_RULES if policy == 'S' else STACK8_RULES
    reasons = tuple(r for r in rules if r in allowed and r not in disabled)
    research = S2_RESEARCH if policy == 'S2' else frozenset(('WF', 'LATE')) if policy == 'S' else frozenset()
    tier = ('RESEARCH' if any(r in research for r in reasons)
            else 'SECONDARY' if reasons else 'MAIN')
    version = ('S2-2026-10-26' if policy == 'S2' else 'S-2026-10-19-open0935' if policy == 'S'
               else 'stack8-2026-10-19')
    return QualityDecision(version, tier, reasons, tuple(labels), escape)
