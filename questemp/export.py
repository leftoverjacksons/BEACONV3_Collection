"""CSV output for parsed QUESTemp reports."""

import csv
from pathlib import Path

from . import printout

LEAD = ["session", "sensor", "timestamp", "time", "unit"]


def _write(path, rows, lead):
    keys = list(lead)
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            out = dict(r)
            if out.get("timestamp") is not None:
                out["timestamp"] = out["timestamp"].isoformat(sep=" ")
            w.writerow(out)


def write_csvs(sessions, rows, stem, celsius=True):
    """Write <stem>.long.csv (one row per sensor per minute) and
    <stem>.wide.csv (one row per minute, sensor-prefixed columns)."""
    stem = Path(stem)
    if celsius:
        rows = [printout.to_celsius(r) for r in rows]
    long_csv = stem.with_name(stem.name + ".long.csv")
    wide_csv = stem.with_name(stem.name + ".wide.csv")
    _write(long_csv, rows, LEAD)
    _write(wide_csv, printout.wide_rows(rows), ["session", "timestamp",
                                                 "time", "unit"])
    return long_csv, wide_csv
