#!/usr/bin/env python3
"""
Print HOBO MX advertisements live, decoded, for checking the decoding
against what HOBOconnect shows. Safe to run while beacon-logger is running
(BlueZ shares the scan).

    python3 -m tools.hobo_scan              # every Onset logger in range
    python3 -m tools.hobo_scan --all        # also repeats of unchanged packets
"""

import argparse
import asyncio
import sys
from datetime import datetime

from beacon_station.hobo import ONSET_COMPANY_ID, decode

try:
    from bleak import BleakScanner
except ImportError:
    sys.exit("python3-bleak missing: sudo apt install python3-bleak")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true",
                    help="print every advertisement, not only changes")
    ap.add_argument("--seconds", type=float, default=0,
                    help="stop after this long (default: until Ctrl+C)")
    args = ap.parse_args()
    last = {}

    def cb(device, adv):
        data = adv.manufacturer_data.get(ONSET_COMPANY_ID)
        if not data:
            return
        kind, fields = decode(data)
        key = (device.address, kind)
        if not args.all and last.get(key) == data:
            return
        last[key] = data
        vals = "  ".join(f"{k}={v:.5g}" if isinstance(v, float) else f"{k}={v}"
                         for k, v in fields.items())
        print(f"{datetime.now():%H:%M:%S.%f}"[:-3], device.address,
              f"rssi={adv.rssi}", f"kind={kind}", vals, f"raw={bytes(data).hex()}",
              flush=True)

    async def run():
        async with BleakScanner(detection_callback=cb):
            print("scanning… Ctrl+C to stop", flush=True)
            if args.seconds:
                await asyncio.sleep(args.seconds)
            else:
                while True:
                    await asyncio.sleep(3600)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
