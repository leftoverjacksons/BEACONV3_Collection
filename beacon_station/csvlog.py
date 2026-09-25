"""
Sparse CSV log with daily rotation and a metadata sidecar per file.

One row per measurement event from either instrument (or an operator event
marker). Columns not belonging to the row's source are left blank — no
forward-filling. Align downstream with merge_asof / resampling.

Schema: the first 11 columns are identical to the original laptop logger
(beacon_env_logger.py), so existing readers keep working. Appended:

    utc_time      same instant as iso_time, UTC, explicit +00:00 offset
    dev_uptime_s  BEACON's own uptime stamp for the 'Raw:' line (beacon rows)
    note          event-marker label (source == 'event')
  schema 3 (HOBO MX2309 over Bluetooth, source == 'hobo'; see hobo.py):
    hobo_T_C hobo_RH_pct solar_Wm2 solar_accum_MJm2 hobo_ch0d
    hobo_addr     Bluetooth address of the logger heard
    hobo_raw      the advertisement payload, hex — decoding is inferred, so
                  the raw bytes are kept for re-decoding

iso_time stays naive local time, as before. Temperatures stay native deg C.

A new file is started at launch and at each local midnight. Files are never
appended to after a restart, so a torn final line from a power cut can only
ever be the last line of a file.
"""

import csv
import json
import os
import platform
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from . import sysinfo

SCHEMA_VERSION = 3
CSV_COLUMNS = [
    "iso_time", "source",
    "TMP119_C", "SHT3x_C", "HDC3022_C", "RH_pct", "P_hPa",
    "comp_temp_C", "WBGT_C",
    "wind_ms", "wind_deg",
    "utc_time", "dev_uptime_s", "note",
    "hobo_T_C", "hobo_RH_pct", "solar_Wm2", "solar_accum_MJm2", "hobo_ch0d",
    "hobo_addr", "hobo_raw",
]
FILE_PREFIX = "beacon_env_log_"


def _f(v, spec=".2f"):
    return "" if v is None else format(v, spec)


def format_row(s):
    """Sample dict -> list of CSV cells, in CSV_COLUMNS order."""
    wall = s["wall"]
    row = dict.fromkeys(CSV_COLUMNS, "")
    row["iso_time"] = wall.replace(tzinfo=None).isoformat(
        timespec="milliseconds")
    row["utc_time"] = wall.astimezone(timezone.utc).isoformat(
        timespec="milliseconds")
    row["source"] = s["source"]
    if s["source"] == "beacon":
        for k in ("TMP119_C", "SHT3x_C", "HDC3022_C", "RH_pct", "P_hPa",
                  "WBGT_C"):
            row[k] = _f(s.get(k))
        row["comp_temp_C"] = _f(s.get("comp_temp_C"), ".3f")
        row["dev_uptime_s"] = _f(s.get("dev_uptime_s"), ".3f")
    elif s["source"] == "anemo":
        row["wind_ms"] = _f(s["wind_ms"])
        row["wind_deg"] = _f(s["wind_deg"], ".1f")
    elif s["source"] == "hobo":
        row["hobo_T_C"] = _f(s.get("hobo_T_C"), ".3f")
        row["hobo_RH_pct"] = _f(s.get("hobo_RH_pct"), ".2f")
        row["solar_Wm2"] = _f(s.get("solar_Wm2"), ".3f")
        row["solar_accum_MJm2"] = _f(s.get("solar_accum_MJm2"), ".6f")
        row["hobo_ch0d"] = _f(s.get("hobo_ch0d"), ".4f")
        row["hobo_addr"] = s.get("hobo_addr", "")
        row["hobo_raw"] = s.get("hobo_raw", "")
    elif s["source"] == "event":
        # Keep the label CSV-safe and single-line.
        row["note"] = " ".join(str(s.get("note", "")).split())[:200]
    return [row[c] for c in CSV_COLUMNS]


class RotatingCsv:
    def __init__(self, data_dir, fsync_interval_s, run_info):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fsync_interval = fsync_interval_s
        self.run_info = run_info
        self.file = None
        self.path = None
        self.rows = 0
        self._day = None
        self._last_sync = time.monotonic()

    def _open(self, now):
        self.close()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        self.path = self.dir / f"{FILE_PREFIX}{stamp}.csv"
        n = 1
        while self.path.exists():       # two opens within one second
            self.path = self.dir / f"{FILE_PREFIX}{stamp}_{n}.csv"
            n += 1
        self.file = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.writer.writerow(CSV_COLUMNS)
        self.file.flush()
        self.rows = 0
        self._day = now.date()
        self._write_meta(now)

    def _write_meta(self, now):
        meta = {
            "schema_version": SCHEMA_VERSION,
            "columns": CSV_COLUMNS,
            "file": self.path.name,
            "opened_local": now.isoformat(timespec="seconds"),
            "opened_utc": now.astimezone(timezone.utc).isoformat(
                timespec="seconds"),
            "utc_offset": now.strftime("%z"),
            "hostname": socket.gethostname(),
            "logger_git": sysinfo.git_version(),
            "python": platform.python_version(),
            "clock_synced_at_open": sysinfo.clock_synced(),
            "units": {"temperature": "degC", "pressure": "hPa",
                      "solar": "W/m2", "solar_accum": "MJ/m2 (inferred)",
                      "wind_speed": "m/s", "wind_dir": "deg from N",
                      "rh": "%"},
            **self.run_info,
        }
        meta_path = self.path.with_suffix(".meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            f.write("\n")

    def write(self, sample):
        now = sample["wall"]
        if self.file is None or now.date() != self._day:
            self._open(now)
        self.writer.writerow(format_row(sample))
        self.file.flush()
        self.rows += 1
        if time.monotonic() - self._last_sync >= self.fsync_interval:
            self.sync()

    def sync(self):
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())
        self._last_sync = time.monotonic()

    def close(self):
        if self.file is not None:
            self.sync()
            self.file.close()
            self.file = None
