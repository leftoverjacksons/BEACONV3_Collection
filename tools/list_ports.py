#!/usr/bin/env python3
"""
List serial ports with their stable /dev/serial/by-id names, and optionally
listen briefly to each to identify which instrument is on it.

    python3 -m tools.list_ports            # list
    python3 -m tools.list_ports --sniff    # list + identify by content

Stop beacon-logger first when sniffing (sudo systemctl stop beacon-logger):
a port can only be read by one process at a time.
"""

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial missing: sudo apt install python3-serial")

BY_ID = Path("/dev/serial/by-id")


def by_id_map():
    out = {}
    if BY_ID.is_dir():
        for link in BY_ID.iterdir():
            out[os.path.realpath(link)] = str(link)
    return out


def sniff(port, baud, seconds):
    try:
        with serial.Serial(port, baud, timeout=0.5) as s:
            t_end = time.monotonic() + seconds
            lines = []
            while time.monotonic() < t_end:
                raw = s.readline()
                if raw:
                    lines.append(raw.decode("utf-8", errors="replace").strip())
    except serial.SerialException as exc:
        return f"cannot open: {exc}", []
    text = "\n".join(lines)
    if "env_hs_sampler" in text or "<inf>" in text:
        guess = "BEACON (Zephyr log stream)"
    elif "t_ms,wind_ms" in text or any(
            l.count(",") >= 2 and l.split(",")[0].isdigit() for l in lines):
        guess = "anemometer bridge (CSV)"
    elif lines:
        guess = "unknown device (data present)"
    else:
        guess = "silent"
    return guess, lines[-3:]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sniff", action="store_true")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=12.0,
                    help="listen time per port (BEACON bursts every ~10 s)")
    args = ap.parse_args()

    ids = by_id_map()
    ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
    if not ports:
        print("no serial ports found")
        return
    for p in ports:
        real = os.path.realpath(p.device)
        print(f"{p.device}")
        print(f"    by-id : {ids.get(real, '(none — device has no USB serial number?)')}")
        vid = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else "-"
        print(f"    usb   : {vid}  serial={p.serial_number or '-'}")
        print(f"    desc  : {p.description}")
        if args.sniff:
            guess, tail = sniff(p.device, args.baud, args.seconds)
            print(f"    looks like: {guess}")
            for line in tail:
                print(f"      | {line[:100]}")
        print()
    print("Put the by-id paths into config.toml ([beacon] port / [anemo] port).")


if __name__ == "__main__":
    main()
