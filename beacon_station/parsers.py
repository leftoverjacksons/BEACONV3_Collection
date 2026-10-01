"""
Line parsers for the two instruments. Pure functions/classes, no I/O, so
they can be tested against captured logs (tests/fixtures/).

BEACON V3 — Zephyr console stream. Three lines of one sampler burst are
used; everything else (noise_proc, soc_estimator, EMA/RoC/Deltas, modem,
dbg) is ignored. Current firmware (module therm_meas_sampler):

    [00:00:12.514,000] <inf> therm_meas_sampler: Raw: TMP119=23.75
        SHT3x=26.92 HDC3022=24.81 RH=48.76 SHT3x_RH=47.04 P=978.59 hPa
    [00:00:12.514,000] <inf> therm_meas_sampler: Compensated temp: 24.203 degC ...
    [00:00:12.515,000] <inf> therm_meas_sampler: HS Sample: T=75.57 deg F,
                        24.20 deg C H=48.76 WBGT=19.41

Earlier firmware called the module env_hs_sampler and had no SHT3x_RH. The
lines are matched by content, not module name, and the Raw line is read as
name=value pairs, so a renamed module or an added field doesn't break
parsing; fields missing from a given firmware are logged blank.

  RH        humidity in the ventilated sensing region (HDC3022) — ambient
  SHT3x_RH  humidity at the PCB-mounted SHT3x, i.e. INSIDE THE HOUSING;
            not ambient. Logged as SHT3x_RH_pct.

A burst opens at 'Raw:' and is emitted when 'HS Sample:' arrives. If a burst
is truncated it is flushed as-is when the next 'Raw:' begins, with missing
fields left as None. All beacon temperatures are native degrees C.

The Zephyr uptime prefix [HHH:MM:SS.mmm,uuu] of the 'Raw:' line is kept as
dev_uptime_s — the device's own clock, independent of USB/host latency.

ANEMOMETER — XF502/AB via the Teensy RS-485 bridge, CSV:

    t_ms,wind_ms,wind_deg,temp_c,rh_pct,press_hpa

'#' lines are diagnostics. Rows with empty wind fields (sensor timeouts) are
dropped. Exact 0.00 m/s is the sensor's firmware deadband (< ~0.3 m/s).
"""

import re

# ANSI SGR escapes. Some terminals (e.g. the Arduino serial monitor) drop the
# ESC byte and leave the bare '[0m' / '[1;33m' behind, so ESC is optional.
ANSI_RE = re.compile(r"\x1b?\[[0-9;]*m")

UPTIME_RE = re.compile(r"\[(\d+):(\d{2}):(\d{2})\.(\d{3}),(\d{3})\]")

_NUM = r"(-?\d+\.?\d*)"
# Any module name ("[\w.]+:") — the content after it is what identifies the line.
RAW_RE = re.compile(r"[\w.]+:\s*Raw:\s*(?=TMP119=)")
KV_RE = re.compile(rf"(\w+)={_NUM}")
COMP_RE = re.compile(rf"[\w.]+:\s*Compensated temp:\s*{_NUM}\s*degC")
WBGT_RE = re.compile(rf"[\w.]+:\s*HS Sample:.*WBGT={_NUM}")

# Raw-line key -> logged field
RAW_KEYS = {
    "TMP119": "TMP119_C",
    "SHT3x": "SHT3x_C",
    "HDC3022": "HDC3022_C",
    "RH": "RH_pct",
    "SHT3x_RH": "SHT3x_RH_pct",     # inside the housing, not ambient
    "P": "P_hPa",
}
BEACON_FIELDS = ("TMP119_C", "SHT3x_C", "HDC3022_C", "RH_pct", "P_hPa",
                 "comp_temp_C", "WBGT_C", "SHT3x_RH_pct")


def clean_line(raw):
    """bytes|str -> stripped text with ANSI escapes removed."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return ANSI_RE.sub("", raw).strip()


def _uptime_before(line, pos):
    """Seconds of the last Zephyr uptime stamp preceding `pos`, else None.

    'Last before the match' rather than 'start of line' because the console
    occasionally interleaves two log lines into one."""
    last = None
    for m in UPTIME_RE.finditer(line, 0, pos):
        last = m
    if last is None:
        return None
    h, mi, s, ms, us = (int(g) for g in last.groups())
    return h * 3600 + mi * 60 + s + ms / 1e3 + us / 1e6


class BeaconParser:
    """Stateful burst assembler. feed() returns ([finished samples], None).

    `wall` is supplied by the caller (host timestamp taken when the 'Raw:'
    line was read); it is carried through untouched."""

    def __init__(self):
        self.pending = None

    def feed(self, line, wall):
        return self._feed(line, wall), None

    def _feed(self, line, wall):
        out = []
        m = RAW_RE.search(line)
        if m:
            if self.pending is not None:     # previous burst truncated
                out.append(self.pending)
            # Read only up to the next log line, in case the console
            # interleaved one onto the end of this one.
            rest = re.split(r"[\[<]", line[m.end():], maxsplit=1)[0]
            vals = {k: float(v) for k, v in KV_RE.findall(rest)}
            self.pending = {
                "source": "beacon",
                "wall": wall,
                "dev_uptime_s": _uptime_before(line, m.start()),
                **{f: None for f in BEACON_FIELDS},
                **{f: vals.get(k) for k, f in RAW_KEYS.items()},
            }
            return out

        if self.pending is None:
            return out

        m = COMP_RE.search(line)
        if m:
            self.pending["comp_temp_C"] = float(m.group(1))
            return out

        m = WBGT_RE.search(line)
        if m:
            self.pending["WBGT_C"] = float(m.group(1))
            out.append(self.pending)         # burst complete
            self.pending = None
        return out

    def flush(self):
        """Emit a half-assembled burst (on disconnect/shutdown)."""
        out = [self.pending] if self.pending is not None else []
        self.pending = None
        return out


class AnemoParser:
    """Stateless parser for the Teensy CSV stream.

    feed() returns ([samples], note_or_None)."""

    def feed(self, line, wall):
        if not line:
            return [], None
        if line.startswith("#"):
            return [], line.lstrip("# ").strip()
        if line.startswith("t_ms"):
            return [], None
        parts = line.split(",")
        if len(parts) < 3:
            return [], None
        try:
            wind_ms = float(parts[1])
            wind_deg = float(parts[2])
        except ValueError:
            return [], None                  # timeout rows: empty wind fields
        return [{
            "source": "anemo",
            "wall": wall,
            "wind_ms": wind_ms,
            "wind_deg": wind_deg,
        }], None
