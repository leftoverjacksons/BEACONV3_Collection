"""
Line parsers for the two instruments. Pure functions/classes, no I/O, so
they can be tested against captured logs (tests/fixtures/).

BEACON V3 — Zephyr console stream. Three lines of one env_hs_sampler burst
are used; everything else (noise_proc, soc_estimator, EMA/RoC/Deltas, modem,
dbg) is ignored:

    [66:28:00.919,000] <inf> env_hs_sampler: Raw: TMP119=23.45 SHT3x=27.19
                        HDC3022=24.66 RH=50.41 P=997.19 hPa
    [66:28:00.919,000] <inf> env_hs_sampler: Compensated temp: 24.113 degC ...
    [66:28:00.920,000] <inf> env_hs_sampler: HS Sample: T=75.40 deg F,
                        24.11 deg C H=50.41 WBGT=19.53

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
RAW_RE = re.compile(
    r"env_hs_sampler:\s*Raw:\s*"
    rf"TMP119={_NUM}\s+SHT3x={_NUM}\s+HDC3022={_NUM}\s+"
    rf"RH={_NUM}\s+P={_NUM}"
)
COMP_RE = re.compile(rf"env_hs_sampler:\s*Compensated temp:\s*{_NUM}\s*degC")
WBGT_RE = re.compile(rf"env_hs_sampler:\s*HS Sample:.*WBGT={_NUM}")

BEACON_FIELDS = ("TMP119_C", "SHT3x_C", "HDC3022_C", "RH_pct", "P_hPa",
                 "comp_temp_C", "WBGT_C")


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
            self.pending = {
                "source": "beacon",
                "wall": wall,
                "dev_uptime_s": _uptime_before(line, m.start()),
                "TMP119_C": float(m.group(1)),
                "SHT3x_C": float(m.group(2)),
                "HDC3022_C": float(m.group(3)),
                "RH_pct": float(m.group(4)),
                "P_hPa": float(m.group(5)),
                "comp_temp_C": None,
                "WBGT_C": None,
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
