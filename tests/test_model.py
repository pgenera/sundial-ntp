import math
import random
from datetime import date, timedelta

import pytest

from solar_chrony import solar
from solar_chrony.model import Model, ModelConfig, declination_deg, clear_sky_ghi, learn

LAT, LON = 40.0, -75.0  # any mid-latitude site
# Each panel is in direct sun between these hour angles (minutes from
# noon), plus a drift per degree of declination: a stand-in for trees.
PANELS = {f"p{k}": (-210 + 13 * k, 10 + 9 * k, (-1) ** k * (1 + k % 3)) for k in range(12)}
SENSOR = (-95, 125, 1.5)
FIRST = date(2026, 9, 1)


def lit(ha, window, decl, ramp=4.0):
    a, b, slope = window
    a, b = a + slope * decl, b - slope * decl
    up = min(1.0, max(0.0, (ha - a) / ramp + 0.5))
    down = min(1.0, max(0.0, (b - ha) / ramp + 0.5))
    return up * down


def ha_of(t):
    eq, _ = solar.sun_params(t)
    return ((t % 86400) / 60 + eq + 4 * LON - 720 + 720) % 1440 - 720


def synth_day(day, shift=0.0, cloud=1.0, snow=False, covered=(), seed=0):
    """(panel series, sensor samples) as the system clock would record them.

    shift: seconds by which the recorded timestamps run ahead of the sun.
    """
    rng = random.Random(seed + day.toordinal())
    decl = declination_deg(day, LON)
    noon = solar.solar_noon_utc(day, LON)
    panels = {k: [] for k in PANELS}
    t = noon - 8 * 3600
    t -= t % 300 - 28
    while t < noon + 8 * 3600:
        true_t = t - shift
        cz = max(0.0, solar.cos_zenith(true_t, LAT, LON))
        ha = ha_of(true_t)
        for k, win in PANELS.items():
            direct = 0.0 if (snow or k in covered) else lit(ha, win, decl)
            w = 240 * cz ** 1.2 * (0.12 + 0.88 * direct * cloud)
            panels[k].append((t, 0.0 if snow else w * rng.uniform(0.98, 1.02)))
        t += 300
    sensor = []
    t = noon - 8 * 3600
    while t < noon + 8 * 3600:
        true_t = t - shift
        cs = clear_sky_ghi(true_t, LAT, LON)
        if not math.isnan(cs):
            v = cs * (0.12 + 0.88 * lit(ha_of(true_t), SENSOR, decl) * cloud)
            sensor.append((t, v * rng.uniform(0.97, 1.03)))
        t += 30
    return panels, sensor


@pytest.fixture(scope="module")
def model():
    probe = Model(LAT, LON, [])
    profiles = [probe.observe(FIRST + timedelta(d), *synth_day(FIRST + timedelta(d))) for d in range(30)]
    return learn(LAT, LON, profiles, ModelConfig(min_points_panels=150, min_points_sensor=30))


def estimate(model, day, **kw):
    return model.estimate(model.observe(day, *synth_day(day, **kw)))


def test_learned_backtest_is_accurate(model):
    # Calibrated as if the model were a week old; floored at 20 s.
    assert model.stats["panels"]["rms"] <= 30
    assert model.stats["sensor"]["rms"] is None or model.stats["sensor"]["rms"] <= 40


@pytest.mark.parametrize("shift", [-240.0, 0.0, 95.0])
def test_recovers_shift(model, shift):
    est = estimate(model, FIRST + timedelta(30), shift=shift)
    assert est.ok, est.notes
    assert est.used == ["panels", "sensor"]
    assert est.offset == pytest.approx(shift, abs=20)


def test_snow_falls_back_to_sensor(model):
    est = estimate(model, FIRST + timedelta(30), shift=60.0, snow=True)
    assert est.ok, est.notes
    assert est.used == ["sensor"]
    assert est.offset == pytest.approx(60.0, abs=30)


def test_covered_panel_is_dropped(model):
    est = estimate(model, FIRST + timedelta(30), shift=60.0, covered={"p3"})
    assert est.ok, est.notes
    assert "panel:p3" not in est.panels.channels
    assert est.offset == pytest.approx(60.0, abs=20)


def test_overcast_day_gives_no_estimate(model):
    est = estimate(model, FIRST + timedelta(30), cloud=0.1)
    assert not est.ok


def test_stale_model_refuses(model):
    est = estimate(model, FIRST + timedelta(60))
    assert not est.ok
    assert any("stale" in n for n in est.notes)


def test_save_load_roundtrip(model, tmp_path):
    path = tmp_path / "model.json"
    model.save(str(path))
    again = Model.load(str(path))
    day = FIRST + timedelta(30)
    a = model.estimate(model.observe(day, *synth_day(day, shift=30.0)))
    b = again.estimate(again.observe(day, *synth_day(day, shift=30.0)))
    assert a.offset == pytest.approx(b.offset, abs=0.5)
