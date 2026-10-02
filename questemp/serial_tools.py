"""
Serial instruments for finding out what the QUESTemp 34/36 says, and when.

  listen  passive: timestamp every byte the unit sends; optionally turn each
          completed "Print" report into CSV as soon as it finishes.
  probe   active: send a list of stimuli and record what (if anything) comes
          back after each.
  bridge  man-in-the-middle between the unit and TSI DMS (through a virtual
          COM pair such as com0com), recording both directions. This is how
          the download/setup protocol can be observed and later reproduced.

Every byte is logged twice: appended raw to <stem>.<dir>.bin and as a
timestamped hex+ASCII record in <stem>.log. Nothing is interpreted on the
way in, so an unexpected binary protocol is preserved exactly.
"""

import sys
import threading
import time
from datetime import datetime
from pathlib import Path

GAP_S = 0.05        # a pause this long ends one logged chunk
CHUNK_MAX = 32      # bytes per hex line in the log


def _now():
    return datetime.now()


def hexdump(data):
    hx = " ".join(f"{b:02x}" for b in data)
    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{hx:<{CHUNK_MAX * 3}} |{asc}|"


class Recorder:
    """Thread-safe log of traffic in all directions, flushed on every write
    so that a crash or Ctrl-C loses nothing."""

    def __init__(self, outdir, stem, echo=True):
        self.dir = Path(outdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stem = stem
        self.echo = echo
        self.lock = threading.Lock()
        self.log = open(self.dir / f"{stem}.log", "a", encoding="utf-8")
        self.bins = {}

    def path(self, suffix):
        return self.dir / f"{self.stem}{suffix}"

    def note(self, text):
        line = f"{_now().isoformat(timespec='milliseconds')}  --  {text}"
        with self.lock:
            self.log.write(line + "\n")
            self.log.flush()
            if self.echo:
                print(line, flush=True)

    def data(self, direction, data, ts=None):
        ts = ts or _now()
        with self.lock:
            f = self.bins.get(direction)
            if f is None:
                safe = {"H>D": "host2dev", "D>H": "dev2host"}.get(
                    direction, direction.lower())   # '>' is illegal on Windows
                f = self.bins[direction] = open(
                    self.path(f".{safe}.bin"), "ab")
            f.write(data)
            f.flush()
            for i in range(0, len(data), CHUNK_MAX):
                piece = data[i:i + CHUNK_MAX]
                line = (f"{ts.isoformat(timespec='milliseconds')}  "
                        f"{direction:<2}  {hexdump(piece)}")
                self.log.write(line + "\n")
                if self.echo:
                    print(line, flush=True)
            self.log.flush()

    def close(self):
        with self.lock:
            self.log.close()
            for f in self.bins.values():
                f.close()


def open_port(port, baud, dtr=None, rts=None, timeout=GAP_S):
    import serial
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = timeout
    # Set the control lines before opening so the unit never sees a glitch
    # it did not ask for.
    if dtr is not None:
        ser.dtr = dtr
    if rts is not None:
        ser.rts = rts
    ser.open()
    return ser


def modem_lines(ser):
    try:
        return {"CTS": ser.cts, "DSR": ser.dsr, "CD": ser.cd, "RI": ser.ri}
    except Exception:
        return {}


def read_chunk(ser):
    """Block up to one timeout for the first byte, then gather until the
    line goes quiet for GAP_S. Returns (timestamp of first byte, bytes)."""
    first = ser.read(1)
    if not first:
        return None, b""
    ts = _now()
    buf = bytearray(first)
    while len(buf) < 4096:
        more = ser.read(ser.in_waiting or 1)
        if not more:
            break
        buf += more
    return ts, bytes(buf)


# --------------------------------------------------------------------- listen

def listen(port, baud, outdir, dtr=None, rts=None, idle_s=10.0,
           auto_parse=True, duration=None, echo=True):
    """Record everything the unit sends. When a burst of traffic ends (no
    byte for `idle_s`) and it looks like a Print report, write it to
    <stem>.reportN.txt and its parsed CSVs alongside."""
    stem = "questemp_" + _now().strftime("%Y%m%d_%H%M%S")
    rec = Recorder(outdir, stem, echo=echo)
    ser = open_port(port, baud, dtr=dtr, rts=rts)
    rec.note(f"listen {port} @ {baud} 8N1, DTR={ser.dtr} RTS={ser.rts}, "
             f"lines {modem_lines(ser)}")
    rec.note("waiting for data. Ctrl-C to stop.")
    lines = modem_lines(ser)
    burst = bytearray()
    last_rx = None
    n_reports = 0
    t_end = time.monotonic() + duration if duration else None
    try:
        while t_end is None or time.monotonic() < t_end:
            ts, data = read_chunk(ser)
            if data:
                rec.data("RX", data, ts)
                burst += data
                last_rx = time.monotonic()
            now_lines = modem_lines(ser)
            if now_lines != lines:
                rec.note(f"modem lines {lines} -> {now_lines}")
                lines = now_lines
            if burst and last_rx and time.monotonic() - last_rx > idle_s:
                rec.note(f"burst ended: {len(burst)} bytes")
                if auto_parse:
                    n_reports += _save_report(rec, bytes(burst), n_reports + 1)
                burst.clear()
    except KeyboardInterrupt:
        pass
    finally:
        if burst and auto_parse:
            _save_report(rec, bytes(burst), n_reports + 1)
        rec.note("stopped")
        ser.close()
        rec.close()
    return rec.path(".log")


def _save_report(rec, data, n):
    from . import export, printout
    text = data.decode("latin-1")
    sessions, rows = printout.parse(text)
    if not rows:
        rec.note("burst does not parse as a Print report; kept in .rx.bin only")
        return 0
    txt = rec.path(f".report{n}.txt")
    txt.write_text(text, encoding="utf-8")
    long_csv, wide_csv = export.write_csvs(sessions, rows, txt.with_suffix(""))
    rec.note(f"report: {len(sessions)} session(s), {len(rows)} rows -> "
             f"{long_csv.name}, {wide_csv.name}")
    return 1


# ---------------------------------------------------------------------- probe

# Conservative stimuli: line endings, common "identify"/"wake" requests, and
# ASCII control characters that terminals and printers use. None of these is
# a documented QUESTemp command, so all of them are guesses.
DEFAULT_STIMULI = [
    b"\r", b"\n", b"\r\n",
    b"?", b"?\r",
    b"\x05",            # ENQ
    b"\x11",            # XON  (printer flow control)
    b"\x13",            # XOFF
    b"\x06",            # ACK
    b"\x15",            # NAK
    b"\x02\x03",        # STX ETX (empty frame)
    b"\x1b",            # ESC
    b"ID\r", b"V\r", b"*IDN?\r", b"STATUS\r",
    b"\x00",
]


def probe(port, baud, outdir, stimuli=None, wait_s=1.5, dtr=None, rts=None,
          echo=True):
    """Send each stimulus, then record any reply for `wait_s`. Returns the
    list of (stimulus, reply bytes)."""
    stimuli = stimuli if stimuli is not None else DEFAULT_STIMULI
    stem = "questemp_probe_" + _now().strftime("%Y%m%d_%H%M%S")
    rec = Recorder(outdir, stem, echo=echo)
    ser = open_port(port, baud, dtr=dtr, rts=rts)
    rec.note(f"probe {port} @ {baud} 8N1, DTR={ser.dtr} RTS={ser.rts}, "
             f"lines {modem_lines(ser)}")
    results = []
    try:
        # Drain anything already in flight so it is not credited to probe 1.
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            ts, data = read_chunk(ser)
            if data:
                rec.data("RX", data, ts)
        for s in stimuli:
            rec.data("TX", s)
            ser.write(s)
            ser.flush()
            reply = bytearray()
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait_s:
                ts, data = read_chunk(ser)
                if data:
                    rec.data("RX", data, ts)
                    reply += data
                    t0 = time.monotonic()   # keep listening while it talks
            results.append((s, bytes(reply)))
    except KeyboardInterrupt:
        rec.note("interrupted")
    finally:
        answered = [(s, r) for s, r in results if r]
        rec.note(f"summary: {len(answered)}/{len(results)} stimuli got a reply")
        for s, r in answered:
            rec.note(f"  {s!r} -> {len(r)} bytes: {r[:48]!r}")
        ser.close()
        rec.close()
    return results


def byte_sweep(lo=0x00, hi=0xFF):
    return [bytes([b]) for b in range(lo, hi + 1)]


# --------------------------------------------------------------------- bridge

def bridge(device_port, host_port, baud, outdir, echo=True):
    """Forward bytes both ways between the instrument (`device_port`, e.g.
    COM9) and one end of a virtual null-modem pair (`host_port`). Point DMS
    at the other end of the pair. Everything is recorded:

        H>D   DMS -> instrument
        D>H   instrument -> DMS

    Control lines are mirrored by polling, assuming the usual com0com wiring
    (one side's DTR appears as the other's DSR, RTS as CTS)."""
    stem = "questemp_bridge_" + _now().strftime("%Y%m%d_%H%M%S")
    rec = Recorder(outdir, stem, echo=echo)
    dev = open_port(device_port, baud)
    host = open_port(host_port, baud)
    rec.note(f"bridge device={device_port} <-> host={host_port} @ {baud} 8N1")
    rec.note("start DMS on the other end of the virtual pair. Ctrl-C to stop.")
    stop = threading.Event()

    def pump(src, dst, tag):
        while not stop.is_set():
            try:
                ts, data = read_chunk(src)
                if data:
                    rec.data(tag, data, ts)
                    dst.write(data)
            except Exception as e:  # port vanished; record and stop
                rec.note(f"{tag} error: {e!r}")
                stop.set()

    threads = [threading.Thread(target=pump, args=(host, dev, "H>D"),
                                daemon=True),
               threading.Thread(target=pump, args=(dev, host, "D>H"),
                                daemon=True)]
    for t in threads:
        t.start()
    state = None
    mirror = True
    try:
        while not stop.is_set():
            if mirror:
                try:
                    new = (host.dsr, host.cts, dev.dsr, dev.cts, dev.cd)
                    if new != state:
                        # Host's DTR/RTS (seen as our DSR/CTS) -> instrument.
                        dev.dtr, dev.rts = new[0], new[1]
                        # Instrument's DSR/CTS -> host (our DTR/RTS).
                        host.dtr, host.rts = new[2], new[3]
                        rec.note(f"lines host DTR={new[0]} RTS={new[1]} | "
                                 f"device DSR={new[2]} CTS={new[3]} "
                                 f"CD={new[4]}")
                        state = new
                except OSError as e:
                    rec.note(f"control lines unavailable ({e}); "
                             f"forwarding data only")
                    mirror = False
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=1.0)
        rec.note("stopped")
        dev.close()
        host.close()
        rec.close()
    return rec.path(".log")


def list_ports():
    from serial.tools import list_ports as lp
    ports = sorted(lp.comports(), key=lambda p: p.device)
    for p in ports:
        print(f"{p.device:<14} {p.description}  [{p.hwid}]")
    if not ports:
        print("no serial ports found", file=sys.stderr)
