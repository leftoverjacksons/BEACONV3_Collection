"""
beacon-web — display server for the touchscreen (and any browser on the LAN).

Holds the last `history_hours` of data in memory: seeded from the CSV files
at startup, then kept current from the logger's UDP datagrams. Serves the
static dashboard from web/ and a small JSON API. Standard library only.

This process can crash or restart freely; it never touches the CSV.

    python3 -m beacon_station.web [--config FILE]
"""

import argparse
import bisect
import csv
import json
import math
import mimetypes
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from . import sysinfo
from .csvlog import FILE_PREFIX

WEB_ROOT = config_mod.REPO_ROOT / "web"
# deploy/kiosk.sh exits instead of relaunching the browser if this exists.
KIOSK_STOP_FLAG = "/tmp/beacon-kiosk.stop"
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

BEACON_KEYS = ("TMP119_C", "SHT3x_C", "HDC3022_C", "comp_temp_C", "WBGT_C",
               "RH_pct", "P_hPa")
# A gap longer than this (s) between plotted points is drawn as a break.
GAP_S = {"beacon": 60.0, "anemo": 10.0}
ROSE_SECTORS = 16
ROSE_CLASSES = (0.5, 1.5, 3.0, 5.0)  # m/s upper edges; last class is open


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


class Store:
    """Time-ordered in-memory history. Lists + bisect; pruned on insert."""

    def __init__(self, keep_s):
        self.keep_s = keep_s
        self.lock = threading.Lock()
        self.t = {"beacon": [], "anemo": [], "event": []}
        self.rows = {"beacon": [], "anemo": [], "event": []}
        self.latest = {"beacon": None, "anemo": None}
        self.status = None
        self.status_rx = None
        self.notes = []

    def _insert(self, src, t, row):
        ts, rs = self.t[src], self.rows[src]
        if ts and t < ts[-1]:
            i = bisect.bisect_right(ts, t)
            if (i and ts[i - 1] == t):      # duplicate (CSV seed vs live)
                return
            ts.insert(i, t)
            rs.insert(i, row)
        else:
            if ts and t == ts[-1]:
                return
            ts.append(t)
            rs.append(row)
        cut = bisect.bisect_left(ts, time.time() - self.keep_s)
        if cut > 1000 or (cut and len(ts) > 200000):
            del ts[:cut]
            del rs[:cut]

    def add_sample(self, s):
        src, t = s.get("source"), s.get("t")
        if t is None:
            return
        with self.lock:
            if src == "beacon":
                row = tuple(_num(s.get(k)) for k in BEACON_KEYS)
                self._insert("beacon", t, row)
                if (self.latest["beacon"] is None
                        or t >= self.latest["beacon"]["t"]):
                    self.latest["beacon"] = {"t": t, **{
                        k: _num(s.get(k)) for k in BEACON_KEYS}}
            elif src == "anemo":
                row = (_num(s.get("wind_ms")), _num(s.get("wind_deg")))
                self._insert("anemo", t, row)
                if (self.latest["anemo"] is None
                        or t >= self.latest["anemo"]["t"]):
                    self.latest["anemo"] = {"t": t, "wind_ms": row[0],
                                            "wind_deg": row[1]}
            elif src == "event":
                self._insert("event", t, (s.get("note", ""),))

    def add_note(self, msg):
        with self.lock:
            self.notes.append(msg)
            del self.notes[:-50]

    def set_status(self, msg):
        with self.lock:
            self.status = msg
            self.status_rx = time.time()

    # ---------------------------------------------------------- queries
    def window(self, src, t0):
        ts = self.t[src]
        i = bisect.bisect_left(ts, t0)
        return ts[i:], self.rows[src][i:]


def seed_from_csv(store, data_dir, keep_s):
    """Load recent rows from CSV files so a display restart keeps history."""
    cutoff = time.time() - keep_s
    files = sorted(Path(data_dir).glob(f"{FILE_PREFIX}*.csv"))
    files = [f for f in files if f.stat().st_mtime >= cutoff]
    n = 0
    for f in files:
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        if r.get("utc_time"):
                            t = datetime.fromisoformat(r["utc_time"]).timestamp()
                        else:   # schema-1 files: naive local time
                            t = datetime.fromisoformat(
                                r["iso_time"]).astimezone().timestamp()
                    except (ValueError, KeyError, TypeError):
                        continue    # torn last line after a power cut
                    if t < cutoff:
                        continue
                    s = {"source": r.get("source"), "t": t,
                         "note": r.get("note", ""), **r}
                    store.add_sample(s)
                    n += 1
        except OSError:
            continue
    return n


class UdpListener(threading.Thread):
    def __init__(self, port, store):
        super().__init__(name="udp", daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.store = store

    def run(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(65536)
                msg = json.loads(data)
            except (OSError, ValueError):
                continue
            kind = msg.get("type")
            if kind == "sample":
                self.store.add_sample(msg)
            elif kind == "status":
                self.store.set_status(msg)
            elif kind == "note":
                self.store.add_note(msg)


# -------------------------------------------------------------- reduction
def _circ_mean(degs):
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None
    return math.degrees(math.atan2(s, c)) % 360


def reduce_series(ts, rows, t0, t1, points, gap_s, reducer):
    """Bin rows into at most `points` bins over [t0, t1]; only non-empty
    bins are emitted, and a None row is inserted wherever consecutive
    emitted points are further apart than the gap threshold — so the
    plot shows real dropouts as breaks but not binning artefacts."""
    if not ts:
        return [], []
    width = (t1 - t0) / max(points, 1)
    raw = len(ts) <= points
    thresh = gap_s if raw else max(gap_s, 2.5 * width)
    out_t, out_r = [], []
    prev = None

    def emit(t, r):
        nonlocal prev
        if prev is not None and t - prev > thresh:
            out_t.append(prev + 1e-3)
            out_r.append(None)
        out_t.append(t)
        out_r.append(r)
        prev = t

    if raw:
        for t, r in zip(ts, rows):
            emit(t, reducer([r]))
        return out_t, out_r

    start = 0
    n = len(ts)
    while start < n:
        b = int((ts[start] - t0) // width)
        edge = t0 + (b + 1) * width
        end = bisect.bisect_left(ts, edge, start)
        end = max(end, start + 1)
        emit(t0 + (b + 0.5) * width, reducer(rows[start:end]))
        start = end
    return out_t, out_r


def _beacon_reducer(rows):
    out = []
    for i in range(len(BEACON_KEYS)):
        vals = [r[i] for r in rows if r[i] is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def _anemo_reducer(rows):
    spd = [r[0] for r in rows if r[0] is not None]
    dirs = [r[1] for r in rows if r[1] is not None and r[0]]
    if not spd:
        return [None, None, None, None]
    mean = sum(spd) / len(spd)
    gust = max(spd)
    return [mean, gust, _circ_mean(dirs) if dirs else None,
            0.0 if gust == 0.0 else None]   # whole bin in deadband


def history(store, window_s, points):
    t1 = time.time()
    t0 = t1 - window_s
    with store.lock:
        bt, br = store.window("beacon", t0)
        at, ar = store.window("anemo", t0)
        et, er = store.window("event", t0)
        bt, br = list(bt), list(br)
        at, ar = list(at), list(ar)
        events = [{"t": t, "note": r[0]} for t, r in zip(et, er)]

    bt2, br2 = reduce_series(bt, br, t0, t1, points, GAP_S["beacon"],
                             _beacon_reducer)
    at2, ar2 = reduce_series(at, ar, t0, t1, points, GAP_S["anemo"],
                             _anemo_reducer)

    def cols(rows, n):
        return [[None if r is None else r[i] for r in rows] for i in range(n)]

    b = cols(br2, len(BEACON_KEYS))
    a = cols(ar2, 4)
    return {
        "t0": t0, "t1": t1,
        "beacon": {"t": bt2, **{k: b[i] for i, k in enumerate(BEACON_KEYS)}},
        "anemo": {"t": at2, "mean": a[0], "gust": a[1], "dir": a[2],
                  "zero": a[3]},
        "events": events,
        "n_raw": {"beacon": len(bt), "anemo": len(at)},
    }


def wind_rose(store, window_s):
    t0 = time.time() - window_s
    with store.lock:
        _, rows = store.window("anemo", t0)
        rows = list(rows)
    counts = [[0] * (len(ROSE_CLASSES) + 1) for _ in range(ROSE_SECTORS)]
    calm = 0
    total = 0
    spd_sum, gust, dirs = 0.0, 0.0, []
    sector_w = 360 / ROSE_SECTORS
    for spd, d in rows:
        if spd is None:
            continue
        total += 1
        spd_sum += spd
        gust = max(gust, spd)
        if spd < ROSE_CLASSES[0] or d is None:
            calm += 1
            continue
        dirs.append(d)
        sec = int(((d + sector_w / 2) % 360) // sector_w)
        cls = bisect.bisect_right(ROSE_CLASSES, spd)
        counts[sec][min(cls, len(ROSE_CLASSES))] += 1
    return {
        "sectors": ROSE_SECTORS, "classes": ROSE_CLASSES,
        "counts": counts, "calm": calm, "total": total,
        "mean": spd_sum / total if total else None,
        "gust": gust if total else None,
        "prevailing": _circ_mean(dirs) if dirs else None,
    }


# -------------------------------------------------------------- http
class SysCache:
    """sysinfo shells out (vcgencmd, timedatectl); cache it briefly."""

    def __init__(self, data_dir, ttl=5.0):
        self.data_dir, self.ttl = data_dir, ttl
        self.val, self.at = None, 0.0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if time.time() - self.at > self.ttl:
                self.val = sysinfo.snapshot(self.data_dir)
                self.at = time.time()
            return self.val


def exit_kiosk():
    """Close the touchscreen browser and keep it closed (until the desktop
    launcher or a reboot starts deploy/kiosk.sh again)."""
    with open(KIOSK_STOP_FLAG, "w"):
        pass
    subprocess.run(["pkill", "-f", "beacon-kiosk-profile"], check=False)


def make_handler(cfg, store, syscache, readonly=False):
    """readonly=True: the public/shared port. Same dashboard, but every
    write (event markers, kiosk control) is refused and the UI hides it."""
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl_addr = ("127.0.0.1", cfg["net"]["control_port"])
    ui_cfg = {
        "stale": {n: [cfg[n]["stale_warn_s"], cfg[n]["stale_error_s"]]
                  for n in ("beacon", "anemo")},
        "enabled": {n: cfg[n]["enabled"] for n in ("beacon", "anemo")},
        "event_labels": cfg["display"]["event_labels"],
        "history_hours": cfg["display"]["history_hours"],
        "readonly": readonly,
    }
    max_window = cfg["display"]["history_hours"] * 3600

    class Handler(BaseHTTPRequestHandler):
        server_version = "beacon-web"

        def log_message(self, fmt, *args):
            pass    # keep the journal quiet; errors still surface

        def _json(self, obj, code=200):
            body = json.dumps(obj, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel):
            path = (WEB_ROOT / rel).resolve()
            if WEB_ROOT.resolve() not in path.parents or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or \
                "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)

            def qnum(name, default, lo, hi):
                try:
                    return min(max(float(qs[name][0]), lo), hi)
                except (KeyError, ValueError, IndexError):
                    return default

            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/api/latest":
                with store.lock:
                    obj = {
                        "now": time.time(),
                        "latest": dict(store.latest),
                        "status": store.status,
                        "status_rx": store.status_rx,
                        "notes": list(store.notes[-10:]),
                    }
                return self._json(obj)
            if u.path == "/api/config":
                return self._json(ui_cfg)
            if u.path == "/api/history":
                w = qnum("window", 3600, 60, max_window)
                p = int(qnum("points", 600, 50, 2000))
                return self._json(history(store, w, p))
            if u.path == "/api/wind":
                w = qnum("window", 600, 60, max_window)
                return self._json(wind_rose(store, w))
            if u.path == "/api/system":
                with store.lock:
                    notes = list(store.notes)
                return self._json({"sys": syscache.get(), "notes": notes,
                                   "config_source": cfg["_source"],
                                   "local": self._is_local()})
            self.send_error(404)

        def _is_local(self):
            return self.client_address[0] in LOOPBACK

        def do_POST(self):
            u = urlparse(self.path)
            if readonly:
                return self._json({"ok": False, "error": "view-only"}, 403)
            if u.path == "/api/kiosk/exit":
                # Only the Pi's own screen may close its kiosk.
                if not self._is_local():
                    return self._json({"ok": False, "error":
                                       "only from the Pi's own screen"}, 403)
                exit_kiosk()
                return self._json({"ok": True})
            if u.path != "/api/event":
                return self.send_error(404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                msg = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                label = str(msg.get("label", "")).strip()[:200]
            except (ValueError, TypeError):
                return self._json({"ok": False, "error": "bad request"}, 400)
            if not label:
                return self._json({"ok": False, "error": "empty label"}, 400)
            try:
                ctl.sendto(json.dumps({"cmd": "event", "label": label})
                           .encode(), ctl_addr)
            except OSError as exc:
                return self._json({"ok": False, "error": str(exc)}, 503)
            return self._json({"ok": True})

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)
    keep_s = cfg["display"]["history_hours"] * 3600

    store = Store(keep_s)
    # Listen first, then seed: rows arriving meanwhile are de-duplicated.
    UdpListener(cfg["net"]["data_port"], store).start()
    n = seed_from_csv(store, cfg["logging"]["data_dir"], keep_s)
    print(f"seeded {n} rows from {cfg['logging']['data_dir']}", flush=True)

    syscache = SysCache(cfg["logging"]["data_dir"])
    host = cfg["net"]["web_host"]
    srv = ThreadingHTTPServer((host, cfg["net"]["web_port"]),
                              make_handler(cfg, store, syscache))
    srv.daemon_threads = True
    print(f"serving on http://{host}:{cfg['net']['web_port']}/", flush=True)

    ro_port = cfg["net"].get("readonly_port") or 0
    if ro_port:
        ro = ThreadingHTTPServer((host, ro_port),
                                 make_handler(cfg, store, syscache,
                                              readonly=True))
        ro.daemon_threads = True
        threading.Thread(target=ro.serve_forever, name="readonly",
                         daemon=True).start()
        print(f"view-only on http://{host}:{ro_port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
