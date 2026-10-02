"""The solar chronyd's `refclock` line, carrying an honest error bound.

chronyd's SHM driver ignores the precision field in the segment, and has
since the driver was written, so the only way to tell clients how wrong
the sun might be is the refclock's `precision` option, which chronyd adds
to every sample's root dispersion.  The feed rewrites a small include file
whenever the bound changes, and a systemd path unit restarts chronyd-solar
to read it.  docs/chrony-shm-precision.md has the whole story.
"""

import math
import os
from datetime import date

from .state import smooth


def served_error(fixes: list[tuple[date, float, float]], days: int, today: date,
                 factor: float = 1.25, growth_per_day: float = 5.0) -> float | None:
    """Error bound (s) for what the feed serves, given (day, offset, sigma) fixes.

    The served value combines the n fixes of the smoothing window, so its
    error is about their RMS sigma over √n; `factor` 1.25 covers 95% of the
    served days in back-testing.  The system clock doesn't drift from the
    sun, so the served value doesn't really get worse with age, but an old
    fix is old news: the bound grows by `growth_per_day` for every day since
    the newest one.  Rounded up to whole seconds, so it changes at most
    once a day between fixes.
    """
    if not fixes:
        return None
    sigma = {d: s for d, _, s in fixes}
    _, used = smooth([(d, o) for d, o, _ in fixes], days)
    rms = math.sqrt(sum(sigma[d] ** 2 for d, _ in used) / len(used))
    age = max(0, (today - max(sigma)).days)
    return float(math.ceil(factor * rms / math.sqrt(len(used)) + growth_per_day * age))


def refclock_conf(unit: int, error: float | None) -> str:
    """The include file's text; error None means there's no current fix.

    Without a fix the feed posts nothing, but chronyd would go on serving
    its last SUN offset for years: the honest bound is so large that its
    slow dispersion growth never reaches `local ... distance`.  noselect
    makes it fall back to the local stratum as soon as it restarts.
    """
    if error is None:
        return ("# No current solar fix (written by solar-noon feed): serve the local stratum.\n"
                f"refclock SHM {unit}:perm=0660 refid SUN poll 4 noselect\n")
    return ("# The sun, with its current error bound (written by solar-noon feed).\n"
            f"refclock SHM {unit}:perm=0660 refid SUN poll 4 precision {error:g}\n")


def write_if_changed(path: str, text: str) -> bool:
    """Atomically replace `path` with `text` unless it already says that."""
    try:
        with open(path) as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True
