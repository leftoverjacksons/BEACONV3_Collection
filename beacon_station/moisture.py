"""
Internal-condensation risk for the BEACON housing.

The SHT3x sits on the PCB inside the sealed housing (TPE membrane): its
temperature + SHT3x_RH give the dew point of the INTERNAL air. Vapour
pressure is uniform through the enclosed air even though temperature is
not, so this one dew point applies at the (colder) housing walls too.

When the device cools — humid outdoors, then a cold room or freezer — the
walls cool first and fastest, and water condenses on any surface colder
than the internal dew point. Wall temperature is not measured; ambient air
(the colder of TMP119 and HDC3022) is used as a conservative proxy, since
the wall tends toward ambient during a cool-down.

    wall margin      M_int = T_ambient - Td_int     (< 0: walls likely wet)
    external margin  M_ext = T_cold    - Td_ext     (< 0: cold device in warm
                                                     humid air: condensation
                                                     on the outside/sensors)
    internal RH      SHT3x_RH at the PCB itself

Limitation of the external check: the external dew point is measured BY the
external sensor. Once the HDC3022 is itself colder than the room's dew point
it saturates (~100 %RH, wet), so Td_ext ~= its own temperature and M_ext
settles near 0 (caution) rather than clearly negative. Sustained external RH
near 100 % with a cold device should be read as "sensor wetted".

Levels: "ok", "caution" (margin below `caution_margin_C`, or internal RH at
or above `rh_int_caution`), "risk" (a margin below 0).

Magnus formula over water, Alduchov & Eskridge (1996) coefficients; ~0.1 C
over -40..50 C. Below 0 C a surface frosts at the frost point, which is
above the dew point; with humid internal air liquid condensation starts far
above 0 C anyway, so the water-based dew point is used throughout. Sensor
accuracy (about +/-2 %RH -> about +/-0.6 C dew point) dominates the formula
error: treat dew-point differences under ~1.5 C as noise.
"""

import math

A, B = 17.625, 243.04          # Magnus, over water
E0 = 6.1094                    # hPa

LEVELS = ("ok", "caution", "risk")


def vapor_pressure(t_c, rh):
    """hPa. None if either input is missing or RH is non-positive."""
    if t_c is None or rh is None or rh <= 0:
        return None
    return E0 * math.exp(A * t_c / (t_c + B)) * min(rh, 100.0) / 100.0


def dew_point(t_c, rh):
    e = vapor_pressure(t_c, rh)
    if e is None:
        return None
    x = math.log(e / E0)
    return B * x / (A - x)


def _min(*vals):
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def assess(s, caution_margin_c=3.0, rh_int_caution=90.0):
    """Beacon sample (dict with the logged field names) -> derived values,
    level and human-readable reasons. Missing inputs give None values and
    never raise; a sample with no SHT3x_RH (old firmware) assesses nothing
    internal."""
    td_int = dew_point(s.get("SHT3x_C"), s.get("SHT3x_RH_pct"))
    td_ext = dew_point(s.get("HDC3022_C"), s.get("RH_pct"))
    t_amb = _min(s.get("TMP119_C"), s.get("HDC3022_C"))
    t_cold = _min(s.get("TMP119_C"), s.get("HDC3022_C"), s.get("SHT3x_C"))
    m_int = None if td_int is None or t_amb is None else t_amb - td_int
    m_ext = None if td_ext is None or t_cold is None else t_cold - td_ext
    rh_int = s.get("SHT3x_RH_pct")

    level, reasons = "ok", []

    def bump(lv, why):
        nonlocal level
        if LEVELS.index(lv) > LEVELS.index(level):
            level = lv
        reasons.append(why)

    if m_int is not None:
        if m_int < 0:
            bump("risk", f"ambient {t_amb:.1f} C below internal dew point "
                         f"{td_int:.1f} C (walls likely condensing)")
        elif m_int < caution_margin_c:
            bump("caution", f"wall margin {m_int:.1f} C")
    if m_ext is not None:
        if m_ext < 0:
            bump("risk", f"device {t_cold:.1f} C below external dew point "
                         f"{td_ext:.1f} C (condensing outside)")
        elif m_ext < caution_margin_c:
            bump("caution", f"external margin {m_ext:.1f} C")
    if rh_int is not None and rh_int >= rh_int_caution:
        bump("caution", f"internal RH {rh_int:.0f} %")

    return {
        "td_int_C": td_int, "td_ext_C": td_ext,
        "e_int_hPa": vapor_pressure(s.get("SHT3x_C"), rh_int),
        "e_ext_hPa": vapor_pressure(s.get("HDC3022_C"), s.get("RH_pct")),
        "t_amb_C": t_amb,
        "margin_int_C": m_int, "margin_ext_C": m_ext,
        "rh_int_pct": rh_int,
        "level": level if (m_int is not None or m_ext is not None) else None,
        "reasons": reasons,
    }


class LevelTracker:
    """Debounces the level: a change is accepted only after it has been
    seen on `persist` consecutive samples, so a single noisy burst does not
    produce a pair of events. update() returns (old, new) on a change."""

    def __init__(self, persist=2):
        self.persist = max(1, int(persist))
        self.level = None
        self._cand, self._n = None, 0

    def update(self, level):
        if level is None or level == self.level:
            self._cand, self._n = None, 0
            return None
        if level != self._cand:
            self._cand, self._n = level, 0
        self._n += 1
        if self._n >= self.persist or self.level is None:
            old, self.level = self.level, level
            self._cand, self._n = None, 0
            return old, level
        return None
