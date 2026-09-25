"""
Instrument reader threads with automatic (re)connection, plus simulated
sources for bench testing without hardware.

A reader never exits on a serial error: it closes the port, flushes any
half-assembled sample, waits, and reopens. Unplugging and replugging a USB
cable therefore needs no intervention.
"""

import math
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .parsers import clean_line

RETRY_S = 3.0
MAX_LINE = 4096


class SerialSource:
    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.ser = None

    def open(self):
        import serial  # imported here so --simulate works without pyserial
        self.ser = serial.Serial(self.port, self.baud, timeout=1.0)
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def readline(self):
        return self.ser.readline(MAX_LINE)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def describe(self):
        return self.port


class _SimSource:
    """Yields scripted lines at a scripted rate."""

    def open(self):
        self._next = time.monotonic()

    def close(self):
        pass

    def readline(self):
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 1.0))
            if self._next > time.monotonic():
                return b""
        line, period = self._produce()
        self._next = time.monotonic() + period
        return line.encode()

    def describe(self):
        return "simulated"


class SimBeacon(_SimSource):
    """Replays the recorded capture, perturbing values, one burst per 10 s."""

    FIXTURE = (Path(__file__).resolve().parent.parent
               / "tests" / "fixtures" / "beacon_capture.txt")

    def open(self):
        super().open()
        self.lines = self.FIXTURE.read_text(encoding="utf-8").splitlines()
        self.i = 0
        self.t0 = time.monotonic()

    def _produce(self):
        line = self.lines[self.i % len(self.lines)]
        self.i += 1
        # Drift the numbers slowly so plots show something.
        phase = (time.monotonic() - self.t0) / 600.0
        off = 2.0 * math.sin(phase) + random.gauss(0, 0.05)
        if "env_hs_sampler" in line:
            line = re.sub(
                r"(TMP119|SHT3x|HDC3022|WBGT)=(-?\d+\.\d+)",
                lambda m: f"{m.group(1)}={float(m.group(2)) + off:.2f}", line)
            line = re.sub(
                r"Compensated temp: (-?\d+\.\d+)",
                lambda m: f"Compensated temp: {float(m.group(1)) + off:.3f}",
                line)
        # The fixture holds two bursts; spread its lines over 20 s so the
        # simulated burst period matches the real ~10 s.
        return line, 20.0 / len(self.lines)


class SimAnemo(_SimSource):
    """Synthetic XF502 stream at 1 Hz with gusts and deadband zeros."""

    def open(self):
        super().open()
        self.t0 = time.monotonic()
        self.header_sent = False
        self.dir = 225.0

    def _produce(self):
        if not self.header_sent:
            self.header_sent = True
            return "t_ms,wind_ms,wind_deg,temp_c,rh_pct,press_hpa", 0.1
        t = time.monotonic() - self.t0
        spd = max(0.0, 1.5 + 1.2 * math.sin(t / 90) + random.gauss(0, 0.5))
        if spd < 0.3:
            spd = 0.0
        self.dir = (self.dir + random.gauss(0, 8)) % 360
        return (f"{int(t * 1000)},{spd:.2f},{self.dir:.1f},"
                f"21.30,48.20,997.10"), 1.0


class InstrumentReader(threading.Thread):
    """Reads lines, parses them, pushes ('sample', dict) / ('note', name, str)
    tuples onto `out_q`. `state` is read by the logger for status reports."""

    def __init__(self, name, source, parser, out_q):
        super().__init__(name=f"reader-{name}", daemon=True)
        self.inst = name
        self.source = source
        self.parser = parser
        self.q = out_q
        self._halt = threading.Event()
        self.state = {"state": "starting", "port": source.describe(),
                      "detail": ""}

    def _note(self, text):
        self.q.put(("note", self.inst, text))

    def _emit(self, samples):
        for s in samples:
            self.q.put(("sample", s))

    def run(self):
        while not self._halt.is_set():
            try:
                self.source.open()
            except Exception as exc:
                msg = f"open failed: {exc}"
                if self.state["detail"] != msg:     # log once, not every retry
                    self._note(msg)
                self._set("waiting", msg)
                self._halt.wait(RETRY_S)
                continue
            self._set("connected", "")
            self._note(f"connected on {self.source.describe()}")
            try:
                while not self._halt.is_set():
                    raw = self.source.readline()
                    if not raw:
                        continue
                    line = clean_line(raw)
                    if not line:
                        continue
                    wall = datetime.now().astimezone()
                    samples, note = self.parser.feed(line, wall)
                    self._emit(samples)
                    if note:
                        self._note(note)
            except Exception as exc:
                self._set("waiting", f"read failed: {exc}")
                self._note(f"disconnected: {exc}")
            finally:
                flush = getattr(self.parser, "flush", None)
                if flush:
                    self._emit(flush())
                self.source.close()
            self._halt.wait(RETRY_S)
        self._set("stopped", "")

    def _set(self, state, detail):
        self.state = {"state": state, "port": self.source.describe(),
                      "detail": detail}

    def stop(self):
        self._halt.set()
