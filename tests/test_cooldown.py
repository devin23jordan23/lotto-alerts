import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from lotto.config import model_v_settings
from lotto.engine import Engine, cooldown_reason, cooldown_underlying
from lotto.features import Candidate
from lotto.main import env_settings
from lotto.models import Bar, ET, Option, Snapshot
from lotto.store import Store


def _option(symbol, at, side, strike):
    mark = "C" if side == "CALL" else "P"
    return Option(
        f"{symbol}{mark}{int(strike)}", at.astimezone(ET).date(), side, float(strike),
        1.0, 1.1, 200, 10, 0.4, 0.02, at,
    )


def _snap(symbol, minute, legs):
    """One session snapshot. `minute` is minutes after 9:30. `legs` are (side, strike, score)."""
    start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
    bars = tuple(
        Bar(start + timedelta(minutes=index), 100, 101, 99, 100, 1000, None)
        for index in range(1, minute + 1)
    )
    at = bars[-1].end
    options = tuple(_option(symbol, at, side, strike) for side, strike, _score in legs)
    snap = Snapshot(symbol, at, 100, at, 100, bars, options, prior_atr=2, source="replay")
    scores = {option.symbol: score for option, (*_leg, score) in zip(options, legs)}
    return snap, scores


def _engine(store, **overrides):
    settings = model_v_settings(fallback_rule="off", cooldown_minutes=overrides.pop("cooldown_minutes", 8), **overrides)
    return Engine(store, settings, dry_run=True)


def _cycles(engine, frames):
    """Each frame is one scan cycle. Every frame's contracts are qualifying."""
    book = {}
    snaps = []
    for snap, scores in frames:
        snaps.append(snap)
        book.update(scores)

    def candidates(snap, history, settings):
        rows = []
        for option in snap.options:
            score = book[option.symbol]
            rows.append(Candidate(
                snap, option, score, {},
                {"cluster_size": 2, "card_score": score, "score_v2": score, "path": "hybrid"},
                ["option side"], True,
            ))
        return rows

    def fake_apply(candidate, *args, **kwargs):
        score = candidate.metrics.get("card_score", candidate.score)
        candidate.metrics["score_v2"] = score
        candidate.metrics["card_score"] = score
        candidate.metrics["path"] = "hybrid"
        candidate.score = score
        candidate.qualifying = True
        return candidate

    posted = []
    with patch("lotto.engine.candidates", candidates), patch("lotto.model_v.apply_v2", fake_apply):
        for snap in snaps:
            posted.extend(engine.process([snap]))
    return posted


def _reasons(store):
    return [row["reason"] for row in store.db.execute("SELECT reason FROM decisions ORDER BY at")]


def _contracts(store):
    return [row["contract"] for row in store.db.execute("SELECT contract FROM alerts ORDER BY at")]


class PostCooldownTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.db.close)

    def test_same_ticker_and_side_is_held_until_eight_minutes(self):
        engine = _engine(self.store)
        first, first_scores = _snap("META", 16, [("CALL", 100, 90)])
        early, early_scores = _snap("META", 23, [("CALL", 105, 88)])
        opened, opened_scores = _snap("META", 24, [("CALL", 110, 86)])
        posted = _cycles(engine, [
            (first, first_scores), (early, early_scores), (opened, opened_scores),
        ])
        self.assertEqual(len(posted), 2)
        self.assertEqual(_contracts(self.store), ["METAC100", "METAC110"])
        self.assertEqual(
            [reason for reason in _reasons(self.store) if reason.startswith("cooldown:")],
            ["cooldown: META CALL posted 420s ago"],
        )
        graded = self.store.db.execute(
            "SELECT contract FROM candidate_evaluations WHERE contract=?", ("METAC105",)).fetchone()
        self.assertEqual(graded["contract"], "METAC105")
        self.assertNotIn("METAC105", _contracts(self.store))

    def test_the_first_card_in_a_cycle_wins(self):
        engine = _engine(self.store)
        snap, scores = _snap("META", 16, [("CALL", 100, 70), ("CALL", 105, 95)])
        posted = _cycles(engine, [(snap, scores)])
        self.assertEqual(len(posted), 1)
        self.assertEqual(_contracts(self.store), ["METAC105"])
        self.assertIn("cooldown: META CALL posted 0s ago", _reasons(self.store))

    def test_the_opposite_side_is_not_held(self):
        engine = _engine(self.store)
        call, call_scores = _snap("META", 16, [("CALL", 100, 90)])
        put, put_scores = _snap("META", 18, [("PUT", 95, 90)])
        posted = _cycles(engine, [(call, call_scores), (put, put_scores)])
        self.assertEqual(len(posted), 2)
        self.assertEqual(_contracts(self.store), ["METAC100", "METAP95"])
        self.assertFalse(any(reason.startswith("cooldown:") for reason in _reasons(self.store)))

    def test_spx_and_spxw_share_one_slot(self):
        self.assertEqual(cooldown_underlying("SPXW"), cooldown_underlying("SPX"))
        engine = _engine(self.store)
        spx, spx_scores = _snap("SPX", 16, [("CALL", 7800, 90)])
        spxw, spxw_scores = _snap("SPXW", 18, [("CALL", 7810, 90)])
        other, other_scores = _snap("SPX", 18, [("PUT", 7800, 90)])
        posted = _cycles(engine, [
            (spx, spx_scores), (spxw, spxw_scores), (other, other_scores),
        ])
        self.assertEqual(len(posted), 2)
        self.assertEqual(_contracts(self.store), ["SPXC7800", "SPXP7800"])
        self.assertIn("cooldown: SPXW CALL posted 120s ago", _reasons(self.store))
        graded = self.store.db.execute(
            "SELECT 1 FROM candidate_evaluations WHERE contract=?", ("SPXWC7810",)).fetchone()
        self.assertIsNotNone(graded)

    def test_zero_disables_the_hold(self):
        engine = _engine(self.store, cooldown_minutes=0)
        first, first_scores = _snap("META", 16, [("CALL", 100, 90)])
        second, second_scores = _snap("META", 18, [("CALL", 105, 90)])
        posted = _cycles(engine, [(first, first_scores), (second, second_scores)])
        self.assertEqual(len(posted), 2)
        self.assertEqual(_contracts(self.store), ["METAC100", "METAC105"])
        self.assertFalse(any(reason.startswith("cooldown:") for reason in _reasons(self.store)))
        self.assertIsNone(cooldown_reason("META", "CALL", first.at, first.at, 0))

    def test_env_override_and_negative_rejected(self):
        self.assertEqual(model_v_settings().cooldown_minutes, 8)
        with patch.dict(os.environ, {"LOTTO_MODEL": "V", "LOTTO_COOLDOWN_MINUTES": "0"}):
            self.assertEqual(env_settings().cooldown_minutes, 0)
        with patch.dict(os.environ, {"LOTTO_MODEL": "V", "LOTTO_COOLDOWN_MINUTES": "12"}):
            self.assertEqual(env_settings().cooldown_minutes, 12)
        with self.assertRaises(ValueError):
            model_v_settings(cooldown_minutes=-1)

    def test_startup_seeds_todays_posted_cards(self):
        today = datetime.now(ET).date()
        posted_at = datetime(today.year, today.month, today.day, 12, 0, tzinfo=ET)
        spxw_at = posted_at + timedelta(minutes=1)
        yesterday = posted_at - timedelta(days=1)
        with self.store.db:
            self._alert("META", "CALL", posted_at, "sent")
            self._alert("SPXW", "CALL", spxw_at, "pending")
            self._alert("META", "PUT", posted_at, "internal")
            self._alert("META", "CALL", posted_at, "dry_run", phase="POTENTIAL", ident="watch")
            self._alert("META", "CALL", yesterday, "sent", ident="yesterday")
        engine = _engine(self.store)
        self.assertEqual(engine.post_cooldown[(today.isoformat(), "META", "CALL")], posted_at)
        self.assertEqual(engine.post_cooldown[(today.isoformat(), "SPX", "CALL")], spxw_at)
        self.assertNotIn((today.isoformat(), "META", "PUT"), engine.post_cooldown)
        self.assertNotIn((yesterday.date().isoformat(), "META", "CALL"), engine.post_cooldown)

        blocked, scores = _snap("META", 19, [("CALL", 110, 90)])
        engine.post_cooldown[("2026-10-26", "META", "CALL")] = blocked.at - timedelta(minutes=3)
        posted = _cycles(engine, [(blocked, scores)])
        self.assertEqual(posted, [])
        self.assertIn("cooldown: META CALL posted 180s ago", _reasons(self.store))

    def _alert(self, symbol, side, at, delivery, phase="ACTIVE", ident=None):
        ident = ident or f"{symbol}-{side}-{delivery}-{at.timestamp()}"
        self.store.db.execute(
            """INSERT INTO alerts
               (id, day, symbol, side, contract, at, entry_ask, payload, snapshot, delivery, phase)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ident, at.astimezone(ET).date().isoformat(), symbol, side, f"{symbol}{side}",
             at.isoformat(), 1.0, "{}", "{}", delivery, phase),
        )


if __name__ == "__main__":
    unittest.main()
