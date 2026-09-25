import time
import unittest

from beacon_station.web import Store, history, reduce_series, wind_rose


class ReduceTests(unittest.TestCase):
    def test_raw_gap_inserts_break(self):
        ts = [0, 10, 20, 200, 210]
        rows = [(1,)] * 5
        t, r = reduce_series(ts, rows, 0, 210, 100, 60, lambda x: x[0])
        self.assertEqual(r.count(None), 1)
        self.assertEqual(len(t), 6)

    def test_binning_does_not_fragment_sparse_series(self):
        # 10 s cadence, bins narrower than cadence: no spurious breaks.
        ts = list(range(0, 3600, 10))
        rows = [(1.0,)] * len(ts)
        t, r = reduce_series(ts, rows, 0, 3600, 200, 60, lambda x: x[0])
        self.assertNotIn(None, r)
        self.assertLessEqual(len(t), 201)


class StoreTests(unittest.TestCase):
    def test_dedupe_and_queries(self):
        s = Store(3600)
        now = time.time()
        for i in range(120):
            s.add_sample({"source": "anemo", "t": now - 119 + i,
                          "wind_ms": 2.0 if i % 2 else 0.0, "wind_deg": 90})
        s.add_sample({"source": "anemo", "t": now, "wind_ms": 2.0,
                      "wind_deg": 90})             # duplicate
        s.add_sample({"source": "beacon", "t": now, "TMP119_C": "21.5",
                      "RH_pct": ""})
        s.add_sample({"source": "event", "t": now, "note": "x"})
        self.assertEqual(len(s.t["anemo"]), 120)
        h = history(s, 600, 600)
        self.assertEqual(h["beacon"]["TMP119_C"], [21.5])
        self.assertEqual(h["beacon"]["RH_pct"], [None])
        self.assertEqual(h["events"][0]["note"], "x")
        rose = wind_rose(s, 600)
        self.assertEqual(rose["total"], 120)
        self.assertEqual(rose["calm"], 60)
        self.assertAlmostEqual(rose["prevailing"], 90)


if __name__ == "__main__":
    unittest.main()
