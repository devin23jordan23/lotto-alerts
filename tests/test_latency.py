"""Latency logging, bar-close evaluation, and options-history seeds."""
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from lotto.config import Settings, recorded_settings
from lotto.demo import demo_frames
from lotto.discord import deliver_pending
from lotto.engine import Engine
from lotto.latency import cycle_wait_seconds, fetch_plan, hot_names, next_aligned_start, request_gap
from lotto.main import env_settings
from lotto.models import ET, Snapshot
from lotto.schwab import Schwab
from lotto.store import Store


def run_demo(settings):
    store = Store(":memory:")
    engine = Engine(store, settings)
    alerts = []
    for frame in demo_frames():
        alerts.extend(engine.process(frame))
    return store, alerts


class LatencyFlagTests(unittest.TestCase):
    def test_defaults_are_off_and_excluded_from_the_hash(self):
        settings = Settings()
        self.assertEqual((settings.latency_log, settings.bar_close_eval_mode, settings.oh_seed_mode), (0, 0, 0))
        self.assertEqual(settings.bar_close_delay_s, 3)
        self.assertEqual(settings.hot_max, 12)
        self.assertEqual(settings.fast_run_mkt, "alone")
        recorded = recorded_settings(settings)
        for key in ("latency_log", "bar_close_eval_mode", "oh_seed_mode"):
            self.assertNotIn(key, recorded)
            self.assertNotIn(key, recorded_settings(Settings(**{key: 1})))
        for name, bad in (("latency_log", 2), ("latency_log", True), ("bar_close_eval_mode", 3),
                          ("bar_close_eval_mode", False), ("oh_seed_mode", True)):
            with self.assertRaises(ValueError):
                Settings(**{name: bad})
        with patch.dict("os.environ", {"LOTTO_BAR_CLOSE_EVAL_MODE": "2", "LOTTO_LATENCY_LOG": "1",
                                        "LOTTO_OH_SEED_MODE": "1"}):
            loaded = env_settings()
        self.assertEqual(loaded.bar_close_eval_mode, 2)
        self.assertEqual(loaded.latency_log, 1)
        self.assertIs(type(loaded.latency_log), int)

    def test_mode_one_matches_mode_zero_alerts(self):
        plain, plain_alerts = run_demo(Settings())
        flagged, flagged_alerts = run_demo(Settings(bar_close_eval_mode=1, oh_seed_mode=1, latency_log=0))
        self.assertEqual([item["payload"] for item in flagged_alerts], [item["payload"] for item in plain_alerts])
        self.assertEqual(flagged.summary(), plain.summary())
        self.assertGreater(flagged.db.execute("SELECT COUNT(*) FROM l2_shadow WHERE mode='L2'").fetchone()[0], 0)
        self.assertEqual(plain.db.execute("SELECT COUNT(*) FROM l2_shadow").fetchone()[0], 0)

    def test_old_database_gains_null_latency_columns(self):
        path = Path("latency-migrate.sqlite3")
        self.addCleanup(path.unlink, missing_ok=True)
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE alerts (
            id TEXT PRIMARY KEY, day TEXT, symbol TEXT, side TEXT, contract TEXT, at TEXT,
            entry_ask REAL, delivery TEXT)""")
        db.execute("INSERT INTO alerts (id, day, symbol, side, contract, at, entry_ask, delivery) "
                   "VALUES ('old','2026-11-20','AMD','CALL','C','2026-11-20T10:00:00-05:00',1,'sent')")
        db.commit()
        db.close()
        store = Store(str(path))
        row = store.db.execute(
            "SELECT symbol, break_bar_close, eval_start, card_created, delivered_at, setup_trigger FROM alerts").fetchone()
        self.assertEqual(row["symbol"], "AMD")
        self.assertIsNone(row["break_bar_close"])
        self.assertIsNone(row["delivered_at"])
        self.assertIsNone(row["setup_trigger"])


class TimingTests(unittest.TestCase):
    def test_cycles_align_to_three_seconds_past_the_minute(self):
        base = datetime(2026, 11, 24, 10, 0, 40, tzinfo=ET)
        target = next_aligned_start(base, 3)
        self.assertEqual(target.second, 3)
        self.assertEqual(target, datetime(2026, 11, 24, 10, 1, 3, tzinfo=ET))
        early = datetime(2026, 11, 24, 10, 0, 1, tzinfo=ET)
        self.assertEqual(next_aligned_start(early, 3), datetime(2026, 11, 24, 10, 0, 3, tzinfo=ET))
        exact = datetime(2026, 11, 24, 10, 0, 3, tzinfo=ET)
        self.assertEqual(next_aligned_start(exact, 3), exact)
        # The poll budget still caps the wait. A 23s alignment inside a 60s budget is kept.
        wait = cycle_wait_seconds(base, 0.0, 10.0, 60, 2, 3)
        self.assertAlmostEqual(wait, 23.0, delta=1.0)
        self.assertEqual(cycle_wait_seconds(base, 0.0, 10.0, 60, 0, 3), 50.0)

    def test_mode_two_fetches_in_play_names_before_the_sweep_without_adding_names(self):
        selected = ["AMD", "NVDA", "AAPL", "SPY"]
        hot = hot_names([("2026-11-24", "NVDA", "CALL", "2026-11-24")], ["AMD"], ["QQQ"], ["SPY", "IWM"], 12)
        self.assertEqual(hot[:3], ["NVDA", "AMD", "QQQ"])
        plan = fetch_plan(selected, hot, 2, 12)
        self.assertLess(plan.index("NVDA"), plan.index("sweep"))
        self.assertLess(plan.index("sweep"), plan.index("AAPL"))
        self.assertEqual(plan[0], "quotes")
        self.assertEqual(set(name for name in plan if name not in {"quotes", "sweep"}), set(selected))
        self.assertEqual(fetch_plan(selected, hot, 0, 12)[:2], ["quotes", "sweep"])
        self.assertEqual(request_gap(0, 5), 0.55)
        self.assertGreaterEqual(request_gap(2, 1.8), 0.55)

    def test_request_spacing_does_not_get_faster(self):
        def response(*_args, **_kwargs):
            payload = Mock()
            payload.__enter__ = Mock(return_value=io.BytesIO(b"{}"))
            payload.__exit__ = Mock(return_value=False)
            return payload
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"SCHWAB_TOKEN_BROKER_URL": "https://broker.test/t",
                                          "SCHWAB_TOKEN_BROKER_KEY": "k"}, clear=True), \
                patch("lotto.schwab.urlopen", side_effect=response):
            client = Schwab(directory)
            client.token = lambda force=False: "token"
            client.last_request = time.monotonic()
            started = time.monotonic()
            for _ in range(4):
                client.get("/quotes", {"symbols": "SPY"})
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(client.request_count, 4)
        self.assertGreaterEqual(elapsed, 3 * 0.55 - 0.05)


class DeliveryAndSameBarTests(unittest.TestCase):
    def test_sent_sets_delivered_at_and_429_does_not(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        now = datetime.now(ET) - timedelta(seconds=2)
        later = now + timedelta(seconds=1)
        payload = json.dumps({"embeds": [{"title": "x"}]})
        store.db.execute("""INSERT INTO alerts
            (id, day, symbol, side, contract, at, entry_ask, payload, delivery, phase, card_created)
            VALUES ('ok','2026-11-24','AMD','CALL','C',?,1,?,'pending','ACTIVE',?)""",
                         (now.isoformat(), payload, now.isoformat()))
        store.db.execute("""INSERT INTO alerts
            (id, day, symbol, side, contract, at, entry_ask, payload, delivery, phase, card_created)
            VALUES ('rate','2026-11-24','NVDA','PUT','P',?,1,?,'pending','ACTIVE',?)""",
                         (later.isoformat(), payload, later.isoformat()))
        store.db.commit()
        ok = Mock()
        ok.__enter__ = Mock(return_value=io.BytesIO(b""))
        ok.__exit__ = Mock(return_value=False)
        rate = HTTPError("https://discord.com/api/webhooks/test/test", 429, "slow", {}, None)
        with patch("lotto.discord.urlopen", side_effect=[ok, rate]):
            deliver_pending(store, "https://discord.com/api/webhooks/test/test", latency_log=1)
        sent = store.db.execute("SELECT delivery, delivered_at, delivery_attempts FROM alerts WHERE id='ok'").fetchone()
        held = store.db.execute("SELECT delivery, delivered_at, delivery_attempts FROM alerts WHERE id='rate'").fetchone()
        self.assertEqual(sent["delivery"], "sent")
        self.assertIsNotNone(sent["delivered_at"])
        self.assertEqual(held["delivery"], "pending")
        self.assertIsNone(held["delivered_at"])
        self.assertEqual(held["delivery_attempts"], 1)

    def test_same_bar_is_evaluated_once(self):
        frames = list(demo_frames())
        snap = frames[20][0]
        store = Store(":memory:")
        engine = Engine(store, Settings())
        engine.process([snap])
        before = store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        engine.process([snap])
        after = store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(after, before)
        self.assertGreater(before, 0)


class SeedTests(unittest.TestCase):
    def test_seed_gap_is_wider_only_for_sweep_seeds(self):
        from lotto.features import _gap_blocks
        earlier = Snapshot("AMD", datetime(2026, 11, 24, 10, 0, tzinfo=ET), 100, datetime(2026, 11, 24, 10, 0, tzinfo=ET),
                           100, (), (), source="sweep-seed")
        later = Snapshot("AMD", earlier.at + timedelta(seconds=180), 100, earlier.at, 100, (), ())
        self.assertTrue(_gap_blocks(earlier, later, 0))
        self.assertFalse(_gap_blocks(earlier, later, 200))
        self.assertTrue(_gap_blocks(later, Snapshot("AMD", later.at + timedelta(seconds=180), 100, later.at, 100, (), ()), 200))


if __name__ == "__main__":
    unittest.main()
