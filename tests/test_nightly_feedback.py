import json
import unittest
from datetime import datetime, timedelta

from lotto.models import ET, Option, Snapshot
from lotto.nightly import alert_feedback, phase_opportunity_summary
from lotto.store import Store


class NightlyFeedbackTests(unittest.TestCase):
    def test_spxw_opportunities_are_reported_separately(self):
        alerts = [
            {"phase":"ACTIVE", "contract":"SPXW  261008C07800000", "delivery":"sent"},
            {"phase":"ACTIVE", "contract":"AAPL  261008C00340000", "delivery":"sent"},
        ]
        rows = [
            {**alerts[0], "opportunity_label":"Strong", "setup":"OPENING_DRIVE"},
            {**alerts[1], "opportunity_label":"Flat/failed", "setup":"OPENING_DRIVE"},
        ]
        spxw = phase_opportunity_summary(alerts, rows, "ACTIVE", "SPXW")
        other = phase_opportunity_summary(alerts, rows, "ACTIVE", "OTHER")
        self.assertEqual(spxw["opportunity_labels"], {"Strong":1})
        self.assertEqual(other["opportunity_labels"], {"Flat/failed":1})
        self.assertEqual(phase_opportunity_summary(alerts, rows, "ACTIVE")["alert_count"], 2)

    def test_compares_first_qualification_with_alert_and_future_bid(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        start = datetime(2026, 10, 1, 13, 25, tzinfo=ET)
        symbol = "IWM   261001C00280000"
        def sample(minute, bid, ask):
            at = start + timedelta(minutes=minute)
            option = Option(symbol, at.date(), "CALL", 280, bid, ask, 1000+minute*100,
                            100, .4, .1, at)
            return Snapshot("IWM", at, 279.7+minute*.1, at, 277, (), (option,),
                            prior_atr=3.5)
        first, alert, peak, last = (sample(*args) for args in
                                    ((0, .34, .35), (2, .44, .45), (7, .81, .82), (60, .01, .02)))
        for snap in (first, alert, peak, last):
            store.record(snap)
        initial = {"contract":symbol, "entry_ask":.35, "blockers":[],
                   "spot":first.spot, "setup":{"name":"COILED_CONTINUATION", "trigger":280}}
        final = {**initial, "entry_ask":.45, "spot":alert.spot}
        with store.db:
            store.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                             ("IWM",first.at.isoformat(),"IGNITION","confirming persistence",72.8,json.dumps(initial)))
            store.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                             ("IWM",alert.at.isoformat(),"IGNITION","potential alert test",72.3,json.dumps(final)))
            store.db.execute("INSERT INTO alerts (id,day,symbol,side,contract,at,entry_ask,snapshot,delivery) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             ("test",alert.day,"IWM","CALL",symbol,alert.at.isoformat(),.45,
                              json.dumps(alert.to_dict()),"sent"))
        row = alert_feedback(store, alert.day)[0]
        self.assertAlmostEqual(row["confirmation_premium_change"], .45/.35-1)
        self.assertAlmostEqual(row["peak_ask_to_bid_return"], .81/.45-1)
        self.assertAlmostEqual(row["last_ask_to_bid_return"], .01/.45-1)
        self.assertAlmostEqual(row["trigger_gap_atr"], abs(alert.spot-280)/3.5)
        self.assertEqual(row["minutes_to_peak"], 5)
        self.assertEqual(row["score"], 72.3)


if __name__ == "__main__":
    unittest.main()
