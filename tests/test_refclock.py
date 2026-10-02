import os
from datetime import date, timedelta

from solar_chrony.refclock import refclock_conf, served_error, write_if_changed

TODAY = date(2026, 10, 2)


def fixes(*specs):
    """(days ago, offset, sigma) -> (day, offset, sigma)."""
    return [(TODAY - timedelta(days=back), off, sig) for back, off, sig in specs]


def test_no_fixes_no_bound():
    assert served_error([], 7, TODAY) is None


def test_one_fix_is_its_own_sigma():
    assert served_error(fixes((0, -18.0, 47.4)), 7, TODAY) == 60      # ceil(1.25 × 47.4)


def test_more_fixes_tighten_the_bound():
    four = fixes((3, 10.0, 44.0), (2, -5.0, 44.0), (1, 20.0, 44.0), (0, 0.0, 44.0))
    assert served_error(four, 7, TODAY) == 28                          # ceil(1.25 × 44 / 2)


def test_only_the_smoothing_window_counts():
    # The 10-day-old fix is outside a 7-day window, so this is one fix, not two.
    assert served_error(fixes((10, 0.0, 44.0), (0, 0.0, 44.0)), 7, TODAY) == 55


def test_rms_of_mixed_sigmas():
    # A sensor-only fix (sigma 120) and a panel fix (44): RMS 90.4, over √2.
    assert served_error(fixes((1, 0.0, 120.0), (0, 0.0, 44.0)), 7, TODAY, growth_per_day=0) == 80


def test_bound_grows_each_day_without_a_fix():
    f = fixes((0, -18.0, 47.4))
    bounds = [served_error(f, 7, TODAY + timedelta(days=k)) for k in range(4)]
    assert bounds == [60, 65, 70, 75]
    assert served_error(f, 7, TODAY + timedelta(days=3), growth_per_day=0) == 60


def test_refclock_line():
    assert refclock_conf(7, 60.0).splitlines()[-1] == "refclock SHM 7:perm=0660 refid SUN poll 4 precision 60"
    assert refclock_conf(7, None).splitlines()[-1] == "refclock SHM 7:perm=0660 refid SUN poll 4 noselect"


def test_write_only_on_change(tmp_path):
    path = str(tmp_path / "refclock.conf")
    assert write_if_changed(path, "a\n")
    mtime = os.stat(path).st_mtime_ns
    assert not write_if_changed(path, "a\n")
    assert os.stat(path).st_mtime_ns == mtime
    assert write_if_changed(path, "b\n")
    assert open(path).read() == "b\n"
    assert os.stat(path).st_mode & 0o777 == 0o644
    assert not os.path.exists(path + ".tmp")
