"""
Parser for the QUESTemp 34/36 "Print" report (the ASCII dump the unit sends
at 9600 baud when Print is started from its menu).

The report is a set of pages. Page 1 holds the session header (start date,
units, logging interval, maxima); each later page is one table for one
sensor bar, or for the weighted average:

    Session: 3                                         Page 2
    Sensor: 1
    Degrees Fahrenheit
    TIME    WBGTi WBGTo    WET    DRY    GLOBE     RH   HI   FLOW  L  M  H  VH
    ----- ----- ----- ...
    11:08   68.7    67.9   59.4   82.4      90.7   13   0    0.5  60 60 60 60

Rows carry HH:MM only. The date comes from the session's "Start:" line and
is advanced by a day whenever the clock goes backwards. A long session is
split over many pages that repeat the "Session:"/"Sensor:" header, so the
clock is tracked per (session, sensor) across pages.
Columns are taken from each table's own header, so tables printed without
FLOW or without stay times parse the same way.
"""

import re
from datetime import date, datetime, timedelta

MONTHS = {m: i for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), start=1)}

# Printed column name -> output field. Temperatures are in the report's units.
COLUMN_MAP = {
    "WBGTi": "wbgt_in",
    "WBGTo": "wbgt_out",
    "WET": "wet_bulb",
    "DRY": "dry_bulb",
    "GLOBE": "globe",
    "RH": "rh_pct",
    "HI": "heat_index",
    "HX": "humidex",
    "FLOW": "flow_mps",
    "L": "stay_light_min",
    "M": "stay_moderate_min",
    "H": "stay_heavy_min",
    "VH": "stay_very_heavy_min",
}
TEMP_FIELDS = {"wbgt_in", "wbgt_out", "wet_bulb", "dry_bulb", "globe",
               "heat_index", "humidex"}

_SESSION = re.compile(r"^\s*Session:\s*(\d+)")
_SESSION_P1 = re.compile(r"Session\s*\((\d+)\)")
_START = re.compile(r"Start:\s*(\d{1,2})-([A-Z]{3})-(\d{2})\s+(\d{1,2}:\d{2}:\d{2})")
_SENSOR = re.compile(r"^\s*Sensor:\s*(.+?)\s*$")
_UNITS = re.compile(r"Degrees\s+(Fahrenheit|Celsius)", re.I)
_INTERVAL = re.compile(r"Logging Interval:\s*(\d+)\s*min", re.I)
_SERIAL = re.compile(r"Serial\s*#\s*(\S+)")
_MODEL = re.compile(r"(Questemp\s*\d+)\s+Rev\s*(\S+)", re.I)
_ROW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+(.*)$")


def _norm(name):
    # QT34 Rev 1.10 prints "RH(%)" and "H.I."; the manual shows "RH", "HI".
    return re.sub(r"[^A-Za-z0-9-]", "", name.split("(")[0])


def parse_date(day, mon, yy):
    return date(2000 + int(yy), MONTHS[mon.upper()], int(day))


def f_to_c(v):
    return (v - 32.0) * 5.0 / 9.0


def _num(tok):
    tok = tok.rstrip("%")
    try:
        return float(tok)
    except ValueError:
        return None


def parse(text):
    """Parse a whole capture (may hold several sessions).

    Returns (sessions, rows). `sessions` maps session number -> header dict.
    Each row is a dict: session, sensor, timestamp (naive local datetime),
    unit ("F"/"C"), plus one key per column present in that table.
    """
    sessions = {}
    rows = []
    cur_session = None
    sensor = None
    units = None
    columns = None
    prev_line = ""
    clock = {}      # (session, sensor) -> [date, minute of day] of last row
    pending = {}    # page-1 fields printed before "Session (n)" appears

    for raw in text.replace("\f", "\n").splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if "HEAT STRESS REPORT" in line:
            pending = {}
            cur_session = None
        m = _SESSION_P1.search(line)
        if m:
            cur_session = int(m.group(1))
            sessions.setdefault(cur_session, {"session": cur_session})
            sessions[cur_session].update(pending)
            pending = {}
        hdr = sessions[cur_session] if cur_session is not None else pending
        m = _START.search(line)
        if m:
            d = parse_date(m.group(1), m.group(2), m.group(3))
            hdr["start"] = datetime.combine(
                d, datetime.strptime(m.group(4), "%H:%M:%S").time())
        m = _SERIAL.search(line)
        if m:
            hdr["serial"] = m.group(1)
        m = _INTERVAL.search(line)
        if m:
            hdr["interval_min"] = int(m.group(1))
        m = _MODEL.search(line)
        if m:
            hdr["model"] = m.group(1)
            hdr["firmware"] = m.group(2)

        m = _SESSION.match(line)
        if m:
            cur_session = int(m.group(1))
            sessions.setdefault(cur_session, {"session": cur_session})
            columns = None
            sensor = None
            prev_line = line
            continue

        m = _UNITS.search(line)
        if m:
            units = "F" if m.group(1).lower().startswith("f") else "C"
            if cur_session is not None:
                sessions[cur_session].setdefault("unit", units)

        m = _SENSOR.match(line)
        if m:
            s = m.group(1)
            sensor = "W-AVG" if "W-AVG" in s.upper() else s.split()[0]
            columns = None

        if stripped.startswith("TIME"):
            names = stripped.split()[1:]
            # The weighted-average table splits its names over two lines:
            #         WBGTi   WBGTo
            # TIME    W-AVG   W-AVG   L  M  H  VH
            above = prev_line.split()
            if names[:2] == ["W-AVG", "W-AVG"] and len(above) >= 2:
                names = [above[0], above[1]] + names[2:]
            columns = [COLUMN_MAP.get(_norm(n), _norm(n).lower())
                       for n in names]

        elif columns is not None and cur_session is not None:
            m = _ROW.match(line)
            if m:
                hh, mm = int(m.group(1)), int(m.group(2))
                minute = hh * 60 + mm
                st = clock.get((cur_session, sensor))
                if st is None:
                    start = sessions[cur_session].get("start")
                    st = clock[(cur_session, sensor)] = (
                        [start.date(), start.hour * 60 + start.minute]
                        if start else [None, None])
                if st[0] is not None:
                    if minute < st[1]:
                        st[0] += timedelta(days=1)
                    st[1] = minute
                day = st[0]
                vals = m.group(3).split()
                row = {
                    "session": cur_session,
                    "sensor": sensor,
                    "timestamp": (datetime.combine(day, datetime.min.time())
                                  + timedelta(minutes=minute)) if day else None,
                    "time": f"{hh:02d}:{mm:02d}",
                    "unit": units,
                }
                for name, tok in zip(columns, vals):
                    row[name] = _num(tok)
                rows.append(row)

        if stripped:
            prev_line = line

    return sessions, rows


def to_celsius(row):
    """Copy of `row` with temperature fields in degC. A heat index of 0 means
    "not computed" on this instrument and is left as 0."""
    if row.get("unit") != "F":
        return dict(row)
    out = dict(row)
    for k in TEMP_FIELDS:
        v = out.get(k)
        if v is None or (k in ("heat_index", "humidex") and v == 0):
            continue
        out[k] = round(f_to_c(v), 2)
    out["unit"] = "C"
    return out


def wide_rows(rows):
    """Pivot to one row per (session, timestamp) with sensor-prefixed columns,
    e.g. s1_wbgt_in, s2_globe, wavg_wbgt_in, which suits merging with other
    instruments on time."""
    merged = {}
    for r in rows:
        key = (r["session"], r["timestamp"] or r["time"])
        out = merged.setdefault(key, {"session": r["session"],
                                      "timestamp": r["timestamp"],
                                      "time": r["time"], "unit": r["unit"]})
        prefix = "wavg" if r["sensor"] == "W-AVG" else f"s{r['sensor']}"
        for k, v in r.items():
            if k not in ("session", "sensor", "timestamp", "time", "unit"):
                out[f"{prefix}_{k}"] = v
    return list(merged.values())
