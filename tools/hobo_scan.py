#!/usr/bin/env python3
"""
Print HOBO MX advertisements live, decoded, for checking against what
HOBOconnect shows. Uses the same listener as beacon-logger and can run
alongside it.

    sudo python3 -m tools.hobo_scan         # raw HCI: sees temp/RH AND solar
    python3 -m tools.hobo_scan              # BlueZ only: mostly solar packets

sudo is needed for the raw HCI socket (CAP_NET_RAW); without it BlueZ merges
the logger's two packet kinds and the temp/RH one is usually lost.
"""

import argparse
import queue
import time

from beacon_station.hobo import HoboReader


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--address", default="", help="only this MAC")
    ap.add_argument("--seconds", type=float, default=0,
                    help="stop after this long (default: until Ctrl+C)")
    args = ap.parse_args()

    q = queue.Queue()
    # heartbeat_s huge: print changes only.
    r = HoboReader({"address": args.address, "heartbeat_s": 1e9}, q)
    r.start()
    t_end = time.monotonic() + args.seconds if args.seconds else None
    try:
        while t_end is None or time.monotonic() < t_end:
            try:
                item = q.get(timeout=1)
            except queue.Empty:
                continue
            if item[0] == "note":
                print("#", item[2], flush=True)
                continue
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
