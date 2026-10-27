"""Decision boundaries for the optional alignment pack."""
import unittest
from datetime import date, datetime, timedelta

from lotto.alignment import alignment_rows, apply_alignment_penalties
from lotto.config import model_v_settings
from lotto.discord import alert_payload
from lotto.features import Candidate
from lotto.models import Bar, ET, Option, Snapshot
from lotto.volume_trend import alignment_features
from lotto.vt_measure import measure_volume


START = datetime(2026, 10, 27, 9, 31, tzinfo=ET)
TOD = {10: {minute: 100 for minute in range(390)}}


def bars(*, side="PUT", last_break=False, last_volume=50):
    rows = []
    for minute in range(16):
        end = START + timedelta(minutes=minute)
        if last_break and minute == 15:
            rows.append(Bar(end, 340.5, 341.0, 339.0, 339.5, last_volume))
        else:
            rows.append(Bar(end, 340.5, 342.0, 340.0,
                            341.0 if minute == 15 else 340.6, 50))
    return rows


class AlignmentPackTests(unittest.TestCase):
    def setUp(self):
        self.pack = model_v_settings(alignment_pack=1)

    def _avgo_rows(self, **overrides):
        inputs = dict(
            side="PUT", option_v2=70, dte=0, bars=bars(), spot=341,
            strike=335, atr=10, levels={}, tod=TOD,
            raw={"BU_5_TOD_LAST": 0.5, "RVOL_CUM": 0.7},
            sources={"RVOL_CUM": "RVOL_CUM_10"},
        )
        inputs.update(overrides)
        return alignment_rows(self.pack, **inputs)

    def test_pack_is_off_until_selected_and_explicit_zero_wins(self):
        self.assertEqual(model_v_settings().pen_stock_flat, 0)
        self.assertEqual(model_v_settings().gate_alignment, 0)
        self.assertEqual(self.pack.pen_stock_flat, -40)
        self.assertEqual(self.pack.gate_alignment, 0)
        self.assertEqual(model_v_settings(alignment_pack=1, pen_stock_flat=0).pen_stock_flat, 0)

    def test_avgo_put_location_and_weak_stock_take_122_below_120(self):
        result = self._avgo_rows()
        self.assertEqual(result["PEN_RANGE_POS"], -30)
        self.assertEqual(result["PEN_VWAP_SIDE_WRONG"], -20)
        self.assertEqual(result["PEN_OTM_FAR"], -15)
        self.assertEqual(result["PEN_STOCK_VOL_VERY_WEAK"], -40)
        self.assertLess(122 + sum(result.values()), 120)
        at = START + timedelta(minutes=15)
        option = Option("AVGO 335P", date(2026, 10, 27), "PUT", 335, 0.4,
                        0.5, 100, 100, -0.3, 0.02, at)
        snap = Snapshot("AVGO", at, 341, at, 340, tuple(bars()), (option,), prior_atr=10)
        object.__setattr__(snap, "vt_tod", TOD)
        candidate = Candidate(snap, option, 122, {}, {"option_v2": 70}, [], True)
        adjusted, applied = apply_alignment_penalties(122, candidate, self.pack, features={
            "_vt_raw": {"BU_5_TOD_LAST": 0.5, "RVOL_CUM": 0.7},
            "_vt_sources": {"RVOL_CUM": "RVOL_CUM_10"},
        })
        self.assertLess(adjusted, 120)
        self.assertEqual(candidate.metrics["alignment_rows"], applied)

    def test_fresh_put_break_exempts_location_and_signed_break_volume(self):
        result = self._avgo_rows(bars=bars(last_break=True, last_volume=200))
        self.assertNotIn("PEN_RANGE_POS", result)
        self.assertNotIn("PEN_OTM_FAR", result)
        self.assertNotIn("PEN_STOCK_VOL_VERY_WEAK", result)

    def test_call_location_mirrors_put_and_zero_range_is_unknown(self):
        low_call = [Bar(START + timedelta(minutes=i), 100, 102, 98,
                        99 if i == 15 else 100, 100) for i in range(16)]
        result = alignment_rows(self.pack, side="CALL", bars=low_call,
                                spot=99, strike=101, atr=10)
        self.assertEqual(result["PEN_RANGE_POS"], -50)
        self.assertEqual(result["PEN_VWAP_SIDE_WRONG"], -20)
        self.assertEqual(result["PEN_OTM_FAR"], -15)
        no_range = [Bar(START + timedelta(minutes=i), 100, 100, 100, 100, 100)
                    for i in range(16)]
        self.assertNotIn("PEN_RANGE_POS", alignment_rows(
            self.pack, side="CALL", bars=no_range, atr=10))

    def test_up_bar_wick_break_cannot_exempt_put_from_weak_volume(self):
        rows = bars()
        rows[-1] = Bar(rows[-1].end, 340.5, 342.0, 339.0, 341.0, 200)
        result = self._avgo_rows(bars=rows)
        self.assertIn("PEN_STOCK_VOL_VERY_WEAK", result)

    def test_flat_and_slow_mirror_and_missing_cumulative_baseline_is_unknown(self):
        quiet = [Bar(START + timedelta(minutes=i), 100.0, 100.05, 99.95,
                     100.0, 50) for i in range(15)]
        for side in ("CALL", "PUT"):
            result = alignment_rows(self.pack, side=side, bars=quiet, atr=10,
                                    raw={"BU_5_TOD_LAST": 0.5, "RVOL_CUM": 0.5},
                                    sources={"RVOL_CUM": "BAR_VS_DAY"})
            self.assertEqual(result["PEN_STOCK_FLAT"], -40)
            self.assertNotIn("PEN_STOCK_VOL_VERY_WEAK", result)
        early = quiet[:5]
        result = alignment_rows(self.pack, side="CALL", bars=early, atr=10)
        self.assertEqual(result["PEN_STOCK_SLOW"], -20)
        self.assertNotIn("PEN_STOCK_FLAT", result)

    def test_three_of_twenty_two_red_peers_do_not_pay_avgo_put(self):
        peers = ([{"symbol": f"R{i}", "open": 100, "last": 99} for i in range(3)]
                 + [{"symbol": f"G{i}", "open": 100, "last": 101} for i in range(19)])
        self.assertEqual(alignment_features(direction=-1, peers=peers)["SECTOR_PEER_COUNT"], 1)
        self.assertEqual(alignment_features(direction=-1, peers=peers,
                                            peer_side_aware=True)["SECTOR_PEER_COUNT"], 0)

    def test_signed_volume_does_not_credit_green_bars_to_put(self):
        series = bars()
        unsigned = measure_volume(series, -1, {}, 10, tod=TOD,
                                  cum_curves={10: 1600}, signed=False)
        signed = measure_volume(series, -1, {}, 10, tod=TOD,
                                cum_curves={10: 1600}, signed=True)
        self.assertEqual(unsigned["raw"]["BU_5_TOD_LAST"], 0.5)
        self.assertEqual(signed["raw"]["BU_5_TOD_LAST"], 0)
        self.assertEqual(signed["raw"]["RVOL_CUM"], 0)

    def test_discord_card_lists_each_applied_penalty(self):
        at = START + timedelta(minutes=15)
        option = Option("AVGO 335P", date(2026, 10, 27), "PUT", 335, 0.4,
                        0.5, 100, 100, -0.3, 0.02, at)
        snap = Snapshot("AVGO", at, 341, at, 340, tuple(bars()), (option,))
        candidate = Candidate(snap, option, 77, {}, {
            "alignment_rows": {"PEN_RANGE_POS": -30, "PEN_VWAP_SIDE_WRONG": -20}}, [], True)
        description = alert_payload(candidate, "ACTIVE")["embeds"][0]["description"]
        self.assertIn("PEN_RANGE_POS: -30", description)
        self.assertIn("PEN_VWAP_SIDE_WRONG: -20", description)


if __name__ == "__main__":
    unittest.main()
