#!/usr/bin/env python3
"""
Watch HOBO MX advertisements live, decoded, for checking against what
HOBOconnect shows and for diagnosing gaps. Uses the same listener as
beacon-logger and can run alongside it.

    sudo python3 -m tools.hobo_scan             # changed packets, decoded
    sudo python3 -m tools.hobo_scan --all       # every packet heard
    sudo python3 -m tools.hobo_scan --summary 30
        # every 30 s: packets heard per kind (A temp/RH, B solar, ?N unknown
        # N-byte layout) and PDU (adv = advertisement, rsp = scan response),
        # with signal strength range

sudo is needed for the raw HCI socket (CAP_NET_RAW); without it BlueZ merges
the logger's two packet kinds and the temp/RH one is usually lost.
"""

import argparse
import queue
import threading
import time
from datetime import datetime

from beacon_station.hobo import HoboReader


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--address", default="", help="only this MAC")
    ap.add_argument("--seconds", type=float, default=0,
                    help="stop after this long (default: until Ctrl+C)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="print every packet")
    mode.add_argument("--summary", type=float, metavar="S",
                      help="print packet counts every S seconds")
    args = ap.parse_args()

    q = queue.Queue()
    r = HoboReader({"address": args.address, "heartbeat_s": 1e9}, q)
    lock = threading.Lock()
    tally = {}

    def tap(addr, payload, rssi, pdu, kind, fields):
        key = f"{kind or '?' + str(len(payload))}/{pdu or '-'}"
        if args.summary:
            with lock:
                n, lo, hi = tally.get(key, (0, None, None))
                if rssi is not None:
                    lo = rssi if lo is None else min(lo, rssi)
                    hi = rssi if hi is None else max(hi, rssi)
                tally[key] = (n + 1, lo, hi)
        elif args.all:
            vals = "  ".join(f"{k}={v:.5g}" for k, v in fields.items()
                             if isinstance(v, float))
            print(f"{datetime.now():%H:%M:%S.%f}"[:-3], addr, f"{key:<8}",
                  f"rssi={rssi}", vals, f"raw={bytes(payload).hex()}", flush=True)

    r.tap = tap
    r.start()
    t_end = time.monotonic() + args.seconds if args.seconds else None
    next_sum = time.monotonic() + (args.summary or 0)
    try:
        while t_end is None or time.monotonic() < t_end:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                item = None
            if args.summary and time.monotonic() >= next_sum:
                next_sum += args.summary
                with lock:
                    snap = dict(tally)
                    tally.clear()
                parts = [f"{k} {n} (rssi {lo}..{hi})" for k, (n, lo, hi)
                         in sorted(snap.items())] or ["nothing heard"]
                print(f"{datetime.now():%H:%M:%S}", " | ".join(parts), flush=True)
            if item is None:
                continue
            if item[0] == "note":
                print("#", item[2], flush=True)
            elif not args.all and not args.summary:
                s = item[1]
                vals = "  ".join(f"{k}={v:.5g}" for k, v in s.items()
                                 if isinstance(v, float))
                print(f"{s['wall']:%H:%M:%S.%f}"[:-3], s["hobo_addr"], vals,
                      f"raw={s['hobo_raw']}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        r.stop()


if __name__ == "__main__":
    main()
