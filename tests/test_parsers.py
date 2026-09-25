"""python3 -m unittest discover -s tests"""

import csv
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from beacon_station.csvlog import CSV_COLUMNS, RotatingCsv, format_row
from beacon_station.parsers import AnemoParser, BeaconParser, clean_line

FIXTURE = Path(__file__).parent / "fixtures" / "beacon_capture.txt"
WALL = datetime(2026, 9, 25, 12, 0, 0, 123456).astimezone()


def run_beacon(lines):
    p = BeaconParser()
    out = []
    for raw in lines:
        line = clean_line(raw)
        if line:
            out.extend(p.feed(line, WALL)[0])
    return out, p


class BeaconTests(unittest.TestCase):
    def test_fixture_two_complete_bursts(self):
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        out, p = run_beacon(lines)
        self.assertEqual(len(out), 2)
        self.assertIsNone(p.pending)
        a, b = out
        self.assertEqual(a["TMP119_C"], 23.45)
        self.assertEqual(a["SHT3x_C"], 27.19)
        self.assertEqual(a["HDC3022_C"], 24.66)
        self.assertEqual(a["RH_pct"], 50.41)
        self.assertEqual(a["P_hPa"], 997.19)
        self.assertEqual(a["comp_temp_C"], 24.113)
        self.assertEqual(a["WBGT_C"], 19.53)
        self.assertAlmostEqual(a["dev_uptime_s"], 66 * 3600 + 28 * 60 + 0.919)
        self.assertEqual(b["RH_pct"], 51.97)
        self.assertEqual(b["WBGT_C"], 19.70)

    def test_real_escape_bytes(self):
        raw = (b"[66:28:00.919,000] \x1b[0m<inf> env_hs_sampler: Raw: "
               b"TMP119=1.00 SHT3x=2.00 HDC3022=3.00 RH=4.00 P=5.00 hPa"
               b"\x1b[0m\r\n")
        _, p = run_beacon([raw])
        self.assertEqual(p.pending["P_hPa"], 5.0)

    def test_truncated_burst_flushed_on_next_raw(self):
        lines = [
            "[1:00:00.000,000] <inf> env_hs_sampler: Raw: TMP119=1 SHT3x=2 "
            "HDC3022=3 RH=4 P=5 hPa",
            "[1:00:10.000,000] <inf> env_hs_sampler: Raw: TMP119=6 SHT3x=7 "
            "HDC3022=8 RH=9 P=10 hPa",
        ]
        out, p = run_beacon(lines)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["TMP119_C"], 1.0)
        self.assertIsNone(out[0]["comp_temp_C"])
        self.assertIsNone(out[0]["WBGT_C"])
        self.assertEqual(p.pending["TMP119_C"], 6.0)
        self.assertEqual(len(p.flush()), 1)

    def test_interleaved_line_uses_nearest_uptime(self):
        line = ("[66:27:49.169,000] <inf> noise_proc: Processed => "
                "[66:28:00.919,000] <inf> env_hs_sampler: Raw: TMP119=1 "
                "SHT3x=2 HDC3022=3 RH=4 P=5 hPa")
        _, p = run_beacon([line])
        self.assertAlmostEqual(p.pending["dev_uptime_s"], 239280.919)

    def test_orphan_lines_ignored(self):
        out, p = run_beacon([
            "<inf> env_hs_sampler: Compensated temp: 24.1 degC (75.4 degF)",
            "<inf> env_hs_sampler: HS Sample: T=75.40 deg F WBGT=19.53",
        ])
        self.assertEqual(out, [])
        self.assertIsNone(p.pending)


class AnemoTests(unittest.TestCase):
    def test_rows(self):
        p = AnemoParser()
        self.assertEqual(p.feed("t_ms,wind_ms,wind_deg", WALL), ([], None))
        self.assertEqual(p.feed("# bridge up", WALL), ([], "bridge up"))
        self.assertEqual(p.feed("123,,,21.0", WALL), ([], None))
        out, _ = p.feed("123,1.25,270.0,21.3,48.2,997.1", WALL)
        self.assertEqual(out[0]["wind_ms"], 1.25)
        self.assertEqual(out[0]["wind_deg"], 270.0)


class CsvTests(unittest.TestCase):
    def test_legacy_columns_unchanged(self):
        self.assertEqual(CSV_COLUMNS[:11], [
            "iso_time", "source", "TMP119_C", "SHT3x_C", "HDC3022_C",
            "RH_pct", "P_hPa", "comp_temp_C", "WBGT_C", "wind_ms",
            "wind_deg"])

    def test_row_format(self):
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        out, _ = run_beacon(lines)
        row = dict(zip(CSV_COLUMNS, format_row(out[0])))
        self.assertEqual(row["iso_time"], "2026-09-25T12:00:00.123")
        self.assertEqual(row["comp_temp_C"], "24.113")
        self.assertEqual(row["wind_ms"], "")
        utc = datetime.fromisoformat(row["utc_time"])
        self.assertEqual(utc.utcoffset().total_seconds(), 0)
        self.assertEqual(utc, WALL.replace(microsecond=123000))

        ev = dict(zip(CSV_COLUMNS, format_row(
            {"source": "event", "wall": WALL, "note": "TSI\nmoved, 2 m"})))
        self.assertEqual(ev["note"], "TSI moved, 2 m")

    def test_rotation_at_midnight(self):
        with tempfile.TemporaryDirectory() as d:
            log = RotatingCsv(d, 30, {})
            day1 = datetime(2026, 9, 25, 23, 59, 59).astimezone()
            day2 = datetime(2026, 9, 26, 0, 0, 1).astimezone()
            log.write({"source": "anemo", "wall": day1,
                       "wind_ms": 1.0, "wind_deg": 90.0})
            log.write({"source": "anemo", "wall": day2,
                       "wind_ms": 2.0, "wind_deg": 90.0})
            log.close()
            files = sorted(Path(d).glob("*.csv"))
            self.assertEqual(len(files), 2)
            self.assertEqual(len(list(Path(d).glob("*.meta.json"))), 2)
            rows = list(csv.reader(io.StringIO(files[1].read_text())))
            self.assertEqual(rows[0], CSV_COLUMNS)
            self.assertEqual(rows[1][9], "2.00")


if __name__ == "__main__":
    unittest.main()
