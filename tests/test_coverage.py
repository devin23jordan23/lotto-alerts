import unittest
from datetime import datetime, timedelta, timezone

from lotto.coverage import ChainCoverage


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


if __name__ == "__main__":
    unittest.main()
