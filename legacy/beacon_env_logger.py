#!/usr/bin/env python3
"""
beacon_env_logger.py — combined live listener for BEACON V3 (Zephyr log
stream) and the XinFeng XF502/AB ultrasonic anemometer (Teensy RS-485
bridge), with live PyQtGraph display and a single sparse CSV log.

[Kept verbatim as the original laptop tool. The Raspberry Pi station in
beacon_station/ supersedes it; its CSV keeps these 11 columns first.]

BEACON (default COM3), from the Zephyr console stream. Lines used:

    <inf> env_hs_sampler: Raw: TMP119=29.88 SHT3x=36.77 HDC3022=33.26
                               RH=62.64 P=989.65 hPa
    <inf> env_hs_sampler: Compensated temp: 30.439 degC (86.790 degF)
    <inf> env_hs_sampler: HS Sample: T=86.79 deg F, 30.44 deg C H=62.64
                               WBGT=26.53

Everything else (noise_proc, soc_estimator, EMA/RoC/Deltas, dbg) is
ignored. ANSI color escapes are stripped. All beacon temperatures are
native degrees C in this stream.

The three lines of one sampler burst arrive within ~1 ms. A burst is
buffered starting at 'Raw:' and emitted as one sample when 'HS Sample:'
arrives; if a burst is truncated, it is flushed as-is when the next
'Raw:' begins, with the missing fields left blank.

ANEMOMETER (default COM6), from the Teensy sketch's CSV stream:

    t_ms,wind_ms,wind_deg,temp_c,rh_pct,press_hpa

'#' lines are diagnostics and go to the status bar. Exact 0.00 m/s
readings are the sensor's firmware deadband (< ~0.3 m/s), not a
measurement, and are flagged visually.

UNITS: the display — plots, axis label and the live readout — is in
°F. The CSV keeps the stream's native °C (columns still named *_C), so
the logged file stays a faithful record of what the device reported and
stays readable by beacon_replay_qt.py, which converts on load.

CSV: one row per measurement event from either instrument, host
timestamped, 'source' column identifying the origin. Columns not
belonging to that instrument are left blank deliberately — no
forward-filling. Align downstream with merge_asof/resampling.

Requires: pip install pyserial pyqtgraph PyQt6
"""

import csv
import math
import re
import sys
import time
from collections import deque
from datetime import datetime

import numpy as np
import pyqtgraph as pg
import serial
import serial.tools.list_ports
from PyQt6 import QtCore, QtGui, QtWidgets

# ----------------------------------------------------------------- config
BEACON_PORT_DEFAULT = "COM3"
ANEMO_PORT_DEFAULT = "COM6"
BAUD = 115200

BUFFER_SAMPLES = 20000          # per-channel plot history
PLOT_WINDOW_S = 1200            # rolling display window
REDRAW_HZ = 5

CSV_FILENAME = f"beacon_env_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
CSV_COLUMNS = [
    "iso_time", "source",
    "TMP119_C", "SHT3x_C", "HDC3022_C", "RH_pct", "P_hPa",
    "comp_temp_C", "WBGT_C",
    "wind_ms", "wind_deg",
]

COLORS = {
    "TMP119":  "#1F77B4",
    "SHT3x":   "#2CA02C",
    "HDC3022": "#8C564B",
    "comp":    "#D62728",
    "WBGT":    "#FF7F0E",
    "RH":      "#4FC3F7",
    "wind":    "#1D9E75",
    "zero":    "#D85A30",
    "dir":     "#7F77DD",
    "grid":    "#888780",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\[0m")

RAW_RE = re.compile(
    r"env_hs_sampler:\s*Raw:\s*"
    r"TMP119=(-?\d+\.?\d*)\s+SHT3x=(-?\d+\.?\d*)\s+HDC3022=(-?\d+\.?\d*)\s+"
    r"RH=(-?\d+\.?\d*)\s+P=(-?\d+\.?\d*)"
)
COMP_RE = re.compile(r"Compensated temp:\s*(-?\d+\.?\d*)\s*degC")
WBGT_RE = re.compile(r"HS Sample:.*WBGT=(-?\d+\.?\d*)")


def c_to_f(c):
    """Celsius -> Fahrenheit, NaN- and None-safe."""
    if c is None:
        return float("nan")
    try:
        v = float(c)
    except (TypeError, ValueError):
        return float("nan")
    if v != v:
        return v
    return v * 9.0 / 5.0 + 32.0


# ----------------------------------------------------------------- readers
class BeaconReader(QtCore.QThread):
    """Parses the Zephyr log stream, assembles sampler bursts."""

    sample = QtCore.pyqtSignal(dict)
    note = QtCore.pyqtSignal(str)
    disconnected = QtCore.pyqtSignal(str)

    def __init__(self, port, parent=None):
        super().__init__(parent)
        self.port = port
        self._running = False

    def run(self):
        self._running = True
        try:
            ser = serial.Serial(self.port, BAUD, timeout=1.0)
        except serial.SerialException as exc:
            self.disconnected.emit(f"beacon open failed: {exc}")
            return
        time.sleep(0.2)
        ser.reset_input_buffer()
        self.note.emit(f"beacon connected on {self.port}")

        pending = None  # burst under assembly

        def flush(p):
            if p is not None:
                self.sample.emit(p)

        while self._running:
            try:
                raw = ser.readline()
            except serial.SerialException as exc:
                self.disconnected.emit(f"beacon read failed: {exc}")
                break
            if not raw:
                continue
            line = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).strip()
            if not line:
                continue

            m = RAW_RE.search(line)
            if m:
                flush(pending)  # previous burst truncated — emit what we had
                pending = {
                    "wall": datetime.now(),
                    "TMP119_C": float(m.group(1)),
                    "SHT3x_C": float(m.group(2)),
                    "HDC3022_C": float(m.group(3)),
                    "RH_pct": float(m.group(4)),
                    "P_hPa": float(m.group(5)),
                    "comp_temp_C": None,
                    "WBGT_C": None,
                }
                continue

            m = COMP_RE.search(line)
            if m and pending is not None:
                pending["comp_temp_C"] = float(m.group(1))
                continue

            m = WBGT_RE.search(line)
            if m and pending is not None:
                pending["WBGT_C"] = float(m.group(1))
                flush(pending)  # burst complete
                pending = None

        flush(pending)
        try:
            ser.close()
        except Exception:
            pass
        self.note.emit("beacon serial closed")

    def stop(self):
        self._running = False
        self.wait(2000)


class AnemoReader(QtCore.QThread):
    """Parses the Teensy XF502 CSV stream."""

    sample = QtCore.pyqtSignal(dict)
    note = QtCore.pyqtSignal(str)
    disconnected = QtCore.pyqtSignal(str)

    def __init__(self, port, parent=None):
        super().__init__(parent)
        self.port = port
        self._running = False

    def run(self):
        self._running = True
        try:
            ser = serial.Serial(self.port, BAUD, timeout=1.0)
        except serial.SerialException as exc:
            self.disconnected.emit(f"anemometer open failed: {exc}")
            return
        time.sleep(0.2)
        ser.reset_input_buffer()
        self.note.emit(f"anemometer connected on {self.port}")

        while self._running:
            try:
                raw = ser.readline()
            except serial.SerialException as exc:
                self.disconnected.emit(f"anemometer read failed: {exc}")
                break
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("#"):
                self.note.emit(line.lstrip("# ").strip())
                continue
            if line.startswith("t_ms"):
                continue

            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                wind_ms = float(parts[1])
                wind_deg = float(parts[2])
            except (ValueError, IndexError):
                continue  # timeout rows have empty wind fields

            self.sample.emit({
                "wall": datetime.now(),
                "wind_ms": wind_ms,
                "wind_deg": wind_deg,
            })

        try:
            ser.close()
        except Exception:
            pass
        self.note.emit("anemometer serial closed")

    def stop(self):
        self._running = False
        self.wait(2000)


# ----------------------------------------------------------------- window
class Monitor(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BEACON V3 + XF502 environmental logger")
        self.resize(1360, 860)

        self.beacon = None
        self.anemo = None
        self.t_start = time.monotonic()

        # beacon buffers
        self.b_t = deque(maxlen=BUFFER_SAMPLES)
        # Plot buffers hold display units: temperatures in °F, RH in %.
        # The CSV keeps the stream's native °C — see on_beacon().
        self.b = {k: deque(maxlen=BUFFER_SAMPLES) for k in
                  ("TMP119_C", "SHT3x_C", "HDC3022_C",
                   "comp_temp_C", "WBGT_C", "RH_pct")}
        # anemometer buffers
        self.a_t = deque(maxlen=BUFFER_SAMPLES)
        self.a_spd = deque(maxlen=BUFFER_SAMPLES)
        self.a_dir = deque(maxlen=BUFFER_SAMPLES)

        self.n_beacon = 0
        self.n_anemo = 0

        # CSV — always on, opened at launch
        self.csv_file = open(CSV_FILENAME, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(CSV_COLUMNS)
        self.csv_file.flush()

        self._build_ui()
        self.status.showMessage(f"logging to {CSV_FILENAME}")

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.redraw)
        self.timer.start(int(1000 / REDRAW_HZ))

    # ------------------------------------------------------------- ui
    def _build_ui(self):
        pg.setConfigOptions(antialias=True, background=None,
                            foreground=COLORS["grid"])

        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addLayout(self._build_controls())
        outer.addWidget(self._build_plots(), 1)
        outer.addLayout(self._build_readouts())
        self.setCentralWidget(central)
        self.status = self.statusBar()

    def _build_controls(self):
        row = QtWidgets.QHBoxLayout()
        ports = [p.device for p in serial.tools.list_ports.comports()]

        def port_combo(default):
            box = QtWidgets.QComboBox()
            box.setEditable(True)
            box.setMinimumWidth(110)
            box.addItems(ports or [default])
            box.setCurrentText(default)
            return box

        row.addWidget(QtWidgets.QLabel("BEACON"))
        self.b_port = port_combo(BEACON_PORT_DEFAULT)
        row.addWidget(self.b_port)
        self.b_btn = QtWidgets.QPushButton("Connect")
        self.b_btn.clicked.connect(self.toggle_beacon)
        row.addWidget(self.b_btn)

        row.addSpacing(24)
        row.addWidget(QtWidgets.QLabel("Anemometer"))
        self.a_port = port_combo(ANEMO_PORT_DEFAULT)
        row.addWidget(self.a_port)
        self.a_btn = QtWidgets.QPushButton("Connect")
        self.a_btn.clicked.connect(self.toggle_anemo)
        row.addWidget(self.a_btn)

        row.addSpacing(24)
        rescan = QtWidgets.QPushButton("Rescan ports")
        rescan.clicked.connect(self.refresh_ports)
        row.addWidget(rescan)

        row.addStretch(1)
        return row

    def _build_plots(self):
        gl = pg.GraphicsLayoutWidget()

        # temperatures
        self.p_temp = gl.addPlot(row=0, col=0)
        self.p_temp.setLabel("left", "Temperature (°F)")
        self.p_temp.showGrid(x=True, y=True, alpha=0.15)
        self.p_temp.addLegend(offset=(-10, 10), colCount=3)
        self.c_temp = {}
        for key, label, width, style in (
            ("TMP119_C", "TMP119", 1.2, QtCore.Qt.PenStyle.SolidLine),
            ("SHT3x_C", "SHT3x", 1.2, QtCore.Qt.PenStyle.SolidLine),
            ("HDC3022_C", "HDC3022", 1.2, QtCore.Qt.PenStyle.SolidLine),
            ("comp_temp_C", "compensated", 2.2, QtCore.Qt.PenStyle.SolidLine),
            ("WBGT_C", "WBGT (fw, °F)", 2.0, QtCore.Qt.PenStyle.DashLine),
        ):
            color = COLORS[key.split("_")[0]] if key.split("_")[0] in COLORS \
                else COLORS["comp" if key.startswith("comp") else "WBGT"]
            self.c_temp[key] = self.p_temp.plot(
                pen=pg.mkPen(color, width=width, style=style), name=label)

        # RH strip
        self.p_rh = gl.addPlot(row=1, col=0)
        self.p_rh.setLabel("left", "RH", units="%")
        self.p_rh.setYRange(0, 100)
        self.p_rh.showGrid(x=True, y=True, alpha=0.15)
        self.c_rh = self.p_rh.plot(pen=pg.mkPen(COLORS["RH"], width=1.3))

        # wind speed
        self.p_wind = gl.addPlot(row=2, col=0)
        self.p_wind.setLabel("left", "Wind", units="m/s")
        self.p_wind.showGrid(x=True, y=True, alpha=0.15)
        self.c_wind = self.p_wind.plot(pen=pg.mkPen(COLORS["wind"], width=1.2))
        self.s_zero = pg.ScatterPlotItem(
            size=6, brush=pg.mkBrush(COLORS["zero"]), pen=None)
        self.p_wind.addItem(self.s_zero)

        # wind direction (scatter — no line across the 360/0 wrap)
        self.p_dir = gl.addPlot(row=3, col=0)
        self.p_dir.setLabel("left", "Dir")
        self.p_dir.setLabel("bottom", "Elapsed", units="s")
        self.p_dir.setYRange(0, 360)
        self.p_dir.getAxis("left").setTicks([[
            (0, "N"), (90, "E"), (180, "S"), (270, "W"), (360, "N")]])
        self.p_dir.showGrid(x=True, y=True, alpha=0.15)
        self.s_dir = pg.ScatterPlotItem(
            size=5, brush=pg.mkBrush(COLORS["dir"]), pen=None)
        self.p_dir.addItem(self.s_dir)

        for p in (self.p_rh, self.p_wind, self.p_dir):
            p.setXLink(self.p_temp)
        gl.ci.layout.setRowStretchFactor(0, 4)
        gl.ci.layout.setRowStretchFactor(1, 1)
        gl.ci.layout.setRowStretchFactor(2, 2)
        gl.ci.layout.setRowStretchFactor(3, 1)
        return gl

    def _build_readouts(self):
        row = QtWidgets.QHBoxLayout()
        font = QtGui.QFont("Consolas")
        font.setStyleHint(QtGui.QFont.StyleHint.Monospace)

        self.lbl_beacon = QtWidgets.QLabel("BEACON: no data")
        self.lbl_beacon.setFont(font)
        self.lbl_anemo = QtWidgets.QLabel("ANEMO: no data")
        self.lbl_anemo.setFont(font)
        row.addWidget(self.lbl_beacon)
        row.addSpacing(30)
        row.addWidget(self.lbl_anemo)
        row.addStretch(1)
        return row

    # ------------------------------------------------------------- serial
    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        for box in (self.b_port, self.a_port):
            current = box.currentText()
            box.clear()
            box.addItems(ports or [current])
            box.setCurrentText(current)

    def toggle_beacon(self):
        if self.beacon and self.beacon.isRunning():
            self.beacon.stop()
            self.beacon = None
            self.b_btn.setText("Connect")
            return
        self.beacon = BeaconReader(self.b_port.currentText().strip())
        self.beacon.sample.connect(self.on_beacon)
        self.beacon.note.connect(self.status.showMessage)
        self.beacon.disconnected.connect(self._beacon_down)
        self.beacon.start()
        self.b_btn.setText("Disconnect")

    def _beacon_down(self, msg):
        self.status.showMessage(msg)
        self.b_btn.setText("Connect")
        self.beacon = None

    def toggle_anemo(self):
        if self.anemo and self.anemo.isRunning():
            self.anemo.stop()
            self.anemo = None
            self.a_btn.setText("Connect")
            return
        self.anemo = AnemoReader(self.a_port.currentText().strip())
        self.anemo.sample.connect(self.on_anemo)
        self.anemo.note.connect(self.status.showMessage)
        self.anemo.disconnected.connect(self._anemo_down)
        self.anemo.start()
        self.a_btn.setText("Disconnect")

    def _anemo_down(self, msg):
        self.status.showMessage(msg)
        self.a_btn.setText("Connect")
        self.anemo = None

    # ------------------------------------------------------------- data
    def _fmt(self, v, spec=".2f"):
        return "" if v is None else format(v, spec)

    def on_beacon(self, s):
        elapsed = time.monotonic() - self.t_start
        self.b_t.append(elapsed)
        for k in self.b:
            v = s.get(k)
            if k == "RH_pct":
                self.b[k].append(float("nan") if v is None else v)
            else:
                self.b[k].append(c_to_f(v))
        self.n_beacon += 1

        self.csv_writer.writerow([
            s["wall"].isoformat(timespec="milliseconds"), "beacon",
            self._fmt(s["TMP119_C"]), self._fmt(s["SHT3x_C"]),
            self._fmt(s["HDC3022_C"]), self._fmt(s["RH_pct"]),
            self._fmt(s["P_hPa"]), self._fmt(s["comp_temp_C"], ".3f"),
            self._fmt(s["WBGT_C"]),
            "", "",
        ])
        self.csv_file.flush()

        comp = c_to_f(s["comp_temp_C"])
        wbgt = c_to_f(s["WBGT_C"])
        self.lbl_beacon.setText(
            f"BEACON {s['wall']:%H:%M:%S}  "
            f"TMP={c_to_f(s['TMP119_C']):.2f}°F  "
            f"SHT={c_to_f(s['SHT3x_C']):.2f}°F  "
            f"HDC={c_to_f(s['HDC3022_C']):.2f}°F  "
            f"RH={s['RH_pct']:.1f}%  "
            f"P={s['P_hPa']:.1f} hPa  "
            f"comp={'—' if comp != comp else f'{comp:.2f}°F'}  "
            f"WBGT={'—' if wbgt != wbgt else f'{wbgt:.2f}°F'}  "
            f"n={self.n_beacon}")

    def on_anemo(self, s):
        elapsed = time.monotonic() - self.t_start
        self.a_t.append(elapsed)
        self.a_spd.append(s["wind_ms"])
        self.a_dir.append(s["wind_deg"])
        self.n_anemo += 1

        self.csv_writer.writerow([
            s["wall"].isoformat(timespec="milliseconds"), "anemo",
            "", "", "", "", "", "", "",
            f"{s['wind_ms']:.2f}", f"{s['wind_deg']:.1f}",
        ])
        self.csv_file.flush()

        self.lbl_anemo.setText(
            f"ANEMO {s['wall']:%H:%M:%S}  "
            f"{s['wind_ms']:5.2f} m/s  {s['wind_deg']:5.1f}°  "
            f"n={self.n_anemo}")

    # ------------------------------------------------------------- draw
    def redraw(self):
        now = time.monotonic() - self.t_start
        x0 = max(0.0, now - PLOT_WINDOW_S)

        if self.b_t:
            t = np.fromiter(self.b_t, float)
            keep = t >= x0
            for k, curve in self.c_temp.items():
                y = np.fromiter(self.b[k], float)
                curve.setData(t[keep], y[keep], connect="finite")
            rh = np.fromiter(self.b["RH_pct"], float)
            self.c_rh.setData(t[keep], rh[keep], connect="finite")

        if self.a_t:
            t = np.fromiter(self.a_t, float)
            s = np.fromiter(self.a_spd, float)
            d = np.fromiter(self.a_dir, float)
            keep = t >= x0
            t, s, d = t[keep], s[keep], d[keep]
            self.c_wind.setData(t, s)
            zero = s == 0.0
            self.s_zero.setData(t[zero], s[zero])
            self.s_dir.setData(t[~zero], d[~zero])

        if self.b_t or self.a_t:
            self.p_temp.setXRange(x0, max(now, x0 + 10), padding=0.01)

    def closeEvent(self, event):
        if self.beacon:
            self.beacon.stop()
        if self.anemo:
            self.anemo.stop()
        self.csv_file.close()
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = Monitor()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
