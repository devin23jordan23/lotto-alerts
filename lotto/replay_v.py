"""Replay Model V cards and classify +200% runner misses.

True win is +40% from the ask before -35%, with the stop checked first on each
later quote. +200% coverage is separate: a posted card on that name and side
at or before the runner time counts as covered. This does not place orders.
"""
from collections import Counter
from datetime import datetime

from .config import Settings
from .main import env_settings
from .engine import Engine
from .model_v import classify_runner, runner_doubled, true_win
from .store import Store


def _posted_rows(store: Store) -> list[dict]:
    rows = []
    for row in store.db.execute(
            """SELECT day, symbol, side, contract, at, entry_ask, delivery, phase FROM alerts
               WHERE phase='ACTIVE' ORDER BY at"""):
        rows.append({"day": row["day"], "symbol": row["symbol"], "side": row["side"],
                     "contract": row["contract"], "at": datetime.fromisoformat(row["at"]),
                     "entry_ask": row["entry_ask"], "posted": row["delivery"] in {"pending", "dry_run", "sent"},
                     "delivery": row["delivery"]})
    return rows


def _evaluations(store: Store) -> list[dict]:
    rows = []
    for row in store.db.execute(
            "SELECT day, symbol, at, contract, score, features FROM candidate_evaluations ORDER BY at"):
        import json
        features = json.loads(row["features"])
        metrics = features.get("metrics") or {}
        rows.append({"day": row["day"], "symbol": row["symbol"], "side": features.get("side"),
                     "at": datetime.fromisoformat(row["at"]), "contract": row["contract"],
                     "score_v2": metrics.get("score_v2"), "post_bar": metrics.get("post_bar"),
                     "below_by": metrics.get("below_by"), "path": metrics.get("path"),
                     "v2_reason": metrics.get("v2_reason")})
    return rows


def _later_quotes(store: Store, alert: dict) -> list[tuple[datetime, float]]:
    quotes = []
    for row in store.db.execute(
            "SELECT at, payload FROM snapshots WHERE symbol=? AND at>? ORDER BY at",
            (alert["symbol"], alert["at"].isoformat())):
        import json
        from .store import read_snapshot
        snap = read_snapshot(row["payload"])
        option = next((item for item in snap.options if item.symbol == alert["contract"]), None)
        if option is not None and option.bid > 0:
            quotes.append((snap.at, option.bid))
    return quotes


def replay(frames, settings: Settings | None = None, runners: list[dict] | None = None,
           scanned: set[str] | None = None) -> dict:
    """Score frames with Model V. `runners` are +200% names to classify."""
    settings = settings or env_settings()
    store = Store(":memory:")
    try:
        engine = Engine(store, settings, dry_run=True)
        days = set()
        symbols = set()
        for frame in frames:
            snaps = frame if isinstance(frame, list) else [frame]
            for snap in snaps:
                days.add(snap.day)
                symbols.add(snap.symbol)
            engine.process(snaps)
        alerts = _posted_rows(store)
        evaluations = _evaluations(store)
        by_day = Counter(row["day"] for row in alerts)
        by_path = Counter()
        wins = losses = unresolved = 0
        doubled = 0
        for alert in alerts:
            path = "option"
            for ev in evaluations:
                if ev["contract"] == alert["contract"] and ev["at"] == alert["at"]:
                    path = ev.get("path") or path
                    break
            by_path[path] += 1
            quotes = _later_quotes(store, alert)
            outcome = true_win(quotes, alert["entry_ask"])
            if outcome is True:
                wins += 1
            elif outcome is False:
                losses += 1
            else:
                unresolved += 1
            if runner_doubled(quotes, alert["entry_ask"]):
                doubled += 1
        resolved = wins + losses
        n_days = max(1, len(days))
        coverage = None
        misses = Counter()
        miss_rows = []
        if runners is not None:
            universe = scanned if scanned is not None else symbols
            for runner in runners:
                item = classify_runner(runner, alerts, evaluations, set(universe))
                misses[item["result"]] += 1
                miss_rows.append({"symbol": runner["symbol"], "side": runner["side"], **item})
            covered = misses["covered"]
            coverage = covered / len(runners) if runners else None
        missing_v2 = sum(1 for row in evaluations if row.get("score_v2") is None)
        return {
            "cards": len(alerts),
            "days": n_days,
            "cards_per_day": round(len(alerts) / n_days, 2),
            "by_day": dict(by_day),
            "by_path": dict(by_path),
            "true_wins": wins,
            "true_losses": losses,
            "unresolved": unresolved,
            "true_win_rate": round(wins / resolved, 4) if resolved else None,
            "plus_200_on_posted_cards": doubled,
            "runner_coverage": coverage,
            "runner_misses": dict(misses),
            "runner_rows": miss_rows,
            "evaluations": len(evaluations),
            "missing_v2": missing_v2,
            "settings_bars": {
                "score_mode": settings.score_mode,
                "preset": settings.preset,
                "combined_0dte": settings.post_combined_0dte,
                "combined_1dte": settings.post_combined_1dte,
                "stock_max_share": settings.stock_max_share,
                "option_0dte": settings.post_v2_option_0dte,
                "option_1dte": settings.post_v2_option_1dte,
                "stock_0dte": settings.post_v2_stock_0dte,
                "stock_1dte": settings.post_v2_stock_1dte,
                "pwin_quintile_0dte": settings.pwin_quintile_0dte,
                "pwin_quintile_1dte": settings.pwin_quintile_1dte,
                "pwin_decile": settings.pwin_decile,
            },
        }
    finally:
        store.db.close()
