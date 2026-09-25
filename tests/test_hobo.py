"""HOBO MX2309 advertisement decoding, against payloads captured with btmon
from a real logger (indoors, ~24 degC, pyranometer covered mid-capture)."""

import queue
import unittest

from beacon_station.hobo import HoboReader, decode, encode_a, encode_b

# (payload hex, times seen in a 2-minute capture)
CAPTURED = [
    ("0d9c9258010b23800052fa002341c0200000424264", 4),
    ("0d9c9258010b23800052fa002341c0b00000424265", 8),
    ("0d9c9258010b23800052fa002341c1400000424262", 4),
    ("0d9c9258010b23820052fa002341c0200000424264", 2),
    ("2ef4010c3faf499a01103c04e6e1010d3fa3badae1c1", 2),
    ("6906010c3f0e200001103c054720010d3fa33f6dfb57", 4),
    ("cf63010c3e4a222101103c054a3b010d3fa6700fdb6c", 4),
]


class DecodeTests(unittest.TestCase):
    def test_kind_a(self):
        kind, f = decode(bytes.fromhex(CAPTURED[0][0]))
        self.assertEqual(kind, "A")
        self.assertEqual(f["hobo_serial"], 228364888)
        self.assertAlmostEqual(f["hobo_T_C"], 24.0156, places=3)
        self.assertAlmostEqual(f["hobo_RH_pct"], 48.598, places=2)

    def test_kind_b(self):
        kind, f = decode(bytes.fromhex(CAPTURED[4][0]))
        self.assertEqual(kind, "B")
        self.assertAlmostEqual(f["solar_Wm2"], 1.3694, places=3)
        self.assertAlmostEqual(f["solar_accum_MJm2"], 0.008112, places=5)
        self.assertAlmostEqual(f["hobo_ch0d"], 1.2791, places=3)

    def test_covered_sensor_reads_lower(self):
        uncovered = decode(bytes.fromhex(CAPTURED[4][0]))[1]["solar_Wm2"]
        covered = decode(bytes.fromhex(CAPTURED[6][0]))[1]["solar_Wm2"]
        self.assertLess(covered, uncovered)

    def test_all_captured_decode(self):
        for h, _ in CAPTURED:
            kind, f = decode(bytes.fromhex(h))
            self.assertIn(kind, ("A", "B"), h)
            self.assertTrue(any(v is not None for v in f.values()), h)

    def test_unknown_layout(self):
        self.assertEqual(decode(b"\x00" * 7), (None, {}))

    def test_encoders_round_trip(self):
        self.assertAlmostEqual(decode(encode_a(1, 30.5, 61.2))[1]["hobo_T_C"], 30.5)
        f = decode(encode_b(812.5, 1.25, 1.3))[1]
        self.assertAlmostEqual(f["solar_Wm2"], 812.5)
        self.assertAlmostEqual(f["solar_accum_MJm2"], 1.25)


class ReaderTests(unittest.TestCase):
    def _reader(self, **cfg):
        q = queue.Queue()
        return HoboReader({"heartbeat_s": 60, **cfg}, q), q

    def _samples(self, q):
        out = []
        while not q.empty():
            item = q.get()
            if item[0] == "sample":
                out.append(item[1])
        return out

    def test_emits_on_change_only(self):
        r, q = self._reader()
        a = bytes.fromhex(CAPTURED[0][0])
        for _ in range(5):
            r.on_advert("f8:27:3e:19:81:a5", a)     # repeats: one row
        r.on_advert("F8:27:3E:19:81:A5", bytes.fromhex(CAPTURED[1][0]))
        r.on_advert("F8:27:3E:19:81:A5", bytes.fromhex(CAPTURED[4][0]))
        rows = self._samples(q)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["hobo_raw"], CAPTURED[0][0])
        self.assertEqual(rows[0]["hobo_addr"], "F8:27:3E:19:81:A5")
        self.assertNotIn("hobo_serial", rows[0])
        self.assertEqual(r.state["serial"], 228364888)
        self.assertIsNotNone(r.state["last_seen"])

    def test_heartbeat_repeats_unchanged(self):
        r, q = self._reader(heartbeat_s=0)
        a = bytes.fromhex(CAPTURED[0][0])
        r.on_advert("F8:27:3E:19:81:A5", a)
        r.on_advert("F8:27:3E:19:81:A5", a)
        self.assertEqual(len(self._samples(q)), 2)

    def test_address_filter(self):
        r, q = self._reader(address="aa:bb:cc:dd:ee:ff")
        r.on_advert("F8:27:3E:19:81:A5", bytes.fromhex(CAPTURED[0][0]))
        self.assertEqual(self._samples(q), [])
        self.assertIsNone(r.state["last_seen"])


class ScanPathTests(unittest.TestCase):
    """run() with a stand-in BleakScanner: the callback wiring, filtering by
    company ID, and the connected state — everything except the radio."""

    def test_run_with_fake_scanner(self):
        import sys
        import time
        import types

        class Dev:
            address = "f8:27:3e:19:81:a5"

        class Adv:
            def __init__(self, md):
                self.manufacturer_data = md

        class FakeScanner:
            def __init__(self, detection_callback=None, **kw):
                self.cb = detection_callback

            async def __aenter__(self):
                self.cb(Dev(), Adv({0x004C: b"\x01\x02"}))       # Apple: ignored
                self.cb(Dev(), Adv({0x00C5: bytes.fromhex(CAPTURED[4][0])}))
                return self

            async def __aexit__(self, *exc):
                return False

        fake = types.ModuleType("bleak")
        fake.BleakScanner = FakeScanner
        saved = sys.modules.get("bleak")
        sys.modules["bleak"] = fake
        try:
            q = queue.Queue()
            r = HoboReader({"heartbeat_s": 60}, q)
            r.start()
            item = None
            while item is None or item[0] != "sample":
                item = q.get(timeout=5)
            self.assertEqual(item[1]["hobo_raw"], CAPTURED[4][0])
            # The adverts fire inside __aenter__, before run() marks the
            # scan as connected, so wait for that rather than race it.
            deadline = time.monotonic() + 5
            while r.state["state"] != "connected" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(r.state["state"], "connected")
            r.stop()
            r.join(timeout=5)
            self.assertFalse(r.is_alive())
        finally:
            if saved is None:
                del sys.modules["bleak"]
            else:
                sys.modules["bleak"] = saved


if __name__ == "__main__":
    unittest.main()
