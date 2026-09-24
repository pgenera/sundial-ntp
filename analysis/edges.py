"""Prototype: per-panel shade-edge timing."""
import math, sqlite3, statistics, sys
from collections import defaultdict
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, '.')
from solar_chrony import solar
from solar_chrony.main import day_bounds

SP = sys.argv[1]
LAT, LON = float(sys.argv[2]), float(sys.argv[3])  # site latitude, longitude
tz = ZoneInfo('America/New_York')
db = sqlite3.connect(f'{SP}/panels.db')
devs = [r[0] for r in db.execute('select distinct dev from p order by dev')]

def ha_min(t):
    eq, _ = solar.sun_params(t)
    return ((t % 86400) / 60 + eq + 4 * LON) - 720

def decl_deg(t):
    return math.degrees(solar.sun_params(t)[1])

def load_day(day):
    s, e = day_bounds(day, tz)
    by_t = defaultdict(dict)
    for dev, ts, w in db.execute('select dev, ts, w from p where ts >= ? and ts < ? order by ts', (s, e)):
        by_t[round(ts / 60) * 60][dev] = w   # all panels scraped together
    # keep only update instants (values changed for most panels vs previous)
    times = sorted(t for t, d in by_t.items() if len(d) == len(devs))
    upd, prev = [], None
    for t in times:
        vals = tuple(by_t[t][d] for d in devs)
        if prev is None or sum(a != b for a, b in zip(vals, prev)) > len(devs) // 2:
            upd.append((t, vals))
        prev = vals
    return upd

days = [date(2026, 8, 21) + timedelta(i) for i in range((date(2026, 9, 23) - date(2026, 8, 21)).days + 1)]
data = {d: load_day(d) for d in days}

# Clear-sky envelope of the brightest panel vs hour angle (5-min bins).
ref_by_bin = defaultdict(list)
for d, upd in data.items():
    for t, vals in upd:
        ref_by_bin[round(ha_min(t) / 5)].append(sorted(vals)[-2])
env = {b: sorted(v)[int(0.95 * (len(v) - 1))] for b, v in ref_by_bin.items()}

LO, HI, MID = 0.3, 0.7, 0.5
def edges_for_day(upd):
    """[(dev_index, direction, t_edge)] of clean shade edges."""
    out = []
    ts = [t for t, _ in upd]
    ref = [sorted(v)[-2] for _, v in upd]
    valid = [ref[i] >= 0.75 * env.get(round(ha_min(ts[i]) / 5), 1e9) and ref[i] > 60 for i in range(len(upd))]
    for k in range(len(devs)):
        r = [upd[i][1][k] / ref[i] if ref[i] else 0 for i in range(len(upd))]
        for i in range(3, len(upd) - 4):
            if not (r[i] < MID <= r[i + 1] or r[i] >= MID > r[i + 1]):
                continue
            rising = r[i + 1] > r[i]
            before = range(i - 3, i + 1 - (0 if (r[i] < LO if rising else r[i] > HI) else 1))
            after = range(i + 1 + (0 if (r[i + 1] > HI if rising else r[i + 1] < LO) else 1), i + 5)
            win = range(i - 3, i + 5)
            if not all(valid[j] for j in win):
                continue
            if any(ts[j + 1] - ts[j] > 400 for j in range(i - 3, i + 4)):
                continue
            if rising and not (all(r[j] < LO for j in before) and all(r[j] > HI for j in after)):
                continue
            if not rising and not (all(r[j] > HI for j in before) and all(r[j] < LO for j in after)):
                continue
            te = ts[i] + (MID - r[i]) / (r[i + 1] - r[i]) * (ts[i + 1] - ts[i])
            out.append((k, rising, te))
    return out

all_edges = {d: edges_for_day(data[d]) for d in days}

def predict(edge, train_days, win=15.0):
    k, rising, te = edge
    ha0 = ha_min(te)
    pts = [(decl_deg(t), ha_min(t)) for d in train_days for (kk, rr, t) in all_edges[d]
           if kk == k and rr == rising and abs(ha_min(t) - ha0) < win]
    if len(pts) < 4:
        return None
    x0 = decl_deg(te)
    for _ in range(2):  # robust linear fit ha = a + b (decl - x0)
        n = len(pts); mx = sum(p[0] - x0 for p in pts) / n; my = sum(p[1] for p in pts) / n
        sxx = sum((p[0] - x0 - mx) ** 2 for p in pts)
        b = sum((p[0] - x0 - mx) * (p[1] - my) for p in pts) / sxx if sxx > 1e-6 else 0.0
        a = my - b * mx
        res = [p[1] - (a + b * (p[0] - x0)) for p in pts]
        mad = statistics.median(abs(x) for x in res) or 0.1
        keep = [p for p, e in zip(pts, res) if abs(e) < 4 * 1.4826 * mad + 0.5]
        if len(keep) < 4: return None
        pts = keep
    span = max(p[0] for p in pts) - min(p[0] for p in pts)
    return ha0 - a, len(pts), span

def evaluate(mode):
    print(f'\n=== {mode} ===')
    print('day          edges used  median  (s)   robust-sd  sem')
    medians = []
    for i, d in enumerate(days):
        if mode == 'walk-forward':
            train = days[max(0, i - 21):i]
            if len(train) < 7: continue
        else:
            train = [x for x in days if x != d]
        res = [p for p in (predict(e, train) for e in all_edges[d]) if p]
        if not res:
            print(f'{d}  {len(all_edges[d]):5d}  {0:4d}   -'); continue
        offs = sorted(r[0] * 60 for r in res)
        med = statistics.median(offs)
        mad = statistics.median(abs(o - med) for o in offs) * 1.4826
        sem = mad / math.sqrt(len(offs))
        medians.append((d, med, len(offs)))
        print(f'{d}  {len(all_edges[d]):5d}  {len(offs):4d}  {med:+7.1f}   {mad:7.1f}  {sem:5.1f}')
    good = [m for _, m, n in medians if n >= 8]
    if good:
        print(f'days with >=8 edges: {len(good)}; offset mean {statistics.mean(good):+.1f}s, '
              f'rms {math.sqrt(sum(x*x for x in good)/len(good)):.1f}s, max |{max(map(abs, good)):.1f}|s')

print('edges per day:', {str(d)[5:]: len(e) for d, e in all_edges.items()})
evaluate('leave-one-out')
evaluate('walk-forward')
