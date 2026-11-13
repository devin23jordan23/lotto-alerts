import unittest
from datetime import datetime, timedelta, timezone

from scripts.model_t_set_a_replay import grade, plus_two_entry, flags


class ModelTSetAReplayTest(unittest.TestCase):
    def test_stop_first_is_not_clean_even_when_bid_later_doubles(self):
        start = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
        path = []
        for minutes, bid in ((1, .70), (3, 2.10)):
            at = (start + timedelta(minutes=minutes)).isoformat()
            path.append((at, at, bid))
        result = grade(path, start, 1.0, .28)
        self.assertFalse(result["clean"])
        self.assertTrue(result["peak100"])
        self.assertTrue(result["stop_first"])

    def test_target_first_is_clean_and_stale_quote_is_ignored(self):
        start = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
        good = (start + timedelta(minutes=1)).isoformat()
        later = (start + timedelta(minutes=2)).isoformat()
        stale = (start - timedelta(minutes=3)).isoformat()
        path = [(good, stale, .10), (good, good, 1.50), (later, later, .70)]
        result = grade(path, start, 1.0, .28)
        self.assertTrue(result["clean"])
        self.assertFalse(result["stop_first"])

    def test_single_target_print_does_not_count_as_sustained(self):
        start = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
        path = []
        for minute, bid in ((1, 1.5), (2, 1.1), (3, .7), (4, .69)):
            stamp = (start + timedelta(minutes=minute)).isoformat()
            path.append((stamp, stamp, bid, bid + .1))
        self.assertTrue(grade(path, start, 1.0, .28)["clean"])
        self.assertFalse(grade(path, start, 1.0, .28, required_observations=2)["clean"])

    def test_late_entry_uses_only_ask_observed_after_two_minutes(self):
        start = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
        path = []
        for minute, ask in ((1, 1.0), (2, 1.3), (3, 1.4)):
            stamp = (start + timedelta(minutes=minute)).isoformat()
            path.append((stamp, stamp, ask - .1, ask))
        self.assertEqual(plus_two_entry(path, start), (start + timedelta(minutes=2), 1.3))

    def test_against_open_exempts_intraday_reversal(self):
        base = {"dte": 0, "metrics": {"move_from_open_atr": -0.3}}
        self.assertFalse(flags({**base, "setup": "INTRADAY_REVERSAL"})["against_open"])
        self.assertFalse(flags({**base, "setup": "REVERSAL"})["against_open"])
        self.assertTrue(flags({**base, "setup": "CONTINUATION"})["against_open"])


if __name__ == "__main__":
    unittest.main()
