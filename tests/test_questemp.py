"""QUESTemp Print-report parsing, against the sample report in the user
manual (056-663 Rev J, figures 19-20), and the serial recorders against a
pseudo-terminal standing in for the instrument."""

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from questemp import export, printout

FIXTURE = Path(__file__).parent / "fixtures" / "questemp_printout.txt"


class PrintoutTests(unittest.TestCase):
    def setUp(self):
        self.sessions, self.rows = printout.parse(FIXTURE.read_text())

    def test_header(self):
        s = self.sessions[3]
        self.assertEqual(s["start"], datetime(2008, 2, 21, 11, 7, 32))
        self.assertEqual(s["serial"], "TK09090909")
        self.assertEqual(s["interval_min"], 1)
        self.assertEqual(s["unit"], "F")
        self.assertEqual(s["model"], "Questemp 36")

    def test_rows(self):
        # 3 sensors + weighted average, 2 minutes each.
        self.assertEqual(len(self.rows), 8)
        r = self.rows[0]
        self.assertEqual(r["sensor"], "1")
        self.assertEqual(r["timestamp"], datetime(2008, 2, 21, 11, 8))
        self.assertEqual(r["wbgt_in"], 68.7)
        self.assertEqual(r["globe"], 90.7)
        self.assertEqual(r["rh_pct"], 13)
        self.assertEqual(r["flow_mps"], 0.5)
        self.assertEqual(r["stay_very_heavy_min"], 60)
        # Sensor 2 has no FLOW column; stay times must not shift into it.
        s2 = [r for r in self.rows if r["sensor"] == "2"][0]
        self.assertNotIn("flow_mps", s2)
        self.assertEqual(s2["stay_moderate_min"], 45)

    def test_weighted_average(self):
        w = [r for r in self.rows if r["sensor"] == "W-AVG"]
        self.assertEqual(len(w), 2)
        self.assertEqual(w[1]["wbgt_in"], 71.8)
        self.assertEqual(w[1]["wbgt_out"], 71.1)
        self.assertEqual(w[1]["stay_very_heavy_min"], 45)

    def test_celsius(self):
        c = printout.to_celsius(self.rows[0])
        self.assertEqual(c["unit"], "C")
        self.assertAlmostEqual(c["dry_bulb"], (82.4 - 32) * 5 / 9, places=2)
        self.assertEqual(c["heat_index"], 0)     # 0 = not computed
        self.assertEqual(c["rh_pct"], 13)

    def test_midnight_rollover(self):
        text = FIXTURE.read_text().replace(
            "11:07:32", "23:58:10").replace("11:08", "23:59").replace(
            "11:09", "00:00")
        _, rows = printout.parse(text)
        self.assertEqual(rows[0]["timestamp"], datetime(2008, 2, 21, 23, 59))
        self.assertEqual(rows[1]["timestamp"], datetime(2008, 2, 22, 0, 0))

    def test_wide(self):
        wide = printout.wide_rows(self.rows)
        self.assertEqual(len(wide), 2)
        self.assertEqual(wide[0]["s2_globe"], 104.5)
        self.assertEqual(wide[0]["wavg_wbgt_in"], 71.5)

    def test_csv(self):
        with tempfile.TemporaryDirectory() as d:
            lc, wc = export.write_csvs(self.sessions, self.rows,
                                       Path(d) / "x")
            self.assertTrue(lc.read_text().startswith(
                "session,sensor,timestamp,time,unit,"))
            self.assertEqual(len(wc.read_text().splitlines()), 3)

    def test_garbage(self):
        self.assertEqual(printout.parse("\x00\xff random noise\n")[1], [])


REAL = Path(__file__).parent / "fixtures" / "questemp34_real_excerpt.txt"


class RealQT34Tests(unittest.TestCase):
    """Pages 1, 2, 20-22 and the trailer of a real QT34 Rev 1.10 report
    (CRLF, form feeds, ^Z), 1-min log starting 01-SEP-26 08:55:28."""

    def setUp(self):
        text = REAL.read_bytes().decode("latin-1")
        self.sessions, self.rows = printout.parse(text)

    def test_header(self):
        s = self.sessions[1]
        self.assertEqual(s["model"], "Questemp 34")
        self.assertEqual(s["firmware"], "1.10")
        self.assertEqual(s["serial"], "TEY120005")
        self.assertEqual(s["start"], datetime(2026, 9, 1, 8, 55, 28))

    def test_column_names(self):
        # Printed as "RH(%)" and "H.I." on this firmware.
        r = self.rows[0]
        self.assertEqual(r["time"], "08:56")
        self.assertEqual(r["rh_pct"], 93)
        self.assertEqual(r["heat_index"], 71)
        self.assertEqual(r["globe"], 74.1)
        self.assertNotIn("rh(%)", r)

    def test_dates_across_pages(self):
        ts = [r["timestamp"] for r in self.rows]
        self.assertEqual(ts, sorted(ts))
        self.assertIn(datetime(2026, 9, 1, 23, 59), ts)
        self.assertIn(datetime(2026, 9, 2, 0, 0), ts)
        # Page 21 starts at 00:46 on the next day, on a fresh page header.
        self.assertIn(datetime(2026, 9, 2, 0, 46), ts)
        self.assertEqual(ts[-1].date(), datetime(2026, 9, 2).date())

    def test_heat_index_converted(self):
        c = printout.to_celsius(self.rows[0])
        self.assertAlmostEqual(c["heat_index"], (71 - 32) * 5 / 9, places=2)


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class SerialToolTests(unittest.TestCase):
    """The pty's slave end is opened as the 'COM port'; the test plays the
    instrument on the master end."""

    def setUp(self):
        try:
            import serial  # noqa: F401
        except ImportError:
            self.skipTest("pyserial not installed")
        import tty
        self.master, slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self.slave = slave
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        os.close(self.master)
        os.close(self.slave)
        self.tmp.cleanup()

    def test_listen_captures_and_parses_report(self):
        from questemp import serial_tools

        def instrument():
            time.sleep(0.5)
            data = FIXTURE.read_bytes().replace(b"\n", b"\r\n")
            for i in range(0, len(data), 200):
                os.write(self.master, data[i:i + 200])
                time.sleep(0.01)

        threading.Thread(target=instrument, daemon=True).start()
        log = serial_tools.listen(self.port, 9600, self.tmp.name, idle_s=0.5,
                                  duration=2.5, echo=False)
        out = Path(self.tmp.name)
        raw = next(out.glob("*.rx.bin")).read_bytes()
        self.assertEqual(raw, FIXTURE.read_bytes().replace(b"\n", b"\r\n"))
        self.assertTrue(list(out.glob("*.report1.long.csv")))
        self.assertIn("report: 1 session(s), 8 rows", log.read_text())

    def test_probe_records_reply(self):
        from questemp import serial_tools

        def instrument():
            buf = b""
            while b"?" not in buf:
                buf += os.read(self.master, 64)
            os.write(self.master, b"QT36\r\n")

        threading.Thread(target=instrument, daemon=True).start()
        res = serial_tools.probe(self.port, 9600, self.tmp.name,
                                 stimuli=[b"\r", b"?"], wait_s=0.4, echo=False)
        self.assertEqual(res[0], (b"\r", b""))
        self.assertEqual(res[1], (b"?", b"QT36\r\n"))


if __name__ == "__main__":
    unittest.main()
