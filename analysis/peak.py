"""Does the peak of the whole array's output keep time?  (No: ~10 min RMS.)

    python3 analysis/peak.py SOLAR_TOML MODEL_JSON

For each day the model's panel fit accepts in back-testing (the clear-ish
days), sum the 17 panels and find the peak of the total three ways, in
minutes from solar noon.  The system clock is right, so after removing the
slow seasonal drift (a straight line against declination, leaving each day
out in turn) whatever is left is the method's error.  Compare the model's
own back-test on the same days.
"""
import math
import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")
from solar_chrony import solar
from solar_chrony.main import fetch_day, load_config
from solar_chrony.model import Model
from solar_chrony.promsource import Prometheus


def interp(pts, x):
    """Linear interpolation in sorted (t, v) points; None outside them."""
    lo, hi = 0, len(pts) - 1
    if not pts or x < pts[0][0] or x > pts[-1][0]:
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if pts[mid][0] <= x else (lo, mid)
    (t0, v0), (t1, v1) = pts[lo], pts[hi]
    return v0 if t1 == t0 else v0 + (v1 - v0) * (x - t0) / (t1 - t0)


def solve(a, b):
    """Gaussian elimination for a small square system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def polyfit(xs, ys, deg):
    """Least-squares polynomial, highest power first."""
    k = deg + 1
    a = [[sum(x ** (2 * deg - i - j) for x in xs) for j in range(k)] for i in range(k)]
    b = [sum(y * x ** (deg - i) for x, y in zip(xs, ys)) for i in range(k)]
    return solve(a, b)


def peak_max(pts):
    return max(pts, key=lambda p: p[1])[0]


def peak_parabola(pts, half):
    m = peak_max(pts)
    sel = [(t - m, v) for t, v in pts if abs(t - m) <= half]
    a, b, _ = polyfit([t for t, _ in sel], [v for _, v in sel], 2)
    return m - b / (2 * a)


def peak_symmetry(pts, span=150):
    """The centre about which the curve is most symmetric."""
    best = None
    for k in range(-480, 480):
        c = k / 4
        diffs = []
        for t in range(5, span):
            l, r = interp(pts, c - t), interp(pts, c + t)
            if l is not None and r is not None:
                diffs.append((l - r) ** 2)
        if len(diffs) >= 0.8 * (span - 5):
            cost = sum(diffs) / len(diffs)
            if best is None or cost < best[0]:
                best = (cost, c)
    return best[1]


def loo_residuals(xs, ys):
    """Residuals of a straight-line fit of ys on xs, each with its own point left out."""
    out = []
    for i in range(len(xs)):
        a, b = polyfit(xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:], 1)
        out.append(ys[i] - (a * xs[i] + b))
    return out


def total_output(cfg, prom, day, lon):
    """Sum of all panels per minute, (minutes from solar noon, W), complete minutes only."""
    for attempt in range(5):
        try:
            panels, _ = fetch_day(cfg, prom, day)
            break
        except OSError as e:
            print(f"retrying {day}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit("prometheus keeps failing")
    tot, cnt = defaultdict(float), defaultdict(int)
    for rows in panels.values():
        for t, v in rows:
            k = round(t / 60) * 60
            tot[k] += v
            cnt[k] += 1
    n = max(cnt.values())
    noon = solar.solar_noon_utc(day, lon)
    return sorted(((t - noon) / 60, p) for t, p in tot.items() if cnt[t] == n and abs(t - noon) < 6 * 3600)


def rms(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def main():
    cfg = load_config(sys.argv[1])
    lon = cfg["site"]["longitude"]
    model = Model.load(sys.argv[2], cfg["model_cfg"])
    decl = {d.day: d.decl for d in model.days}
    clear = [e for e in model.backtest() if "panels" in e.used]
    prom = Prometheus(cfg["prometheus"]["url"])
    curves = {e.day: total_output(cfg, prom, e.day, lon) for e in clear}
    xs = [decl[e.day] for e in clear]
    print(f"{len(clear)} clear days, {clear[0].day}..{clear[-1].day}, declination {min(xs):+.1f}..{max(xs):+.1f}")
    print(f"{'method':24}{'peak vs noon':>14}{'error RMS':>11}{'worst':>9}")
    for name, f in (("highest minute", peak_max),
                    ("parabola ±60 min", lambda p: peak_parabola(p, 60)),
                    ("parabola ±120 min", lambda p: peak_parabola(p, 120)),
                    ("centre of symmetry", peak_symmetry)):
        mins = [f(curves[e.day]) for e in clear]
        res = [r * 60 for r in loo_residuals(xs, mins)]
        print(f"{name:24}{sum(mins) / len(mins):+10.1f} min{rms(res):9.0f} s{max(map(abs, res)):7.0f} s")
    shifts = [e.panels.shift for e in clear]
    print(f"{'shade-edge model':24}{'':14}{rms(shifts):9.0f} s{max(map(abs, shifts)):7.0f} s")
    flat = []
    for e in clear:
        top = max(v for _, v in curves[e.day])
        near = [t for t, v in curves[e.day] if v >= 0.97 * top]
        flat.append(max(near) - min(near))
    print(f"minutes within 3% of the day's peak: median {sorted(flat)[len(flat) // 2]:.0f}")


if __name__ == "__main__":
    main()
