import unittest
import base64
import gzip
import hashlib
import json
from datetime import datetime, timezone

from lotto.audit import diagnose_symbols, audit_coil_burst_hypothesis
from lotto.models import ET, Option, Snapshot
from lotto.store import Store
from lotto.forensics import export_case, export_window_closed


class TargetedAuditTests(unittest.TestCase):
    def test_coil_hypothesis_audit_uses_saved_blockers_and_short_expiry(self):
        import tempfile
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        at = datetime(2026,9,29,13,50,tzinfo=ET).astimezone(timezone.utc)
        features = {"setup":{"name":"COILED_CONTINUATION"},
                    "blockers":["options activity not replenishing"],
                    "contract":"AAPL  260930P00330000",
                    "metrics":{"option_acceleration":2.74,"local_rvol_5m":2.44,
                               "volume_acceleration":1.5,"option_volume_5m":2933}}
        store.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                         ("AAPL",at.isoformat(),"DISCOVERY","options activity not replenishing",74.58,json.dumps(features)))
        with tempfile.TemporaryDirectory() as directory:
            result = audit_coil_burst_hypothesis(store,"2026-09-29",directory)
        self.assertEqual(result["short_dte_strong_burst_observations"],1)
        self.assertEqual(result["independent_symbol_contract_cases"][0]["dte"],1)

    def test_forensic_export_allows_same_day_after_close_only(self):
        from datetime import timedelta
        opening = datetime(2026,9,29,9,30,tzinfo=ET)
        closing = datetime(2026,9,29,16,0,tzinfo=ET)
        session = (opening, closing)
        self.assertFalse(export_window_closed("2026-09-29", opening-timedelta(minutes=1), session))
        self.assertFalse(export_window_closed("2026-09-29", opening+timedelta(minutes=1), session))
        self.assertTrue(export_window_closed("2026-09-29", closing, session))
        self.assertFalse(export_window_closed("2026-09-30", closing, session))
        self.assertTrue(export_window_closed("2026-09-28", opening+timedelta(minutes=1), None))

    def test_distinguishes_stock_discovery_from_chain_and_deep_checks(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        at = datetime(2026, 9, 25, 10, 0, tzinfo=ET)
        store.record_discovery([{"symbol":"AAPL", "observed_at":at, "promoted":False,
                                 "priority":4.2, "return_5m":.01}])
        store.record_chain_coverage([{"symbol":"AAPL", "at":at, "promoted":False,
                                      "contracts":50, "spot":250.0, "flow_side":None,
                                      "flow_cluster":0}])
        result = diagnose_symbols(store, "2026-09-25", ["AAPL"])["AAPL"]
        self.assertEqual(result["stock_quote_observations"], 1)
        self.assertEqual(result["chain_observations"], 1)
        self.assertEqual(result["deep_promotions"], 0)
        self.assertEqual(result["scored_decisions"], 0)

    def test_case_export_is_bounded_and_round_trips(self):
        from datetime import timezone
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        for minute in (31,32,33):
            at = datetime(2026,9,25,10,minute,tzinfo=ET).astimezone(timezone.utc)
            store.record_discovery([{"symbol":"AAPL","observed_at":at,"promoted":True}])
        packed, chunks = export_case(store,"2026-09-25","AAPL","10:30","10:32")
        self.assertEqual(base64.b64decode("".join(c["data"] for c in chunks)),packed)
        self.assertEqual(chunks[0]["sha256"],hashlib.sha256(packed).hexdigest())
        case = json.loads(gzip.decompress(packed))
        self.assertEqual(case["record_counts"]["discovery"],2)

    def test_case_export_filters_saved_option_quotes_without_changing_decisions(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        from datetime import timezone
        at = datetime(2026,9,29,14,0,tzinfo=ET).astimezone(timezone.utc)
        options = (Option("SPY260929C00764000",at.date(),"CALL",764,.50,.51,200,100,.25,.02,at),
                   Option("SPY260929C00765000",at.date(),"CALL",765,.20,.21,200,100,.15,.02,at))
        store.record(Snapshot("SPY",at,765,at,764,(),options))
        packed, _ = export_case(store,"2026-09-29","SPY","13:59","14:01",
                                contract_expiry="2026-09-29",strike_low=764,strike_high=764)
        case = json.loads(gzip.decompress(packed))
        self.assertEqual([o["strike"] for o in case["records"]["snapshots"][0]["payload"]["options"]], [764])
        self.assertEqual(case["option_filter"],{"expiry":"2026-09-29","strike_low":764,"strike_high":764})
