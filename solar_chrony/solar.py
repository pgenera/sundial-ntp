"""NOAA solar position equations.

Everything here works in POSIX seconds (UTC); time zones never enter the
math.  Accuracy is well under a second for solar noon, which is far better
than anything an irradiance sensor can resolve.
"""

import math
from datetime import date, datetime, timezone

_DAY = 86400.0


def _julian_century(ts: float) -> float:
    jd = ts / _DAY + 2440587.5
    return (jd - 2451545.0) / 36525.0


def sun_params(ts: float) -> tuple[float, float]:
    """Return (equation of time in minutes, declination in radians) at ts."""
    t = _julian_century(ts)
    l0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0)
    m_deg = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    m = math.radians(m_deg)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * m) * (0.019993 - 0.000101 * t)
         + math.sin(3 * m) * 0.000289)
    true_long = math.degrees(l0) + c
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(app_long))
    y = math.tan(eps / 2) ** 2
    eqtime = 4 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * e * math.sin(m)
        + 4 * e * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * e * e * math.sin(2 * m))
    return eqtime, decl


def solar_noon_utc(day: date, lon: float) -> float:
    """POSIX timestamp of solar noon on the given (local) date.

    lon is degrees east (negative in the Americas).
    """
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    noon = midnight + (720 - 4 * lon) * 60
    for _ in range(3):
        eqtime, _ = sun_params(noon)
        noon = midnight + (720 - 4 * lon - eqtime) * 60
    return noon


def cos_zenith(ts: float, lat: float, lon: float) -> float:
    eqtime, decl = sun_params(ts)
    minutes = (ts % _DAY) / 60.0
    tst = minutes + eqtime + 4 * lon
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    return math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)


def elevation(ts: float, lat: float, lon: float) -> float:
    """Geometric solar elevation in degrees (no refraction)."""
    return math.degrees(math.asin(max(-1.0, min(1.0, cos_zenith(ts, lat, lon)))))
