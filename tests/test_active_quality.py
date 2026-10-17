import unittest
from dataclasses import replace
from datetime import date, datetime
from datetime import timedelta

from lotto.config import Settings
from lotto.engine import active_quality_reason
from lotto.features import Candidate
from lotto.models import ET, Option, Snapshot
from lotto.store import Store


class ActiveQualityTests(unittest.TestCase):
    def candidate(self, hour, metrics):
        at = datetime(2026, 10, 16, hour, 10, tzinfo=ET)
        option = Option('TEST', date(2026, 10, 16), 'CALL', 100, .4, .5, 100, 100, .3, .1, at)
        snapshot = Snapshot('TEST', at, 100, at, 99, (), (option,))
        return Candidate(snapshot, option, 90, {}, metrics, [], True)

    def test_weak_response_and_stock_follow_through_are_research_only(self):
        metrics = {'response_proxy': .4, 'return_5m_directional': .004}
        self.assertIsNotNone(active_quality_reason(self.candidate(10, metrics)))
        self.assertIsNone(active_quality_reason(self.candidate(10, {**metrics, 'return_5m_directional': .006})))
        self.assertIsNone(active_quality_reason(self.candidate(10, {**metrics, 'opening_expansion': 1})))

    def test_late_one_atr_chase_is_research_only(self):
        metrics = {'response_proxy': .8, 'return_5m_directional': .01, 'move_from_open_atr': 1.1}
        self.assertIsNone(active_quality_reason(self.candidate(13, metrics)))
        self.assertIsNotNone(active_quality_reason(self.candidate(14, metrics)))

    def test_gate_is_reversible_without_other_setting_changes(self):
        self.assertEqual(Settings(active_quality_gate=0).active_quality_gate, 0)
        with self.assertRaises(ValueError):
            Settings(active_quality_gate=2)

    def test_research_only_idea_tracks_option_path_without_entering_alerts(self):
        store = Store(':memory:')
        candidate = self.candidate(10, {'response_proxy': .4, 'return_5m_directional': .004})
        store.research_candidate(candidate, 'weak follow-through')
        store.research_candidate(candidate, 'weak follow-through')
        self.assertEqual(len(store.active_options(candidate.snapshot.day)), 1)
        later = candidate.snapshot.at + timedelta(minutes=1)
        option = replace(candidate.option, bid=.8, ask=.9, quote_time=later)
        snapshot = replace(candidate.snapshot, at=later, spot_time=later, options=(option,))
        store.track(snapshot, Settings())
        row = store.db.execute('SELECT max_return FROM research_ideas').fetchone()
        self.assertAlmostEqual(row['max_return'], .6)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 0)
        store.db.close()
