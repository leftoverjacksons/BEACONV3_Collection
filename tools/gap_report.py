#!/usr/bin/env python3
"""
Report gaps in the logged data: for each source, the typical interval
between rows and every gap longer than a threshold. The HOBO's two packet
kinds are reported separately (A = temp/RH, B = solar).

    python3 -m tools.gap_report               # last 24 h
    python3 -m tools.gap_report --hours 6
"""

import argparse
import csv
import statistics
import time
from datetime import datetime
from pathlib import Path

from beacon_station import config
from beacon_station.csvlog import FILE_PREFIX

THRESH_S = {"beacon": 60, "anemo": 10, "hobo-A temp/RH": 180,
            "hobo-B solar": 180, "event": None}


def classify(r):
    src = r.get("source")
    if src != "hobo":
        return src
    if r.get("solar_Wm2") or r.get("solar_accum_MJm2"):
        return "hobo-B solar"
    if r.get("hobo_T_C") or r.get("hobo_RH_pct"):
        return "hobo-A temp/RH"
    return "hobo-?"


def row_time(r):
    if r.get("utc_time"):
        return datetime.fromisoformat(r["utc_time"]).timestamp()
    return datetime.fromisoformat(r["iso_time"]).astimezone().timestamp()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = config.load(args.config)
    cutoff = time.time() - args.hours * 3600

    times, files = {}, []
    for f in sorted(Path(cfg["logging"]["data_dir"]).glob(f"{FILE_PREFIX}*.csv")):
        if f.stat().st_mtime < cutoff:
            continue
        files.append(f)
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    t = row_time(r)
                except (ValueError, KeyError, TypeError):
                    continue
                if t >= cutoff:
                    times.setdefault(classify(r), []).append(t)

    fmt = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")
    print(f"{len(files)} file(s), last {args.hours:g} h; file starts (= logger restarts):")
    for f in files:
        print("   ", f.name)
    for src in sorted(times):
        ts = sorted(times[src])
        d = [b - a for a, b in zip(ts, ts[1:])]
        med = statistics.median(d) if d else float("nan")
        print(f"\n{src}: {len(ts)} rows, {fmt(ts[0])} .. {fmt(ts[-1])}, "
              f"median interval {med:.1f} s")
        th = THRESH_S.get(src)
        if th is None:
            continue
        gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > th]
        if not gaps:
            print(f"   no gaps > {th} s")
        for a, b in gaps[:40]:
            print(f"   gap {fmt(a)} -> {fmt(b)}  ({(b - a) / 60:.1f} min)")
        if len(gaps) > 40:
            print(f"   ... {len(gaps) - 40} more")


if __name__ == "__main__":
    main()
