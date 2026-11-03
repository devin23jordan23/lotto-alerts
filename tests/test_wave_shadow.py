import csv
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import broke_20d_range, candidates
from lotto.main import env_settings
from lotto.models import Bar, ET, Option, Snapshot
from lotto.patterns import detect_setup, path_features
from lotto.schwab import Schwab
from lotto.shadow_export import export_wave_shadow
from lotto.store import Store
from lotto.features import Candidate
from lotto.patterns import Setup
from lotto.wave import (
    aggregate, card_tags, checkpoints, detect_f1, detect_f2, detect_f4, detect_f7,
    detect_signals, log_rejects, pick_contract, run_shadow, volume_passed,
)


def _bars(start, rows, volume=200):
    bars = []
    total = 0
    for index, row in enumerate(rows):
        opened, high, low, closed = row[:4]
        size = row[4] if len(row) > 4 else volume
        total += size
        at = start + timedelta(minutes=index + 1)
        bars.append(Bar(at, opened, high, low, closed, size, float(total)))
    return tuple(bars)


def _repeat(spec, copies=5):
    return [spec] * copies


def _option(at, side="CALL", delta=0.40, strike=110.0, bid=0.40, ask=0.45):
    mark = "C" if side == "CALL" else "P"
    signed = delta if side == "CALL" else -abs(delta)
    return Option(f"TEST{strike:.0f}{mark}", at.astimezone(ET).date(), side, strike,
                  bid, ask, 200, 100, signed, 0.05, at)


def _snap(bars, **kwargs):
    last = bars[-1]
    options = kwargs.pop("options", ())
    return Snapshot("TEST", last.end, kwargs.pop("spot", last.close), last.end,
                    kwargs.pop("prior_close", 90.0), bars, options,
                    prior_atr=kwargs.pop("prior_atr", 10.0),
                    prior_atr21=kwargs.pop("prior_atr21", 10.0), **kwargs)


def _f4_rows():
    # Shortest pole is two 5m bars; the flag is the next two; the fifth breaks.
    pole0 = (100.0, 101.0, 99.0, 100.5, 200)
    pole1 = (100.5, 105.2, 100.2, 105.0, 200)
    flag2 = (105.0, 105.4, 103.0, 104.0, 160)
    flag3 = (104.0, 105.5, 103.2, 104.5, 160)
    broke = (104.5, 107.0, 104.4, 106.5, 2000)
    rows = []
    for spec in (pole0, pole1, flag2, flag3, broke):
        rows.extend(_repeat(spec))
    return rows


def _mirror_bar(bar, center=300.0):
    return replace(bar, open=center - bar.open, high=center - bar.low,
                   low=center - bar.high, close=center - bar.close)


def _dump(store):
    alerts = [tuple(row) for row in store.db.execute(
        """SELECT id, day, symbol, side, contract, at, entry_ask, payload, snapshot, delivery,
                  max_return, min_return, latest_return, last_quote, closed, phase, parent_id, thesis_id
           FROM alerts ORDER BY id""")]
    candidates_rows = [tuple(row) for row in store.db.execute(
        """SELECT day, symbol, at, contract, strategy, qualifying, score, features
           FROM candidate_evaluations ORDER BY at, contract""")]
    decisions = [tuple(row) for row in store.db.execute(
        "SELECT symbol, at, state, reason, score, features FROM decisions ORDER BY at, state, reason, score")]
    return alerts, candidates_rows, decisions


def _play(settings):
    store = Store(":memory:")
    engine = Engine(store, settings)
    alerts = []
    for frame in demo_frames():
        alerts.extend(engine.process(frame))
    return store, alerts


class WaveShadowTests(unittest.TestCase):
    def test_flags_are_integers_and_default_off(self):
        # bool("false") is True, so a bool env flag cannot be turned off.
        self.assertTrue(bool("false"))
        self.assertEqual(int("0"), 0)
        self.assertEqual(int("1"), 1)
        with self.assertRaises(ValueError):
            int("false")
        settings = Settings()
        for name in ("wave_shadow", "wave_card_shadow", "wave_f4_shadow", "wave_f7_shadow",
                     "wave_f1_shadow", "wave_f2_shadow", "wave_reject_shadow"):
            self.assertIs(type(getattr(settings, name)), int)
            self.assertEqual(getattr(settings, name), 0)
        self.assertEqual(settings.wave_cooldown_minutes, 15)
        environ = {key: value for key, value in os.environ.items() if not key.startswith("LOTTO_")}
        with patch.dict(os.environ, {**environ, "LOTTO_WAVE_SHADOW": "0"}, clear=True):
            self.assertEqual(env_settings().wave_shadow, 0)
        with patch.dict(os.environ, {**environ, "LOTTO_WAVE_SHADOW": "1", "LOTTO_WAVE_F4_SHADOW": "1"}, clear=True):
            loaded = env_settings()
            self.assertEqual((loaded.wave_shadow, loaded.wave_f4_shadow), (1, 1))

    def test_snapshot_fields_round_trip_and_stay_out_of_old_payloads(self):
        at = datetime(2026, 11, 3, 9, 31, tzinfo=ET)
        bar = Bar(at, 100, 101, 99, 100.5, 1000, 1000)
        plain = Snapshot("TEST", at, 100.5, at, 100, (bar,), ())
        payload = plain.to_dict()
        self.assertNotIn("prior_atr21", payload)
        self.assertNotIn("prior_high_20d", payload)
        self.assertEqual(Snapshot.from_dict(payload), plain)
        richer = replace(plain, prior_atr21=2.5, prior_high_20d=110.0, prior_low_20d=90.0)
        self.assertEqual(Snapshot.from_dict(richer.to_dict()), richer)
        self.assertFalse(broke_20d_range(plain, 1))
        self.assertFalse(broke_20d_range(replace(plain, prior_high_20d=110), 1))
        self.assertTrue(broke_20d_range(replace(plain, prior_high_20d=100), 1))
        self.assertTrue(broke_20d_range(replace(plain, prior_low_20d=99.5), -1))

    def test_detect_setup_ignores_shadow_fields(self):
        frames = list(demo_frames())
        snap = frames[20][0]
        enriched = replace(snap, prior_atr21=9, prior_high_20d=1, prior_low_20d=1)
        for direction in (1, -1):
            base = path_features(snap, direction)
            other = path_features(enriched, direction)
            self.assertEqual(base, other)
            self.assertEqual(detect_setup(snap, direction, base), detect_setup(enriched, direction, other))
            self.assertEqual(
                [(item.option.symbol, item.score, item.qualifying, item.blockers)
                 for item in candidates(snap, [frames[i][0] for i in range(15, 20)], Settings())],
                [(item.option.symbol, item.score, item.qualifying, item.blockers)
                 for item in candidates(enriched, [frames[i][0] for i in range(15, 20)], Settings(wave_shadow=1))])

    def test_replay_cards_match_with_shadow_off_and_on(self):
        quiet, quiet_alerts = _play(Settings())
        loud, loud_alerts = _play(Settings(wave_shadow=1))
        partial, partial_alerts = _play(Settings(
            wave_card_shadow=1, wave_f4_shadow=1, wave_f7_shadow=1,
            wave_f1_shadow=1, wave_f2_shadow=1))
        reject_only, reject_alerts = _play(Settings(wave_reject_shadow=1))
        self.assertEqual(_dump(quiet), _dump(loud))
        self.assertEqual(_dump(quiet), _dump(partial))
        self.assertEqual(_dump(quiet), _dump(reject_only))
        self.assertEqual(quiet_alerts, loud_alerts)
        self.assertEqual(quiet_alerts, partial_alerts)
        self.assertEqual(quiet_alerts, reject_alerts)
        self.assertTrue(quiet_alerts)
        self.assertEqual(quiet.db.execute("SELECT COUNT(*) FROM wave_shadow").fetchone()[0], 0)
        families = {row[0] for row in loud.db.execute("SELECT family FROM wave_shadow")}
        self.assertIn("CARD", families)
        self.assertIn("REJECT", families)
        self.assertTrue(families <= {"CARD", "CARD_PULLIN", "CARD_REJECT", "REJECT"})
        reject_families = {row[0] for row in reject_only.db.execute("SELECT family FROM wave_shadow")}
        self.assertEqual(reject_families, {"REJECT"})
        posted = {(row["symbol"], row["bar_end"]) for row in loud.db.execute(
            "SELECT symbol, bar_end FROM wave_shadow WHERE family='CARD'")}
        rejected = {(row["symbol"], row["bar_end"]) for row in loud.db.execute(
            "SELECT symbol, bar_end FROM wave_shadow WHERE family='REJECT'")}
        self.assertFalse(posted & rejected)
        for alert in loud_alerts:
            self.assertNotIn("WAVE_", json.dumps(alert["payload"]))
        # Stored evaluations keep the pre-shadow settings keys.
        features = json.loads(quiet.db.execute("SELECT features FROM candidate_evaluations LIMIT 1").fetchone()[0])
        self.assertTrue(all(not key.startswith("wave_") for key in features["settings"]))
        card = json.loads(loud.db.execute(
            "SELECT features FROM wave_shadow WHERE family='CARD'").fetchone()[0])
        self.assertEqual(card["v3"], 1)
        self.assertEqual(card["h2"], 1)
        self.assertIsNone(card["v6"])
        self.assertIn("day", card)
        self.assertIn("spy_hod", card)
        self.assertIn("rs", card)

    def test_v6_tag_does_not_block_the_card(self):
        frames = list(demo_frames())

        def broke(snap):
            return replace(snap, prior_high_20d=50, prior_low_20d=1, prior_atr21=5)

        def run(flag):
            store = Store(":memory:")
            engine = Engine(store, Settings(wave_shadow=flag))
            alerts = []
            for frame in frames:
                alerts.extend(engine.process([broke(snap) for snap in frame]))
            return store, alerts

        off, off_alerts = run(0)
        on, on_alerts = run(1)
        self.assertEqual(off_alerts, on_alerts)
        self.assertTrue(on_alerts)
        self.assertEqual(_dump(off)[0], _dump(on)[0])
        row = on.db.execute(
            "SELECT features, card_alert_id FROM wave_shadow WHERE family='CARD'").fetchone()
        card = json.loads(row["features"])
        self.assertEqual(card["v6"], 1)
        self.assertEqual(row["card_alert_id"], on_alerts[0]["id"])
        self.assertEqual(off.db.execute("SELECT COUNT(*) FROM wave_shadow").fetchone()[0], 0)

    def test_f4_fires_on_the_energy_flag_and_not_without_volume_or_atr21(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        bars = _bars(start, _f4_rows())
        snap = _snap(bars, options=(_option(bars[-1].end, delta=0.41),))
        found = detect_f4(snap, 1)
        self.assertIsNotNone(found)
        self.assertEqual(found["family"], "F4")
        self.assertGreater(found["setup"].trigger, 105.5)
        self.assertEqual(found["features"]["k"], 2)
        self.assertLessEqual(found["features"]["touches"], 2)
        self.assertEqual(found["features"]["bv"], 1)
        mirrored = tuple(_mirror_bar(bar) for bar in bars)
        put = detect_f4(_snap(mirrored, spot=mirrored[-1].close), -1)
        self.assertIsNotNone(put)
        self.assertAlmostEqual(put["setup"].trigger, 300 - found["setup"].trigger)
        rows = _f4_rows()
        rows[-5:] = [(104.5, 107.0, 104.4, 106.5, 50)] * 5
        self.assertIsNone(detect_f4(_snap(_bars(start, rows)), 1))
        self.assertIsNone(detect_f4(_snap(bars, prior_atr21=None, prior_atr=10), 1))
        events, blocker = detect_signals(_snap(bars, prior_atr21=None), 1, Settings(wave_shadow=1))
        self.assertEqual(events, [])
        self.assertEqual(blocker, "wave: atr21 unavailable")
        late = _bars(start + timedelta(minutes=1), _f4_rows())
        self.assertEqual(aggregate(late, 5), ())
        self.assertEqual(detect_signals(_snap(late), 1, Settings(wave_shadow=1))[1], "wave: session not anchored")

    def test_f4_shadow_row_is_not_posted(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        bars = _bars(start, _f4_rows())
        snap = _snap(bars, options=(_option(bars[-1].end, delta=0.42, bid=0.50, ask=0.55),))
        store = Store(":memory:")
        engine = Engine(store, Settings(wave_shadow=1))
        self.assertEqual(engine.process([snap]), [])
        rows = store.db.execute("SELECT family, contract, ask, card_alert_id FROM wave_shadow").fetchall()
        self.assertEqual([row["family"] for row in rows], ["F4"])
        self.assertEqual(rows[0]["contract"], "TEST110C")
        self.assertAlmostEqual(rows[0]["ask"], 0.55)
        self.assertIsNone(rows[0]["card_alert_id"])
        self.assertEqual(store.summary(), [])
        dark = Store(":memory:")
        self.assertEqual(Engine(dark, Settings()).process([snap]), [])
        self.assertEqual(dark.db.execute("SELECT COUNT(*) FROM wave_shadow").fetchone()[0], 0)

    def test_shadow_cooldown_keeps_a_second_fire_out_of_the_table(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        bars = _bars(start, _f4_rows())
        snap = _snap(bars, options=(_option(bars[-1].end),))
        store = Store(":memory:")
        earlier = (snap.at - timedelta(minutes=5)).isoformat()
        store.wave_shadow({
            "day": snap.day, "symbol": snap.symbol, "at": earlier, "family": "F4", "side": "CALL",
            "bar_end": earlier, "trigger": 1, "invalidation": 1, "spot": 1, "atr21": 10,
            "contract": None, "ask": None, "bid": None, "delta": None,
            "card_alert_id": None, "features": {},
        })
        self.assertEqual(Engine(store, Settings(wave_shadow=1)).process([snap]), [])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM wave_shadow").fetchone()[0], 1)

    def test_f1_reclaim_and_f2_failure(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        rows = _repeat((100, 100, 100, 100, 100)) + _repeat((100, 102, 100, 102, 100))
        bars = _bars(start, rows, volume=100)
        reclaim = detect_f1(_snap(bars), 1)
        self.assertIsNotNone(reclaim)
        self.assertEqual(reclaim["family"], "F1")
        self.assertGreater(reclaim["setup"].trigger, 100)
        self.assertIsNone(detect_f1(_snap(bars), -1))
        failure = _repeat((100, 105, 95, 100, 100)) + _repeat((100, 106, 94, 100, 100))
        failed = _bars(start, failure, volume=100)
        self.assertEqual(detect_f2(_snap(failed), -1)["family"], "F2")
        self.assertEqual(detect_f2(_snap(failed), 1)["family"], "F2")
        self.assertAlmostEqual(detect_f2(_snap(failed), -1)["setup"].trigger, 105)
        self.assertAlmostEqual(detect_f2(_snap(failed), 1)["setup"].trigger, 95)

    def test_f7_rejection_and_base_break_are_shadow_only(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        bars = _bars(start, _f7_rows(), volume=100)
        snap = _snap(bars, spot=bars[-1].close, prior_atr21=10, prior_atr=10)
        found = {item["family"]: item for item in detect_f7(snap, 1)}
        self.assertIn("F7A", found)
        self.assertEqual(found["F7A"]["features"]["reg"], 1)
        self.assertIn("F7B", found)
        store = Store(":memory:")
        self.assertEqual(Engine(store, Settings(wave_f7_shadow=1)).process([snap]), [])
        families = sorted(row[0] for row in store.db.execute("SELECT family FROM wave_shadow"))
        self.assertIn("F7A", families)
        self.assertNotIn("CARD", families)

    def test_entry_path_records_pullin_reject_and_extremes(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        flat = [(100.0, 100.2, 100.05, 100.1, 100) for _ in range(30)]
        # Shallow hold of the 2m 9 EMA, then a later rejection wick.
        pull = [(100.0, 100.12, 100.08, 100.10, 100), (100.10, 100.14, 100.09, 100.12, 100)]
        reject = [(100.0, 100.7, 99.6, 100.55, 100), (100.4, 100.8, 100.2, 100.7, 100)]
        bars = _bars(start, flat)
        card_at = bars[-1].end
        option = _option(card_at, delta=0.40, bid=0.80, ask=0.90)
        snap = _snap(bars, options=(option,), prior_atr=2, prior_atr21=2, spot=100.1)
        later_bars = _bars(start, flat + pull + reject)
        later_at = later_bars[-1].end
        later_option = _option(later_at, delta=0.40, bid=0.55, ask=1.20)
        later = _snap(later_bars, options=(later_option,), prior_atr=2, prior_atr21=2, spot=100.5)
        pull_hit, reject_hit = checkpoints(later.bars, card_at, 1, 2, card_at + timedelta(minutes=30))
        self.assertIsNotNone(pull_hit)
        self.assertIsNotNone(reject_hit)
        self.assertLess(pull_hit["bar"].end, reject_hit["bar"].end)
        store = Store(":memory:")
        memory = {"symbols": {}, "skips": set(), "purged": None, "cap_warned": False}
        from lotto.discord import alert_payload
        from lotto.features import Candidate
        from lotto.patterns import Setup
        candidate = Candidate(snap, option, 88, {}, {}, ["test"], True,
                              setup=Setup("CONTINUATION", 100.2, 99.5))
        payload = alert_payload(candidate, "ACTIVE")
        ident = store.queue(candidate, payload, True, "ACTIVE")
        run_shadow(store, Settings(wave_card_shadow=1), [snap], [(candidate, ident)], [snap], memory)
        run_shadow(store, Settings(wave_card_shadow=1), [later], [], [later], memory)
        families = {row["family"] for row in store.db.execute("SELECT family FROM wave_shadow")}
        self.assertEqual(families, {"CARD", "CARD_PULLIN", "CARD_REJECT"})
        features = json.loads(store.db.execute(
            "SELECT features FROM wave_shadow WHERE family='CARD'").fetchone()[0])
        self.assertAlmostEqual(features["min_bid"], 0.55)
        self.assertAlmostEqual(features["max_ask"], 1.20)
        self.assertEqual(store.summary()[0]["id"], ident)
        self.assertEqual(len(store.summary()), 1)

    def test_card_tags_use_spy_and_fail_open_without_20d(self):
        start = datetime(2026, 11, 3, 9, 30, tzinfo=ET)
        bars = _bars(start, [(100, 101, 99.8, 100.8, 100) for _ in range(12)])
        snap = _snap(bars, spot=100.9, prior_atr=2, premarket_high=101)
        spy_bars = _bars(start, [(50, 50.2, 49.9, 50.15, 100) for _ in range(12)])
        spy = Snapshot("SPY", spy_bars[-1].end, 50.15, spy_bars[-1].end, 50, spy_bars, (), prior_atr=1)
        tags = card_tags(snap, 1, spy)
        self.assertIsNone(tags["v6"])
        self.assertEqual(tags["h2"], 1)
        self.assertIsNotNone(tags["rs"])
        self.assertIsNotNone(tags["spy_hod"])
        self.assertIn(tags["spy_010"], (0, 1))
        tagged = replace(snap, prior_high_20d=101)
        self.assertEqual(card_tags(tagged, 1, spy)["v6"], 0)
        self.assertEqual(card_tags(replace(snap, prior_high_20d=100), 1, spy)["v6"], 1)

    def _reject_candidate(self, at, blockers, score=64):
        bar = Bar(at, 100, 101, 99, 100.4, 1000, 5000)
        option = _option(at, delta=0.4, bid=0.4, ask=0.45)
        snap = Snapshot("MSFT", at, 100.4, at, 90, (bar,), (option,), prior_atr=2)
        metrics = {
            "pace_rvol": 2.4, "pace_persistence": 0.91, "option_acceleration": 1.05,
            "options_persistent_20m": 0, "cluster_size": 3, "context_directional_return": -0.0008,
            "relative_sector_5m": 0.0004, "peer_directional_return": 0.0,
            "relative_market_5m": -0.001, "trigger_gap_atr": 0.42,
            "opposing_volume_share": 0.18, "options_window_minutes": 5,
        }
        return Candidate(snap, option, score, {}, metrics, [], False,
                         setup=Setup("CONTINUATION", 101, 99), blockers=tuple(blockers))

    def test_reject_rows_keep_one_streak_per_ticker_and_skip_the_volume_gate(self):
        start = datetime(2026, 11, 3, 14, 0, tzinfo=ET)
        blocked = ("options activity not replenishing", "independent sector/peer confirmation missing")
        gate = self._reject_candidate(start, ("stock volume pace/burst below threshold",) + blocked)
        self.assertFalse(volume_passed(gate))
        first = self._reject_candidate(start, blocked)
        second = self._reject_candidate(start + timedelta(minutes=1), blocked, score=66)
        third = self._reject_candidate(start + timedelta(minutes=2), ("score below threshold",), score=61)
        store = Store(":memory:")
        memory = {"cap_warned": False}
        settings = Settings(wave_reject_shadow=1)
        log_rejects(store, settings, [{"candidate": gate, "stage": None, "extra": {}}], memory)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM wave_shadow").fetchone()[0], 0)
        for candidate in (first, second, third):
            log_rejects(store, settings, [{"candidate": candidate, "stage": None, "extra": {}}], memory)
        rows = store.db.execute(
            "SELECT bar_end, features FROM wave_shadow WHERE family='REJECT' ORDER BY bar_end").fetchall()
        self.assertEqual(len(rows), 2)
        streak = json.loads(rows[0]["features"])
        self.assertEqual(streak["why"], "options|confirm")
        self.assertEqual(streak["stage"], "options")
        self.assertEqual(streak["n"], 2)
        self.assertEqual(streak["score"], 66)
        self.assertEqual(streak["need"], 70)
        self.assertEqual(streak["acc"], 1.05)
        self.assertAlmostEqual(streak["gap"], 0.42)
        self.assertEqual(streak["flow"], 0)
        self.assertIn("mkt", streak)
        changed = json.loads(rows[1]["features"])
        self.assertEqual(changed["stage"], "score")
        self.assertEqual(changed["n"], 1)
        self.assertEqual(changed["score"], 61)

    def test_contract_prefers_the_35_to_45_delta_band(self):
        at = datetime(2026, 11, 3, 10, 0, tzinfo=ET)
        bars = _bars(datetime(2026, 11, 3, 9, 30, tzinfo=ET), [(100, 101, 99, 100, 100)])
        options = (
            _option(at, delta=0.20, strike=120),
            _option(at, delta=0.42, strike=105),
            _option(at, delta=0.55, strike=100),
        )
        snap = _snap(bars, options=options, spot=100)
        self.assertEqual(pick_contract(snap, 1, 10).symbol, "TEST105C")

    def test_retention_cap_and_csv_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "live.sqlite3")
            store = Store(path)
            today = "2026-11-03"
            old = "2026-09-01"
            base = dict(symbol="TEST", at=today + "T14:00:00+00:00", side="CALL",
                        bar_end=today + "T14:00:00+00:00", trigger=1, invalidation=1,
                        spot=1, atr21=1, contract="C", ask=0.4, bid=0.3, delta=0.4,
                        card_alert_id=None, features={"k": 1})
            self.assertEqual(store.wave_shadow({**base, "day": old, "family": "F4"}), "inserted")
            self.assertEqual(store.wave_shadow({**base, "day": today, "family": "F1"}), "inserted")
            self.assertEqual(store.wave_shadow({**base, "day": today, "family": "F1"}), "duplicate")
            self.assertEqual(store.purge_wave_shadow(today, keep_days=30), 1)
            self.assertEqual(
                [row[0] for row in store.db.execute("SELECT day FROM wave_shadow")], [today])
            self.assertEqual(store.wave_shadow({**base, "day": today, "family": "F2"}, cap=1), "capped")
            store.db.close()
            destination = str(Path(directory) / "wave.csv")
            self.assertEqual(export_wave_shadow(path, today, destination), 1)
            self.assertEqual(export_wave_shadow(path, today, str(Path(directory) / "f1.csv"), family="F1"), 1)
            self.assertEqual(export_wave_shadow(path, today, str(Path(directory) / "rejects.csv"), family="REJECT"), 0)
            with open(destination, newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["family"], "F1")
            self.assertEqual(rows[0]["symbol"], "TEST")

    def test_baseline_adds_atr21_and_20d_without_replacing_atr14(self):
        client = Schwab.__new__(Schwab)
        client.baselines = {}
        today = datetime(2026, 11, 3, 10, 0, tzinfo=ET)
        candles = []
        for age in range(1, 26):
            start = (today - timedelta(days=age)).replace(hour=9, minute=30, second=0, microsecond=0)
            high = 99 if age == 25 else 11
            candles.append({"datetime": start.timestamp() * 1000, "open": 10, "high": high,
                            "low": 9, "close": 10, "volume": 1000})
        client.candles = Mock(return_value={"candles": candles})
        client.baseline("TEST", today)
        key = ("TEST", today.date())
        self.assertEqual(client.atrs[key], 2)
        self.assertEqual(client.atrs21[key], 2)
        self.assertEqual(client.levels[key]["prior_high_20d"], 11)
        self.assertEqual(client.levels[key]["prior_low_20d"], 9)
        short = Schwab.__new__(Schwab)
        short.baselines = {}
        short.candles = Mock(return_value={"candles": candles[-15:]})
        short.baseline("TEST", today)
        self.assertIsNotNone(short.atrs[key])
        self.assertIsNone(short.atrs21[key])
        self.assertNotIn("prior_high_20d", short.levels.get(key, {}))


def _f7_rows():
    """Flat tape with HOD at least 3 points over VWAP, then a tight EMA backtest.

    ATR21 is 10. The last two minutes are the rejection wick and the base-high break.
    """
    rows = []
    for minute in range(24):
        if minute == 12:
            rows.append((110, 114, 109.8, 110.2, 100))
        else:
            rows.append((110, 110.4, 109.7, 110.1, 100))
    for minute in range(16):
        if 10 <= minute <= 14:
            rows.append((110.4, 111.2, 110.15, 110.8, 100))
        else:
            rows.append((110.6, 111.4, 110.5, 111.0, 100))
    rows.append((110.8, 112.4, 110.2, 111.6, 100))
    rows.append((111.6, 112.8, 111.0, 112.4, 100))
    return rows


if __name__ == "__main__":
    unittest.main()
