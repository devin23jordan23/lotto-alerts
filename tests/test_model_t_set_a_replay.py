import unittest
from datetime import datetime, timedelta, timezone

from scripts.model_t_set_a_replay import grade


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


if __name__ == "__main__":
    unittest.main()
