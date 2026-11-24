"""Inline Discord delivery, 429 retry, latency columns, and warmup after a full disk."""
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from lotto.config import Settings, recorded_settings
from lotto.demo import demo_frames
from lotto.discord import deliver_pending, publish_ready, retry_after_seconds, stamp_would_send
from lotto.engine import Engine
from lotto.main import env_settings, handle_hot_snapshot, review_then_warm
from lotto.nightly import run_nightly
from lotto.schwab import Schwab, hot_worker_count
from lotto.store import Store
from tests.test_scanner_fixes import aligned_bench, climb


def run_demo(settings, dry_run=True):
    store = Store(":memory:")
    engine = Engine(store, settings, dry_run=dry_run)
    alerts = []
    for frame in demo_frames():
        alerts.extend(engine.process(frame))
    return store, alerts


def ok_response():
    payload = Mock()
    payload.__enter__ = Mock(return_value=io.BytesIO(b""))
    payload.__exit__ = Mock(return_value=False)
    return payload


def rate_limit(retry_after, header=None):
    body = json.dumps({"message": "slow", "retry_after": retry_after}).encode()
    headers = {"Retry-After": header} if header is not None else {}
    return HTTPError("https://discord.com/api/webhooks/test/test", 429, "slow", headers, io.BytesIO(body))


WEBHOOK = "https://discord.com/api/webhooks/test/test"


def pending(store, ident, at, phase="ACTIVE"):
    payload = json.dumps({"embeds": [{"title": ident}]})
    store.db.execute("""INSERT INTO alerts
        (id, day, symbol, side, contract, at, entry_ask, payload, delivery, phase)
        VALUES (?,?, 'AMD', 'CALL', 'C', ?, 1, ?, 'pending', ?)""",
                     (ident, at.date().isoformat(), at.isoformat(), payload, phase))
    store.db.commit()


class FlagTests(unittest.TestCase):
    def test_defaults_are_off_excluded_and_rejected_when_invalid(self):
        settings = Settings()
        self.assertEqual((settings.inline_delivery, settings.discord_429_retry,
                          settings.hot_parallel_mode, settings.warmup_independent_mode), (0, 0, 0, 0))
        recorded = recorded_settings(settings)
        for key in ("inline_delivery", "discord_429_retry", "hot_parallel_mode", "warmup_independent_mode"):
            self.assertNotIn(key, recorded)
            self.assertNotIn(key, recorded_settings(Settings(**{key: 1})))
            with self.assertRaises(ValueError):
                Settings(**{key: True})
            with self.assertRaises(ValueError):
                Settings(**{key: 3})
        with patch.dict("os.environ", {"LOTTO_INLINE_DELIVERY": "2", "LOTTO_DISCORD_429_RETRY": "1",
                                        "LOTTO_HOT_PARALLEL_MODE": "1", "LOTTO_WARMUP_INDEPENDENT_MODE": "1"}):
            loaded = env_settings()
        self.assertEqual((loaded.inline_delivery, loaded.discord_429_retry,
                          loaded.hot_parallel_mode, loaded.warmup_independent_mode), (2, 1, 1, 1))
        self.assertIs(type(loaded.inline_delivery), int)

    def test_flags_do_not_change_which_cards_fire(self):
        plain, plain_alerts = run_demo(Settings())
        off, off_alerts = run_demo(Settings(inline_delivery=0, discord_429_retry=0,
                                            hot_parallel_mode=0, warmup_independent_mode=0))
        on, on_alerts = run_demo(Settings(inline_delivery=2, discord_429_retry=2,
                                          hot_parallel_mode=2, warmup_independent_mode=2))
        self.assertEqual([item["payload"] for item in off_alerts], [item["payload"] for item in plain_alerts])
        self.assertEqual([item["payload"] for item in on_alerts], [item["payload"] for item in plain_alerts])
        self.assertEqual(off.summary(), plain.summary())
        self.assertEqual(on.summary(), plain.summary())


class InlineTimingTests(unittest.TestCase):
    def test_send_order_follows_the_flag(self):
        built = {"n": 0}

        class FakeEngine:
            def process(self, snaps):
                built["n"] += 1
                return [{"id": str(built["n"]), "phase": "ACTIVE",
                         "payload": {"embeds": [{"description": "x"}]}}]

        def run(mode):
            events = []
            engine = FakeEngine()
            early = []
            for _ in range(2):
                handle_hot_snapshot(object(), engine, early, mode, True,
                                    lambda: events.append("send"),
                                    lambda alerts: events.append("stamp"))
                events.append("sweep")
            return events, early

        self.assertEqual(run(2)[0], ["send", "sweep", "send", "sweep"])
        self.assertEqual(run(1)[0], ["stamp", "sweep", "stamp", "sweep"])
        self.assertEqual(run(0)[0], ["sweep", "sweep"])
        events = []
        publish_ready([{"id": "a", "phase": "ACTIVE"}], 0, True,
                      lambda: events.append("send"), lambda alerts: events.append("stamp"))
        self.assertEqual(events, [])

    def test_mode_one_records_the_time_and_does_not_post_early(self):
        store = Store(":memory:")
        now = datetime.now(timezone.utc) - timedelta(seconds=2)
        pending(store, "card", now)
        noted = []
        with patch("lotto.discord.urlopen") as send:
            publish_ready([{"id": "card", "phase": "ACTIVE"}], 1, True,
                          lambda: send(), lambda alerts: stamp_would_send(store, alerts) or noted.append(alerts[0]["id"]))
        send.assert_not_called()
        row = store.db.execute("SELECT delivery, inline_would_send_at FROM alerts WHERE id='card'").fetchone()
        self.assertEqual(row["delivery"], "pending")
        self.assertIsNotNone(row["inline_would_send_at"])
        self.assertEqual(noted, ["card"])
        with patch("lotto.discord.urlopen", return_value=ok_response()):
            deliver_pending(store, WEBHOOK, latency_log=1)
        sent = store.db.execute("SELECT delivery, delivered_at FROM alerts WHERE id='card'").fetchone()
        self.assertEqual(sent["delivery"], "sent")
        self.assertIsNotNone(sent["delivered_at"])


class RetryTests(unittest.TestCase):
    def test_retry_after_comes_from_the_body(self):
        self.assertEqual(retry_after_seconds(rate_limit(1.5, header="9")), 1.5)
        header_only = HTTPError(WEBHOOK, 429, "slow", {"Retry-After": "2"}, io.BytesIO(b"{}"))
        self.assertEqual(retry_after_seconds(header_only), 2.0)

    def test_flag_zero_still_stops_the_loop(self):
        store = Store(":memory:")
        now = datetime.now(timezone.utc) - timedelta(seconds=2)
        pending(store, "first", now)
        pending(store, "second", now + timedelta(seconds=1))
        with patch("lotto.discord.urlopen", side_effect=[rate_limit(0.2), ok_response()]) as send:
            deliver_pending(store, WEBHOOK, latency_log=1, retry_429=0, sleeper=lambda seconds: None)
        self.assertEqual(send.call_count, 1)
        rows = {row["id"]: row["delivery"] for row in store.db.execute("SELECT id, delivery FROM alerts")}
        self.assertEqual(rows, {"first": "pending", "second": "pending"})

    def test_retries_then_sends_and_continues(self):
        store = Store(":memory:")
        now = datetime.now(timezone.utc) - timedelta(seconds=2)
        pending(store, "first", now)
        pending(store, "second", now + timedelta(seconds=1))
        waits = []
        with patch("lotto.discord.urlopen", side_effect=[rate_limit(0.4), ok_response(), ok_response()]):
            deliver_pending(store, WEBHOOK, latency_log=1, retry_429=1,
                            sleeper=lambda seconds: waits.append(seconds))
        self.assertEqual(waits, [0.4])
        rows = {row["id"]: row for row in store.db.execute(
            "SELECT id, delivery, delivered_at, delivery_attempts FROM alerts")}
        self.assertEqual(rows["first"]["delivery"], "sent")
        self.assertIsNotNone(rows["first"]["delivered_at"])
        self.assertEqual(rows["first"]["delivery_attempts"], 1)
        self.assertEqual(rows["second"]["delivery"], "sent")
        self.assertIsNotNone(rows["second"]["delivered_at"])

    def test_age_cap_does_not_post_and_the_next_card_still_goes(self):
        store = Store(":memory:")
        now = datetime.now(timezone.utc)
        pending(store, "old", now - timedelta(seconds=100))
        pending(store, "fresh", now - timedelta(seconds=1))
        pending(store, "stale", now - timedelta(seconds=121))
        waits = []
        with patch("lotto.discord.urlopen", side_effect=[rate_limit(30), ok_response()]) as send:
            deliver_pending(store, WEBHOOK, latency_log=1, retry_429=2,
                            sleeper=lambda seconds: waits.append(seconds))
        self.assertEqual(waits, [])
        self.assertEqual(send.call_count, 2)
        rows = {row["id"]: row["delivery"] for row in store.db.execute("SELECT id, delivery FROM alerts")}
        self.assertEqual(rows["old"], "pending")
        self.assertEqual(rows["fresh"], "sent")
        self.assertEqual(rows["stale"], "expired")

    def test_three_attempts_then_the_next_card(self):
        store = Store(":memory:")
        now = datetime.now(timezone.utc) - timedelta(seconds=2)
        pending(store, "burst", now)
        pending(store, "after", now + timedelta(seconds=1))
        with patch("lotto.discord.urlopen", side_effect=[rate_limit(0.01)] * 3 + [ok_response()]) as send:
            deliver_pending(store, WEBHOOK, retry_429=1, sleeper=lambda seconds: None)
        self.assertEqual(send.call_count, 4)
        rows = {row["id"]: row["delivery"] for row in store.db.execute("SELECT id, delivery FROM alerts")}
        self.assertEqual(rows["burst"], "pending")
        self.assertEqual(rows["after"], "sent")


class LatencyColumnTests(unittest.TestCase):
    def test_setup_trigger_and_delivered_at_are_written_when_logging_is_on(self):
        # The main queue already stored setup_trigger. Stock-first did not, and
        # delivered_at stays empty until a webhook accepts the card (dry-run never sends).
        store = Store(":memory:")
        engine = Engine(store, Settings(stock_first_mode=2, stock_first_cap=2, late_move_atr=1.1, latency_log=1),
                        dry_run=False)
        snap = climb(30)
        engine.bench.update(aligned_bench(snap.at))
        alerts = engine.process([snap])
        self.assertEqual(len(alerts), 1)
        row = store.db.execute(
            "SELECT setup_trigger, card_created, delivered_at, delivery FROM alerts").fetchone()
        self.assertIsNotNone(row["setup_trigger"])
        self.assertIsNotNone(row["card_created"])
        self.assertIsNone(row["delivered_at"])
        self.assertEqual(row["delivery"], "pending")
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        store.db.execute("UPDATE alerts SET at=?", (fresh,))
        store.db.commit()
        with patch("lotto.discord.urlopen", return_value=ok_response()):
            deliver_pending(store, WEBHOOK, latency_log=1)
        sent = store.db.execute("SELECT delivered_at, setup_trigger FROM alerts").fetchone()
        self.assertIsNotNone(sent["delivered_at"])
        self.assertEqual(sent["setup_trigger"], row["setup_trigger"])

    def test_demo_card_records_setup_trigger_only_when_the_flag_is_on(self):
        logged, alerts = run_demo(Settings(latency_log=1))
        self.assertTrue(alerts)
        row = logged.db.execute("SELECT setup_trigger, card_created, delivered_at FROM alerts").fetchone()
        self.assertIsNotNone(row["setup_trigger"])
        self.assertIsNotNone(row["card_created"])
        self.assertIsNone(row["delivered_at"])
        quiet, _ = run_demo(Settings(latency_log=0))
        blank = quiet.db.execute("SELECT setup_trigger, card_created, delivered_at FROM alerts").fetchone()
        self.assertIsNone(blank["setup_trigger"])
        self.assertIsNone(blank["card_created"])


class WarmupTests(unittest.TestCase):
    def test_failed_review_still_warms_only_when_the_flag_is_on(self):
        now = datetime.now(timezone.utc)
        client = Mock()
        with patch("lotto.main.maybe_nightly", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                review_then_warm(Mock(), client, ["SPY"], "data", now, False)
        client.warm_universe.assert_not_called()
        client.warm_universe.reset_mock()
        with patch("lotto.main.maybe_nightly", side_effect=OSError(28, "No space left on device")):
            review_then_warm(Mock(), client, ["SPY"], "data", now, True)
        client.warm_universe.assert_called_once()

    def test_report_write_fails_soft_without_deleting_files(self):
        store = Store(":memory:")
        with tempfile.TemporaryDirectory() as directory:
            saved = run_nightly(store, "2026-11-20", directory)
            self.assertTrue(saved.exists())
            real = Path.write_text

            def full(self, *args, **kwargs):
                if self.name == "report.tmp":
                    raise OSError(28, "No space left on device")
                return real(self, *args, **kwargs)

            with patch.object(Path, "unlink", side_effect=AssertionError("no deletions")), \
                    patch.object(Path, "write_text", full):
                with self.assertRaises(OSError):
                    run_nightly(store, "2026-11-21", directory, fail_soft=False)
                missed = run_nightly(store, "2026-11-22", directory, fail_soft=True)
            self.assertIsNone(missed)
            self.assertTrue(saved.exists())
            self.assertTrue((Path(directory) / "nightly" / "2026-11-20" / "report.json").exists())


class ParallelFetchTests(unittest.TestCase):
    def test_worker_cap_and_request_spacing(self):
        self.assertEqual(hot_worker_count(0, 7), 1)
        self.assertEqual(hot_worker_count(1, 7), 1)
        self.assertEqual(hot_worker_count(2, 1), 1)
        self.assertEqual(hot_worker_count(2, 3), 3)
        self.assertEqual(hot_worker_count(2, 9), 4)

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
            threads = [threading.Thread(target=lambda: client.get("/quotes", {"symbols": "SPY"})) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            elapsed = time.monotonic() - started
        self.assertEqual(client.request_count, 4)
        self.assertGreaterEqual(elapsed, 3 * 0.55 - 0.05)


if __name__ == "__main__":
    unittest.main()
