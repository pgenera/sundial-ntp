from datetime import date, datetime, timezone

import pytest

from solar_chrony import solar


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("day, expected", [
    # Extremes of the equation of time (published values, ±a few s).
    (date(2024, 2, 11), utc(2024, 2, 11, 12, 14, 14)),
    (date(2024, 11, 3), utc(2024, 11, 3, 11, 43, 35)),
    # Near-zero crossings.
    (date(2024, 6, 13), utc(2024, 6, 13, 12, 0, 0)),
    (date(2024, 12, 25), utc(2024, 12, 25, 12, 0, 0)),
])
def test_noon_at_greenwich(day, expected):
    assert solar.solar_noon_utc(day, 0.0) == pytest.approx(expected, abs=20)


def test_longitude_shifts_noon():
    d = date(2026, 9, 24)
    assert solar.solar_noon_utc(d, -75.0) - solar.solar_noon_utc(d, 0.0) == pytest.approx(5 * 3600, abs=5)


def test_sun_is_highest_at_noon():
    lat, lon = 40.7, -74.0
    noon = solar.solar_noon_utc(date(2026, 9, 24), lon)
    for dt in (-600, -60, 60, 600):
        assert solar.elevation(noon, lat, lon) > solar.elevation(noon + dt, lat, lon)
    # Equinox-ish: noon elevation ≈ 90 − lat.
    assert solar.elevation(noon, lat, lon) == pytest.approx(90 - lat, abs=1.0)
