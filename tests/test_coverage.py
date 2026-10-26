import unittest
from datetime import datetime, timedelta, timezone

from lotto.coverage import ChainCoverage
from lotto.discovery import Discovery


class CoverageTests(unittest.TestCase):
    def test_rotating_sweep_keeps_whole_universe_fresh(self):
        symbols = [f"S{i:03}" for i in range(105)]
        deep = set(symbols[:29])
        coverage = ChainCoverage(interval_minutes=3)
        start = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)
        fresh = []
        for cycle in range(9):
            now = start + timedelta(minutes=cycle)
            for index, symbol in enumerate(coverage.due(symbols, now, deep, limit=26)):
                at = now + timedelta(seconds=index * .8)
                coverage.attempt(symbol, at)
                coverage.observe(symbol, at, ())
            for index, symbol in enumerate(sorted(deep)):
                coverage.observe(symbol, now + timedelta(seconds=27 + index * .8), (), promoted=True)
            fresh.append(coverage.count_fresh(symbols, now + timedelta(seconds=54)))
        self.assertEqual(fresh[2:], [105] * 7)

    def test_no_near_expiry_is_reset_each_session(self):
        coverage = ChainCoverage()
        day = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)
        coverage.reset(day)
        coverage.observe("TEST", day, ())
        self.assertIs(coverage.has_near_expiry["TEST"], False)
        coverage.reset(day + timedelta(days=1))
        self.assertNotIn("TEST", coverage.has_near_expiry)

    def test_stock_setup_replaces_only_unprotected_deep_slot(self):
        now = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
        discovery = Discovery(capacity=4, always_deep=("SPY",))
        discovery.observations = [
            {"symbol": symbol, "priority": priority, "promoted": symbol in {"SPY", "A", "B", "C"}}
            for symbol, priority in (("SPY", 1), ("A", 2), ("B", 3), ("C", 4), ("PANW", 10))
        ]
        idea = type("Idea", (), {"symbol": "PANW", "metrics": {"recent_move_atr": .35}})()
        chosen = discovery.prioritize_stock_ideas(["SPY", "A", "B", "C"], [idea], {"B": []}, now)
        self.assertEqual(set(chosen), {"SPY", "B", "C", "PANW"})
        self.assertEqual(len(chosen), 4)
        self.assertTrue(next(row for row in discovery.observations if row["symbol"] == "PANW")["promoted"])


if __name__ == "__main__":
    unittest.main()
