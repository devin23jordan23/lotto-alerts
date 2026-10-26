"""Additive call/put weights from the v4c book.

Every numeric weight is read from the JSON. A feature the book does not list
scores 0. Two gates are generic and live on the weight row:

- `requires_min_conf`: the row stays at 0 until that many of
  {level_break, rvol15, spy_with, flow200} are on. Those names count from
  the feature dict, including names the book gives no points.
- `requires_min_other_positive`: the row stays at 0 until that many other
  ungated rows already contribute points. A row that fails its confirmation
  gate is not one of those others. Gated rows do not count each other.

On the bypass path the gate count includes primary positives. The bypass
score does not. A row counts as positive when its value is at least 0.5
and its points are positive. Points above zero with a smaller value do not.
"""
import json
import logging
from functools import lru_cache
from math import isfinite
from pathlib import Path

from .config import WEIGHTS_PATH

LOG = logging.getLogger(__name__)

# Nia STOCK_NCONF. Counted even when the book assigns the name no points.
CONF_FEATURES = ("level_break", "rvol15", "spy_with", "flow200")
# Research counted these as on/off at 0.5. A 0.2 match is not a partial positive.
BINARY_AT_HALF = {
    "e1_ema9_2m_pullin_x_rvol15",
    "e1_ema9_2m_pullin_pre10",
    "e2_ema9_5m_pullin",
}


def clamp01(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


@lru_cache(maxsize=4)
def load_weights(path: str | None = None) -> dict:
    book = json.loads(Path(path or WEIGHTS_PATH).read_text())
    if not isinstance(book, dict):
        raise ValueError("weights file must be an object")
    return book


def _number(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not isfinite(number):
        return float(default)
    return number


def _whole(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


# Old LOTTO_PROFILE values. Numbers come from the named block.
PROFILE_ALIASES = {
    "loose": "aggressive",
    "moderate": "conservative",
}


def active_profile(book: dict | None = None, name: str | None = None) -> dict:
    """The profile LOTTO_PROFILE selected.

    post_threshold is used as written. A value above 60 stays. Negative
    thresholds clamp to 0. loose selects aggressive and moderate selects
    conservative. An unknown name logs a warning and uses moderate (90).
    Missing keys use the moderate preset: 90 / 40 / 0 / 10.
    """
    if book is None:
        book = load_weights()
    profiles = book.get("profiles") if isinstance(book, dict) else None
    if not isinstance(profiles, dict):
        profiles = {}
    requested = str(name or "moderate").strip().lower() or "moderate"
    canonical = PROFILE_ALIASES.get(requested, requested)
    if canonical not in profiles:
        LOG.warning(
            "Unknown LOTTO_PROFILE %r; using moderate. "
            "Expected aggressive, middle, or conservative.",
            requested,
        )
        requested = "moderate"
        canonical = PROFILE_ALIASES["moderate"]
    raw = profiles.get(canonical)
    if not isinstance(raw, dict):
        raw = {}
    bolt = raw.get("bolt_min_features", raw.get("bolt_min_positive_features", 10))
    return {
        "profile": requested,
        "post_threshold": max(0.0, _number(raw.get("post_threshold"), 90)),
        "bypass_threshold": max(0.0, _number(raw.get("bypass_threshold"), 40)),
        "min_positive_features": max(0, _whole(raw.get("min_positive_features"), 0)),
        "strong_feature_min": max(0, _whole(bolt, 10)),
    }


def _side_weight(spec, side: str) -> float:
    if not isinstance(spec, dict):
        return 0.0
    key = "call" if side == "CALL" else "put" if side == "PUT" else None
    if key is None:
        return 0.0
    try:
        weight = float(spec.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0
    return weight if isfinite(weight) else 0.0


def _field_count(spec, name: str) -> int:
    if not isinstance(spec, dict):
        return 0
    raw = spec.get(name) or 0
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def confirmation_count(features: dict) -> int:
    """How many STOCK_NCONF names are fully on. Partial matches do not count."""
    count = 0
    for name in CONF_FEATURES:
        if clamp01((features or {}).get(name) or 0) >= 1:
            count += 1
    return count


def _contributions(table: dict, features: dict, side: str) -> dict:
    out = {}
    for name, spec in (table or {}).items():
        if not isinstance(spec, dict):
            continue
        value = clamp01((features or {}).get(name) or 0)
        if name in BINARY_AT_HALF:
            value = 1.0 if value >= 0.5 else 0.0
        raw = _side_weight(spec, side) * value
        other = _field_count(spec, "requires_min_other_positive")
        conf = _field_count(spec, "requires_min_conf")
        out[name] = (raw, other, conf, value)
    return out


def score_path(features: dict, side: str, book: dict | None = None, path: str = "primary"):
    """Points by feature, the additive score, and the count of positive points.

    `path` is "primary" or "bypass". Unknown features stay at 0.
    """
    book = book if book is not None else load_weights()
    primary = {**(book.get("weights") or {}), **(book.get("penalties") or {})}
    bypass = {**(book.get("bypass_weights") or {}), **(book.get("bypass_penalties") or {})}
    primary_c = _contributions(primary, features, side)
    bypass_c = _contributions(bypass, features, side) if path == "bypass" else {}
    universe = primary_c if path == "primary" else {**primary_c, **bypass_c}
    conf = confirmation_count(features)

    def conf_ok(need: int) -> bool:
        return need <= 0 or conf >= need

    ungated = sum(
        1 for raw, other, need, value in universe.values()
        if not other and raw > 0 and value >= 0.5 and conf_ok(need)
    )

    def apply(contribs):
        points = {}
        counted = {}
        for name, (raw, other, need, value) in contribs.items():
            allowed = conf_ok(need) and (not other or ungated >= other)
            points[name] = raw if allowed else 0.0
            counted[name] = value if allowed else 0.0
        return points, counted

    points, counted = apply(bypass_c if path == "bypass" else primary_c)
    score = sum(points.values())
    positive = sum(
        1 for name, value in points.items() if value > 0 and counted[name] >= 0.5
    )
    return points, score, positive
