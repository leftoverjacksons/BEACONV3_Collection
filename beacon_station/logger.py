"""
beacon-logger — headless acquisition service.

Reads both instruments, writes the CSV, and publishes every sample plus a
periodic status report as JSON datagrams to the display process on
localhost. Publishing is fire-and-forget UDP: the logger never waits on,
or fails because of, the display.

Operator event markers arrive the other way (display -> control port) and
are written through the same path as samples, so the CSV has one writer.

    python3 -m beacon_station.logger [--config FILE] [--simulate]
"""

import argparse
import json
import queue
import signal
import socket
import sys
import threading
import time
from datetime import datetime

from . import config as config_mod
from .csvlog import RotatingCsv
from .parsers import AnemoParser, BeaconParser
from .serial_io import InstrumentReader, SerialSource, SimAnemo, SimBeacon

STATUS_PERIOD_S = 2.0


def log(msg):
    # stdout goes to the systemd journal: `journalctl -u beacon-logger`
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def sample_to_wire(s):
    d = {k: v for k, v in s.items() if k != "wall"}
    # Truncate to ms exactly as the CSV does, so the display can
    # de-duplicate live samples against rows it re-reads from disk.
    w = s["wall"]
    d["t"] = w.replace(microsecond=w.microsecond // 1000 * 1000).timestamp()
    return d


class ControlListener(threading.Thread):
    """Receives {"cmd": "event", "label": "..."} from the display."""

    def __init__(self, port, out_q):
        super().__init__(name="control", daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(1.0)
        self.q = out_q
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if msg.get("cmd") == "event":
                self.q.put(("sample", {
                    "source": "event",
                    "wall": datetime.now().astimezone(),
                    "note": str(msg.get("label", ""))[:200],
                }))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config")
    ap.add_argument("--simulate", action="store_true",
                    help="use synthetic instruments instead of serial ports")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)
    log(f"config: {cfg['_source']}")

    q = queue.Queue()
    readers = {}
    for name, parser_cls, sim_cls in (("beacon", BeaconParser, SimBeacon),
                                      ("anemo", AnemoParser, SimAnemo)):
        c = cfg[name]
        if not c["enabled"]:
            continue
        src = sim_cls() if args.simulate else SerialSource(c["port"],
                                                            c["baud"])
        readers[name] = InstrumentReader(name, src, parser_cls(), q)

    run_info = {
        "simulated": args.simulate,
        "instruments": {n: {"port": r.source.describe(),
                            "baud": cfg[n]["baud"]}
                        for n, r in readers.items()},
    }
    csvlog = RotatingCsv(cfg["logging"]["data_dir"],
                         cfg["logging"]["fsync_interval_s"], run_info)

    pub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pub_addr = ("127.0.0.1", cfg["net"]["data_port"])

    def publish(obj):
        try:
            pub.sendto(json.dumps(obj).encode(), pub_addr)
        except OSError:
            pass  # display not running — irrelevant to logging

    control = ControlListener(cfg["net"]["control_port"], q)

    halt = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: halt.set())

    counts = {"beacon": 0, "anemo": 0, "event": 0}
    last_t = {"beacon": None, "anemo": None, "event": None}
    started = time.time()

    control.start()
    for r in readers.values():
        r.start()
    log(f"logging to {cfg['logging']['data_dir']}"
        + (" [SIMULATED]" if args.simulate else ""))

    next_status = 0.0
    try:
        while not halt.is_set():
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                item = None

            if item is not None and item[0] == "sample":
                s = item[1]
                try:
                    csvlog.write(s)
                except OSError as exc:
                    # Disk full / SD failure. Keep running so the display
                    # shows it; systemd would just restart into the same.
                    log(f"CSV write failed: {exc}")
                counts[s["source"]] += 1
                last_t[s["source"]] = s["wall"].timestamp()
                publish({"type": "sample", **sample_to_wire(s)})
                if s["source"] == "event":
                    log(f"event: {s['note']}")
            elif item is not None and item[0] == "note":
                _, inst, text = item
                log(f"{inst}: {text}")
                publish({"type": "note", "inst": inst, "text": text,
                         "t": time.time()})

            if time.monotonic() >= next_status:
                next_status = time.monotonic() + STATUS_PERIOD_S
                publish({
                    "type": "status",
                    "t": time.time(),
                    "started": started,
                    "simulated": args.simulate,
                    "file": csvlog.path.name if csvlog.path else None,
                    "file_rows": csvlog.rows,
                    "counts": counts,
                    "last_t": last_t,
                    "readers": {n: r.state for n, r in readers.items()},
                })
    finally:
        log("stopping")
        for r in readers.values():
            r.stop()
        for r in readers.values():
            r.join(timeout=3)
        control.halt.set()
        # Drain what the readers flushed on the way out.
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "sample":
                try:
                    csvlog.write(item[1])
                except OSError:
                    pass
        csvlog.close()
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
