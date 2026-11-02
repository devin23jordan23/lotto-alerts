import unittest
from datetime import date, datetime

from lotto.models import Bar, ET, Option, Snapshot


class SnapshotCompatibilityTest(unittest.TestCase):
    def test_newer_saved_fields_are_ignored_without_losing_known_values(self):
        at = datetime(2026, 10, 29, 9, 31, tzinfo=ET)
        bar = Bar(at, 100, 101, 99, 100.5, 1000, 10000)
        option = Option("AAPL261029C00101000", date(2026, 10, 29), "CALL",
                        101, 1.0, 1.1, 100, 200, .3, .01, at)
        original = Snapshot("AAPL", at, 100.5, at, 100, (bar,), (option,))
        payload = original.to_dict()
        payload["options"][0]["direct_quote_verified"] = True
        payload["bars"][0]["future_bar_field"] = "unused"
        payload.update(market_return_from_open=.01, market_vwap_side="above",
                       prior_full_days=20, rvol_base=1.2)

        self.assertEqual(Snapshot.from_dict(payload), original)

        del payload["options"][0]["bid"]
        with self.assertRaises(TypeError):
            Snapshot.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
