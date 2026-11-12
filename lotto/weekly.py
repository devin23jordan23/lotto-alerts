"""Friday-weekly chain window. LOTTO_WEEKLY_FALLBACK_DTE=0 is a no-op.

Needs Andre's approval before it is set in Railway. Friday-only names are
2–4 calendar DTE on Mon–Wed, so a 0–1 DTE chain request comes back empty.
"""
from datetime import timedelta

from .models import ET
from .resilience import flag

CAP = 7


def weekly_fallback_dte() -> int:
    """0 keeps today's window. Positive values are capped at 7 calendar days."""
    value = flag("LOTTO_WEEKLY_FALLBACK_DTE")
    if value <= 0:
        return 0
    return min(value, CAP)


def chain_to_date(day, max_dte, fallback_dte=None):
    """toDate for both /chains calls. Flag 0 returns day+max_dte, same as before."""
    if fallback_dte is None:
        fallback_dte = weekly_fallback_dte()
    span = max(int(max_dte), int(fallback_dte) if fallback_dte else 0)
    return day + timedelta(days=span)


def expiry_limit(snap, settings) -> int:
    """DTE ceiling for scoring. Wider only when this snapshot has no 0–1 DTE contract."""
    fallback = weekly_fallback_dte()
    if fallback <= settings.max_dte:
        return settings.max_dte
    day = snap.at.astimezone(ET).date()
    has_near = any(0 <= (option.expiry - day).days <= settings.max_dte for option in snap.options)
    return settings.max_dte if has_near else fallback
