"""Observable contract potential and accelerating price for public ACTIVE ideas."""

from .config import Settings
from .features import Candidate


def public_active_quality(candidate: Candidate, settings: Settings) -> bool:
    if not settings.active_quality_gate:
        return True
    metrics = candidate.metrics
    return (candidate.score >= settings.active_min_score
            and (metrics.get("response_proxy", 0) >= settings.active_min_response_proxy
                 or metrics.get("price_acceleration", 0) >= settings.active_min_price_acceleration))
