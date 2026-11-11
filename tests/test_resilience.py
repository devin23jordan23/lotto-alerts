"""Outage-resilience flags (2026-11-10). Every flag defaults to 0 = unchanged behaviour."""
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from lotto import resilience
from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.discord import alert_payload
from lotto.discovery import Discovery
from lotto.engine import bars_confirm_after_gap
from lotto.features import candidates, observation_window
from lotto.models import ET
from lotto.operations import HealthMonitor
from lotto.schwab import Schwab, benchmark_for
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

    def test_clean_shorter_window_beats_a_longer_one_that_straddles_a_gap(self):
        # A 4-minute hole sits inside the 5-minute window's tolerance, while the
        # 2-minute window is already contiguous. Flag 0 still voids on the first
        # matching width. Flag 1 keeps the clean window instead of bridging the hole.
        resilience.reset_feed_state()
        snap_at = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
        missing = {snap_at - timedelta(minutes=m) for m in (10, 9, 8)}
        times = [snap_at - timedelta(minutes=m) for m in range(30, 0, -1)
                 if snap_at - timedelta(minutes=m) not in missing]
        history = [SimpleNamespace(at=t, bars=[None] * 10) for t in times]
        resilience.note_feed_time(snap_at - timedelta(minutes=11))
        resilience.note_feed_time(snap_at - timedelta(minutes=7))
        snap = SimpleNamespace(at=snap_at, bars=[None] * 10)
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(observation_window(snap, history))
        with patch.dict("os.environ", {"LOTTO_GAP_BRIDGE_MODE": "1"}, clear=True):
            _five, ten, window, minutes = observation_window(snap, history)
        self.assertEqual(minutes, 2)
        self.assertGreaterEqual(ten.at, snap_at - timedelta(minutes=7))
        self.assertFalse(any((b.at - a.at).total_seconds() > 150 for a, b in zip(window, window[1:])))


class DiscoveryBridgeTests(unittest.TestCase):
    def run_gap(self, env):
        resilience.reset_feed_state()
        d = Discovery(8, always_deep={"SPY"})
        quote = lambda at, price, vol: {"SPY": {"price": price, "open": 100.0, "previous": 99.0,
                                                "volume": vol, "at": at}}
        with patch.dict("os.environ", env, clear=True):
            for m in range(0, 15):
                at = T0 + timedelta(minutes=m)
                d.update(quote(at, 100.0, 1000.0*m), at, ["SPY"])
            after = T0 + timedelta(minutes=14, seconds=440)
            d.update(quote(after, 101.0, 30000.0), after, ["SPY"])
        resilience.reset_feed_state()
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

    def test_fast_mode_does_not_carry_the_clock_across_the_close(self):
        now = datetime(2026, 11, 10, 15, 59, tzinfo=ET)
        with patch.dict("os.environ", {"LOTTO_HEALTH_FAST_MODE": "1"}, clear=True):
            m, sent = self.monitor()
            self.assertFalse(m.failed_cycle(now, "x"))
            self.assertFalse(m.failed_cycle(now + timedelta(minutes=2), "x"))
            opened = datetime(2026, 11, 11, 9, 31, tzinfo=ET)
            self.assertFalse(m.failed_cycle(opened, "x"))
            self.assertTrue(m.failed_cycle(opened + timedelta(seconds=10), "x"))
            self.assertEqual(len(sent), 1)


def quote_at(at, price):
    return {"price": price, "open": price, "previous": price, "volume": 1000.0, "at": at}


class BenchmarkBridgeTests(unittest.TestCase):
    """sector_context reads the benchmark return (SMH for MU/AVGO/AMD, SPY for QQQ)."""

    before = {"MU": 100.0, "AMD": 110.0, "AVGO": 120.0, "QQQ": 400.0, "SMH": 200.0, "SPY": 500.0}
    after_prices = {"MU": 101.0, "AMD": 112.0, "AVGO": 123.0, "QQQ": 404.0, "SMH": 202.0, "SPY": 505.0}

    def tearDown(self):
        resilience.reset_feed_state()

    def feed(self, prices_before, prices_after, bridge_mode, hold=()):
        resilience.reset_feed_state()
        discovery = Discovery(12)
        env = {"LOTTO_GAP_BRIDGE_MODE": str(bridge_mode)} if bridge_mode else {}
        with patch.dict("os.environ", env, clear=True):
            for minute in range(15):
                at = T0 + timedelta(minutes=minute)
                discovery.update({s: quote_at(at, p) for s, p in prices_before.items()}, at, list(prices_before))
            if hold:
                for step in range(1, 8):
                    at = T0 + timedelta(minutes=14, seconds=60 * step)
                    discovery.update({s: quote_at(at, prices_before[s]) for s in hold}, at, list(hold))
            after = T0 + timedelta(minutes=14, seconds=440)
            discovery.update({s: quote_at(after, p) for s, p in prices_after.items()}, after, list(prices_after))
        return discovery, after

    def sector_context(self, symbol, label, context_return):
        snaps = [frame[0] for frame in demo_frames()]
        when = snaps[30].at if context_return is not None else None
        snap = replace(snaps[30], symbol=symbol, context_label=label,
                       context_return_5m=context_return, context_time=when)
        with patch.dict("os.environ", {}, clear=True):
            found = candidates(snap, snaps[10:30], Settings())
        self.assertTrue(found)
        for candidate in found:
            self.assertTrue(candidate.option and candidate.option.symbol)
            card = alert_payload(candidate, "POTENTIAL" if candidate.setup and candidate.setup.building else "ACTIVE")
            self.assertIn(f"{candidate.option.strike:g}", card["embeds"][0]["description"])
            self.assertIn(f"${candidate.option.ask:.2f}", card["embeds"][0]["description"])
        return found[0].parts["sector_context"], found[0].metrics["context_directional_return"]

    def test_flag_off_leaves_benchmark_return_missing_and_sector_context_zero(self):
        self.assertEqual(benchmark_for("MU"), "SMH")
        self.assertEqual(benchmark_for("AMD"), "SMH")
        self.assertEqual(benchmark_for("AVGO"), "SMH")
        self.assertEqual(benchmark_for("QQQ"), "SPY")
        discovery, after = self.feed(self.before, self.after_prices, 0)
        self.assertEqual(discovery.return_5m("SMH", after), (None, None))
        self.assertEqual(discovery.return_5m("SPY", after), (None, None))
        term, context = self.sector_context("MU", "SMH", None)
        self.assertEqual(term, 0)
        self.assertEqual(context, 0)
        # The same contract scores a benchmark term when the return is present.
        present, _ = self.sector_context("MU", "SMH", 0.01)
        self.assertGreater(present, 0)

    def test_feed_gap_bridges_benchmark_into_sector_context(self):
        discovery, after = self.feed(self.before, self.after_prices, 1)
        smh, smh_at = discovery.return_5m("SMH", after)
        spy, spy_at = discovery.return_5m("SPY", after)
        self.assertAlmostEqual(smh, 202.0 / 200.0 - 1)
        self.assertAlmostEqual(spy, 505.0 / 500.0 - 1)
        self.assertLessEqual((after - smh_at).total_seconds(), 90)
        self.assertLessEqual((after - spy_at).total_seconds(), 90)
        mu_term, mu_context = self.sector_context("MU", "SMH", smh)
        qqq_term, qqq_context = self.sector_context("QQQ", "SPY", spy)
        self.assertGreater(mu_term, 0)
        self.assertGreater(qqq_term, 0)
        self.assertAlmostEqual(mu_context, smh)
        self.assertAlmostEqual(qqq_context, spy)

    def test_mode_one_does_not_bridge_a_benchmark_while_the_feed_keeps_printing(self):
        discovery, after = self.feed(self.before, self.after_prices, 1, hold=("SPY",))
        self.assertEqual(discovery.return_5m("SMH", after), (None, None))
        term, _ = self.sector_context("MU", "SMH", None)
        self.assertEqual(term, 0)
        bridged, after = self.feed(self.before, self.after_prices, 2, hold=("SPY",))
        value, _ = bridged.return_5m("SMH", after)
        self.assertAlmostEqual(value, 202.0 / 200.0 - 1)


if __name__ == "__main__":
    unittest.main()
