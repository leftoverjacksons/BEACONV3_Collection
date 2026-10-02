"""
QUESTemp 34/36 serial toolkit (standalone; not used by beacon_station).

    python -m questemp ports
    python -m questemp listen COM9
    python -m questemp probe  COM9
    python -m questemp bridge COM9 COM21
    python -m questemp parse  capture.txt
"""

import argparse
import sys
from pathlib import Path

from . import export, printout, serial_tools


def _onoff(v):
    if v is None:
        return None
    return v.lower() in ("1", "on", "true", "high")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m questemp",
                                 description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports")

    def common(p):
        p.add_argument("port", help="instrument port, e.g. COM9 or /dev/ttyUSB0")
        p.add_argument("--baud", type=int, default=9600,
                       help="default 9600 (fixed on the unit per the manual)")
        p.add_argument("--out", default="questemp_captures",
                       help="output directory (default ./questemp_captures)")
        p.add_argument("--dtr", choices=["on", "off"],
                       help="force DTR (default: driver's choice, usually on)")
        p.add_argument("--rts", choices=["on", "off"], help="force RTS")
        p.add_argument("--quiet", action="store_true",
                       help="do not echo traffic to the console")

    p = sub.add_parser("listen", help="record everything the unit sends")
    common(p)
    p.add_argument("--idle", type=float, default=10.0,
                   help="seconds of silence that end a report (default 10)")
    p.add_argument("--no-parse", action="store_true",
                   help="only record raw bytes")
    p.add_argument("--duration", type=float,
                   help="stop after this many seconds")

    p = sub.add_parser("probe", help="send stimuli and record replies")
    common(p)
    p.add_argument("--wait", type=float, default=1.5,
                   help="seconds to wait for a reply to each stimulus")
    p.add_argument("--send", action="append", metavar="HEX",
                   help="send these bytes instead of the default list "
                        "(hex, e.g. 0d or 3f0d); repeatable")
    p.add_argument("--sweep", action="store_true",
                   help="send every single byte 0x00-0xFF. Undocumented "
                        "bytes could be commands; save your logged data "
                        "first")

    p = sub.add_parser("bridge",
                       help="sit between the unit and TSI DMS and record both")
    p.add_argument("device_port", help="instrument port, e.g. COM9")
    p.add_argument("host_port",
                   help="our end of a virtual COM pair; DMS opens the other")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--out", default="questemp_captures")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("parse", help="convert a captured Print report to CSV")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--fahrenheit", action="store_true",
                   help="keep the report's units (default: convert to degC)")

    a = ap.parse_args(argv)

    if a.cmd == "ports":
        serial_tools.list_ports()
    elif a.cmd == "listen":
        serial_tools.listen(a.port, a.baud, a.out, dtr=_onoff(a.dtr),
                            rts=_onoff(a.rts), idle_s=a.idle,
                            auto_parse=not a.no_parse, duration=a.duration,
                            echo=not a.quiet)
    elif a.cmd == "probe":
        if a.send:
            stimuli = [bytes.fromhex(h) for h in a.send]
        elif a.sweep:
            stimuli = serial_tools.byte_sweep()
        else:
            stimuli = None
        serial_tools.probe(a.port, a.baud, a.out, stimuli=stimuli,
                           wait_s=a.wait, dtr=_onoff(a.dtr),
                           rts=_onoff(a.rts), echo=not a.quiet)
    elif a.cmd == "bridge":
        serial_tools.bridge(a.device_port, a.host_port, a.baud, a.out,
                            echo=not a.quiet)
    elif a.cmd == "parse":
        rc = 0
        for f in a.files:
            text = f.read_bytes().decode("latin-1")
            sessions, rows = printout.parse(text)
            if not rows:
                print(f"{f}: no report tables found", file=sys.stderr)
                rc = 1
                continue
            lc, wc = export.write_csvs(sessions, rows, f.with_suffix(""),
                                       celsius=not a.fahrenheit)
            print(f"{f}: {len(sessions)} session(s), {len(rows)} rows -> "
                  f"{lc}, {wc}")
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
