import unittest
import base64
import gzip
import hashlib
import json
from datetime import datetime

from lotto.audit import diagnose_symbols
from lotto.models import ET
from lotto.store import Store
from lotto.forensics import export_case


class TargetedAuditTests(unittest.TestCase):
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
