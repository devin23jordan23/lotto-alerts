import unittest
from dataclasses import replace

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.store import Store


def _frames_with_high_low():
    frames = []
    for frame in demo_frames():
        frames.append([replace(snap, prior_hl_atr=snap.prior_atr * 0.8) for snap in frame])
    return frames


def _posted(store):
    return [tuple(row) for row in store.db.execute(
        "SELECT symbol, side, contract, at, phase, entry_ask FROM alerts ORDER BY at, contract")]


def _decisions(store):
    return [tuple(row) for row in store.db.execute(
        "SELECT symbol, at, state, reason, score FROM decisions ORDER BY at, symbol, state, reason, score")]


def _qualified(store):
    return [tuple(row) for row in store.db.execute(
        """SELECT symbol, at, contract, qualifying, score FROM candidate_evaluations
           ORDER BY symbol, at, contract""")]


def _run(settings, frames):
    store = Store(":memory:")
    try:
        engine = Engine(store, settings)
        for frame in frames:
            engine.process(frame)
        return _posted(store), _decisions(store), _qualified(store)
    finally:
        store.db.close()


class DefaultPostingTests(unittest.TestCase):
    def test_shadow_defaults_post_the_same_cards_as_the_pre_gate_baseline(self):
        """Mode 1 is the current default. Mode 0 is the 7ef6597 posting behavior."""
        frames = _frames_with_high_low()
        baseline = Settings(far_otm_time_mode=0, dm_atr_basis_mode=0)
        shadow = Settings()
        self.assertEqual((shadow.far_otm_time_mode, shadow.dm_atr_basis_mode), (1, 1))
        before = _run(baseline, frames)
        after = _run(shadow, frames)
        self.assertTrue(before[0], "demo baseline posted no cards; the comparison would be empty")
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertEqual(before[2], after[2])
        live_blocks = dict(dead_zone_mode=2, burst_flat_mode=2, spent_mode=1)
        blocked_before = _run(Settings(far_otm_time_mode=0, dm_atr_basis_mode=0, **live_blocks), frames)
        blocked_after = _run(Settings(far_otm_time_mode=1, dm_atr_basis_mode=1, **live_blocks), frames)
        self.assertTrue(blocked_before[1])
        self.assertEqual(blocked_before[0], blocked_after[0])
        self.assertEqual(blocked_before[1], blocked_after[1])
        self.assertEqual(blocked_before[2], blocked_after[2])


if __name__ == "__main__":
    unittest.main()
