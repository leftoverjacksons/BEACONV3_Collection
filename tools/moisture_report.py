#!/usr/bin/env python3
"""
Is moisture getting into the housing, or is the internal RH just following
temperature? Prints, per time bin, the internal temperature, RH, dew point
and absolute humidity (water content, g/m3) from the BEACON's SHT3x, next to
the outside RH.

RH rises whenever air cools, with no water added. The internal dew point and
absolute humidity only rise when water vapour is added: a leak, air drawn in
through the seals, or water released by materials inside. So a flat dew
point with rising RH = cooling only; a rising dew point = water got in.

    python3 -m tools.moisture_report               # last 24 h, 30-min bins
    python3 -m tools.moisture_report --hours 12 --bin 15
"""

import argparse
import csv
import time
from datetime import datetime
from pathlib import Path

from beacon_station import config
from beacon_station.csvlog import FILE_PREFIX
from beacon_station.moisture import dew_point, vapor_pressure


def abs_humidity(t_c, rh):
    """Water vapour density, g/m3."""
    e = vapor_pressure(t_c, rh)
    return None if e is None else 216.7 * e / (t_c + 273.15)


def num(r, k):
    try:
        return float(r[k])
    except (KeyError, TypeError, ValueError):
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--bin", type=float, default=30, help="minutes per row")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = config.load(args.config)
    cutoff = time.time() - args.hours * 3600
    width = args.bin * 60

    bins = {}
    for f in sorted(Path(cfg["logging"]["data_dir"]).glob(f"{FILE_PREFIX}*.csv")):
        if f.stat().st_mtime < cutoff:
            continue
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("source") != "beacon":
                    continue
                try:
                    t = (datetime.fromisoformat(r["utc_time"]).timestamp()
                         if r.get("utc_time") else
                         datetime.fromisoformat(r["iso_time"]).astimezone().timestamp())
                except (KeyError, TypeError, ValueError):
                    continue
                ti, rhi = num(r, "SHT3x_C"), num(r, "SHT3x_RH_pct")
                if t < cutoff or ti is None or rhi is None:
                    continue
                b = bins.setdefault(int(t // width), [])
                b.append((ti, rhi, dew_point(ti, rhi), abs_humidity(ti, rhi),
                          num(r, "RH_pct"), num(r, "HDC3022_C")))

    if not bins:
        print("No BEACON rows with internal RH (SHT3x_RH_pct) in that window.")
        return
    print(f"{'time':<12} {'T_in C':>7} {'RH_in %':>8} {'Td_in C':>8} "
          f"{'AH_in g/m3':>11} {'T_out C':>8} {'RH_out %':>9}")
    first = None
    for k in sorted(bins):
        cols = [mean(c) for c in zip(*bins[k])]
        ti, rhi, td, ah, rho, to = cols
        first = first or cols
        f = lambda v, w, p=1: f"{v:>{w}.{p}f}" if v is not None else " " * (w - 1) + "-"
        print(f"{datetime.fromtimestamp(k * width):%m-%d %H:%M} "
              f"{f(ti, 7)} {f(rhi, 8)} {f(td, 8)} {f(ah, 11, 2)} {f(to, 8)} {f(rho, 9)}")
    last = cols
    print(f"\nover the window: internal RH {last[1] - first[1]:+.1f} %, "
          f"temperature {last[0] - first[0]:+.1f} C, dew point "
          f"{last[2] - first[2]:+.1f} C, water content {last[3] - first[3]:+.2f} g/m3")


if __name__ == "__main__":
    main()
