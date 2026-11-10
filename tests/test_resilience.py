"""Outage-resilience flags (2026-11-10). Every flag defaults to 0 = unchanged behaviour."""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from lotto import resilience
from lotto.discovery import Discovery
from lotto.engine import bars_confirm_after_gap
from lotto.features import observation_window
from lotto.models import ET
from lotto.operations import HealthMonitor
from lotto.schwab import Schwab
from lotto.store import Store

T0 = datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)


def gap_history():
    """Snapshots every 60 s to 9:44, a 7m20s outage, then every 60 s again (Nov 10 shape)."""
    times = [T0 + timedelta(minutes=m) for m in range(0, 15)]                         # 9:30-9:44
    times += [T0 + timedelta(minutes=14, seconds=440 + 60*k) for k in range(0, 3)]     # 9:51:20 on
    return [SimpleNamespace(at=t, bars=[None]*10) for t in times]


class FlagTests(unittest.TestCase):
    def test_defaults_are_off_and_bad_values_fall_back(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resilience.gap_bridge_seconds(), 0)
            self.assertEqual(resilience.flag("LOTTO_GAP_CONFIRM_MODE"), 0)
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "yes"}, clear=True):
            self.assertEqual(resilience.gap_bridge_seconds(), 0)
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "1", "LOTTO_GAP_BRIDGE_MAX_SECONDS": "60"}, clear=True):
            self.assertEqual(resilience.gap_bridge_seconds(), 150)

    def test_mode_one_bridges_only_feed_wide_outages(self):
        resilience.reset_feed_state()
        a, b = T0, T0 + timedelta(seconds=440)
        resilience.note_feed_time(a)
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "1"}, clear=True):
            self.assertFalse(resilience.gap_is_bridgeable(a, b))      # no outage seen yet
            resilience.note_feed_time(b)
            self.assertTrue(resilience.gap_is_bridgeable(a, b))
            # A name rotated out 20 min earlier is not explained by a 7 min outage.
            self.assertFalse(resilience.gap_is_bridgeable(a - timedelta(minutes=20), b))
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "2"}, clear=True):
            self.assertTrue(resilience.gap_is_bridgeable(a - timedelta(minutes=20), b))
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(resilience.gap_is_bridgeable(a, b))
        resilience.reset_feed_state()


class WindowTests(unittest.TestCase):
    def setUp(self):
        resilience.reset_feed_state()
        self.history = gap_history()
        for s in self.history:
            resilience.note_feed_time(s.at)
        self.snap = SimpleNamespace(at=self.history[-1].at + timedelta(seconds=60), bars=[None]*10)

    def tearDown(self):
        resilience.reset_feed_state()

    def test_flag_zero_keeps_window_void_after_gap(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(observation_window(self.snap, self.history))

    def test_bridge_uses_pre_gap_history_without_redating_it(self):
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "1"}, clear=True):
            five, ten, window, minutes = observation_window(self.snap, self.history)
        self.assertIn(minutes, (2, 5))
        self.assertLessEqual(five.at, self.snap.at - timedelta(minutes=minutes) + timedelta(seconds=20))
        self.assertIn(ten, self.history[:15])          # the real pre-gap sample, original timestamp
        self.assertEqual(ten.at, T0 + timedelta(minutes=14))

    def test_gap_free_window_is_preferred_once_available(self):
        history = gap_history() + [SimpleNamespace(at=T0 + timedelta(minutes=14, seconds=440 + 60*k), bars=[None]*10)
                                   for k in range(3, 6)]
        for s in history[-3:]:
            resilience.note_feed_time(s.at)
        snap = SimpleNamespace(at=history[-1].at + timedelta(seconds=60), bars=[None]*10)
        with patch.dict("os.environ", {}, clear=True):
            plain = observation_window(snap, history)
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "1"}, clear=True):
            bridged = observation_window(snap, history)
        self.assertIsNotNone(plain)
        self.assertEqual(plain, bridged)

    def test_gap_longer_than_limit_is_not_bridged(self):
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "1", "LOTTO_GAP_BRIDGE_MAX_SECONDS": "300"}, clear=True):
            self.assertIsNone(observation_window(self.snap, self.history))


class DiscoveryBridgeTests(unittest.TestCase):
    def run_gap(self, env):
        d = Discovery(8, always_deep={"SPY"})
        quote = lambda at, price, vol: {"SPY": {"price": price, "open": 100.0, "previous": 99.0,
                                                "volume": vol, "at": at}}
        with patch.dict("os.environ", env, clear=True):
            for m in range(0, 15):
                at = T0 + timedelta(minutes=m)
                d.update(quote(at, 100.0, 1000.0*m), at, ["SPY"])
            after = T0 + timedelta(minutes=14, seconds=440)
            d.update(quote(after, 101.0, 30000.0), after, ["SPY"])
        return d.history["SPY"][-1]

    def test_flag_zero_has_no_return_right_after_gap(self):
        self.assertIsNone(self.run_gap({})["return_5m"])

    def test_bridge_measures_from_last_pre_gap_quote_and_keeps_5m_volume_rate(self):
        record = self.run_gap({"LOTTO_GAP_BRIDGE_MODE": "1"})
        self.assertAlmostEqual(record["return_5m"], 0.01)
        span = 440 + 0.0  # 9:44:00 -> 9:51:20
        self.assertAlmostEqual(record["volume_5m"], (30000.0-14000.0)*300/span)


class GapConfirmationTests(unittest.TestCase):
    def candidate(self, side, trigger, building=False):
        return SimpleNamespace(option=SimpleNamespace(side=side),
                               setup=SimpleNamespace(trigger=trigger, building=building))

    def test_two_closes_through_trigger_on_card_side(self):
        snap = SimpleNamespace(bars=[SimpleNamespace(close=c) for c in (99.0, 100.6, 100.9)])
        self.assertTrue(bars_confirm_after_gap(snap, self.candidate("CALL", 100.5)))
        self.assertFalse(bars_confirm_after_gap(snap, self.candidate("CALL", 100.7)))
        self.assertFalse(bars_confirm_after_gap(snap, self.candidate("PUT", 100.5)))
        self.assertFalse(bars_confirm_after_gap(snap, self.candidate("CALL", 100.5, building=True)))


def broker_response(token="broker-token", expires=240):
    response = Mock()
    response.__enter__ = Mock(return_value=io.BytesIO(json.dumps({"access_token": token, "expires_in": expires}).encode()))
    response.__exit__ = Mock(return_value=False)
    return response


class TokenResilienceTests(unittest.TestCase):
    env = {"SCHWAB_TOKEN_BROKER_URL": "https://broker.test/schwab-token", "SCHWAB_TOKEN_BROKER_KEY": "k"}

    def test_flag_zero_raises_on_first_broker_error(self):
        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", self.env, clear=True), \
                patch("lotto.schwab.urlopen", side_effect=TimeoutError()) as open_url:
            client = Schwab(d)
            with self.assertRaises(Exception):
                client.token()
            self.assertEqual(open_url.call_count, 1)

    def test_retries_then_succeeds(self):
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1"}
        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", env, clear=True), \
                patch("lotto.schwab.time.sleep"), \
                patch("lotto.schwab.urlopen", side_effect=[TimeoutError(), ConnectionResetError(), broker_response()]) as open_url:
            client = Schwab(d)
            self.assertEqual(client.token(), "broker-token")
            self.assertEqual(open_url.call_count, 3)

    def test_cached_token_is_reused_until_real_expiry(self):
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1"}
        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", env, clear=True), \
                patch("lotto.schwab.time.sleep"):
            client = Schwab(d)
            with patch("lotto.schwab.urlopen", return_value=broker_response()):
                self.assertEqual(client.token(), "broker-token")
            client.broker_deadline = 0  # refresh due (lead time reached), token still valid
            with patch("lotto.schwab.urlopen", side_effect=TimeoutError()) as open_url:
                self.assertEqual(client.token(), "broker-token")
                self.assertEqual(open_url.call_count, 4)
            client.broker_expiry = 0  # really expired: must fail loudly, never use a dead token
            with patch("lotto.schwab.urlopen", side_effect=TimeoutError()):
                with self.assertRaises(RuntimeError):
                    client.token()

    def test_rejected_key_is_not_retried(self):
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1"}
        error = HTTPError("https://broker.test", 401, "no", {}, None)
        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", env, clear=True), \
                patch("lotto.schwab.time.sleep"), patch("lotto.schwab.urlopen", side_effect=error) as open_url:
            with self.assertRaises(RuntimeError):
                Schwab(d).token()
            self.assertEqual(open_url.call_count, 1)

    def test_refresh_happens_before_expiry(self):
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1", "LOTTO_TOKEN_REFRESH_LEAD_SECONDS": "60"}
        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", env, clear=True), \
                patch("lotto.schwab.urlopen", return_value=broker_response(expires=1800)):
            client = Schwab(d)
            client.token()
            self.assertAlmostEqual(client.broker_expiry - client.broker_deadline, 55, delta=1)


class FastHealthTests(unittest.TestCase):
    def monitor(self):
        sent = []
        return HealthMonitor(Store(":memory:"), "https://discord.com/api/webhooks/test/test",
                             sender=lambda _u, p: sent.append(p)), sent

    def test_flag_zero_waits_three_cycles(self):
        now = datetime(2026, 11, 10, 9, 44, tzinfo=ET)
        with patch.dict("os.environ", {}, clear=True):
            m, sent = self.monitor()
            m.failed_cycle(now, "x"); m.failed_cycle(now + timedelta(seconds=60), "x")
            self.assertEqual(sent, [])
            m.failed_cycle(now + timedelta(seconds=120), "x")
            self.assertEqual(len(sent), 1)

    def test_fast_mode_notifies_on_second_failure_with_duration(self):
        now = datetime(2026, 11, 10, 9, 44, tzinfo=ET)
        with patch.dict("os.environ", {"LOTTO_HEALTH_FAST_MODE": "1"}, clear=True):
            m, sent = self.monitor()
            m.failed_cycle(now, "Schwab token broker unavailable")
            self.assertEqual(sent, [])
            m.failed_cycle(now + timedelta(seconds=10), "Schwab token broker unavailable")
            self.assertEqual(len(sent), 1)
            self.assertIn("10 s", sent[0]["embeds"][0]["description"])
            m.failed_cycle(now + timedelta(seconds=20), "Schwab token broker unavailable")
            self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
