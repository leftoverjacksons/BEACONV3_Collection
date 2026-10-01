import unittest

from beacon_station.moisture import LevelTracker, assess, dew_point, vapor_pressure


class DewPointTests(unittest.TestCase):
    def test_reference_values(self):
        # Psychrometric tables: 20 C / 50 % -> 9.3 C; 30 C / 80 % -> 26.2 C
        self.assertAlmostEqual(dew_point(20.0, 50.0), 9.27, delta=0.1)
        self.assertAlmostEqual(dew_point(30.0, 80.0), 26.17, delta=0.1)
        # Saturated air: dew point equals air temperature.
        self.assertAlmostEqual(dew_point(15.0, 100.0), 15.0, places=6)
        # Saturation vapour pressure at 20 C is about 23.4 hPa.
        self.assertAlmostEqual(vapor_pressure(20.0, 100.0), 23.37, delta=0.1)

    def test_missing_inputs(self):
        self.assertIsNone(dew_point(None, 50))
        self.assertIsNone(dew_point(20, None))
        self.assertIsNone(dew_point(20, 0))


def beacon(tmp, hdc, rh, sht, sht_rh):
    return {"TMP119_C": tmp, "HDC3022_C": hdc, "RH_pct": rh,
            "SHT3x_C": sht, "SHT3x_RH_pct": sht_rh}


class AssessTests(unittest.TestCase):
    def test_capture_from_new_firmware_is_ok(self):
        m = assess(beacon(23.75, 24.81, 48.76, 26.92, 47.04))
        self.assertEqual(m["level"], "ok")
        self.assertAlmostEqual(m["td_int_C"], 14.67, delta=0.05)
        self.assertAlmostEqual(m["td_ext_C"], 13.30, delta=0.05)
        self.assertAlmostEqual(m["margin_int_C"], 23.75 - 14.67, delta=0.05)

    def test_freezer_entry_is_risk(self):
        # Humid internal air (dew point ~26 C) after a hot humid day; the
        # device has just entered a cold room: ambient 5 C.
        m = assess(beacon(5.0, 5.5, 60.0, 29.0, 85.0))
        self.assertEqual(m["level"], "risk")
        self.assertLess(m["margin_int_C"], 0)
        self.assertTrue(any("walls" in r for r in m["reasons"]))

    def test_caution_band(self):
        td = dew_point(25.0, 70.0)
        m = assess(beacon(td + 1.5, td + 2.0, 50.0, 25.0, 70.0))
        self.assertEqual(m["level"], "caution")

    def test_high_internal_rh_is_caution(self):
        m = assess(beacon(30.0, 30.0, 40.0, 30.5, 92.0), caution_margin_c=0.1)
        self.assertEqual(m["level"], "caution")

    def test_cold_device_in_warm_humid_air(self):
        # Device still cold in a 25 C / 80 % room (dew point ~21 C). The
        # HDC3022 is itself below that dew point, so it is saturated and
        # reads ~100 %: the external dew point then equals its own
        # temperature and the margin pins near zero -> caution, and risk
        # only where another sensor (here TMP119) is colder still.
        m = assess(beacon(2.0, 3.0, 100.0, 4.0, 30.0))
        self.assertAlmostEqual(m["td_ext_C"], 3.0, places=6)
        self.assertLess(m["margin_ext_C"], 0)
        self.assertEqual(m["level"], "risk")
        m = assess(beacon(3.0, 3.0, 100.0, 4.0, 30.0))
        self.assertEqual(m["level"], "caution")

    def test_old_firmware_without_internal_rh(self):
        m = assess(beacon(20.0, 20.0, 50.0, 22.0, None))
        self.assertIsNone(m["td_int_C"])
        self.assertIsNone(m["margin_int_C"])
        self.assertEqual(m["level"], "ok")          # external margin still assessed


class TrackerTests(unittest.TestCase):
    def test_debounce(self):
        t = LevelTracker(persist=2)
        self.assertEqual(t.update("ok"), (None, "ok"))   # first level accepted at once
        self.assertIsNone(t.update("caution"))           # one burst: not yet
        self.assertIsNone(t.update("ok"))                # back: candidate reset
        self.assertIsNone(t.update("caution"))
        self.assertEqual(t.update("caution"), ("ok", "caution"))
        self.assertIsNone(t.update("caution"))
        self.assertIsNone(t.update(None))                # no data: no change


if __name__ == "__main__":
    unittest.main()
