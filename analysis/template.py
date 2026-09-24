"""Prototype: per-panel lit-fraction templates, joint time-shift fit."""
import math, statistics, sys
from datetime import date, timedelta
sys.path.insert(0, '.')
sys.argv = sys.argv[:4]
exec(open(f'{sys.argv[1]}/edges.py').read().split("all_edges = {")[0])   # reuse loaders, env, ha_min, decl_deg

HA_LO, HA_HI = -300, 240          # minutes from solar noon considered
NB = HA_HI - HA_LO + 1

def day_profile(upd):
    """Per panel: 1-min grid of lit fraction r (NaN where invalid), plus decl."""
    ts = [t for t, _ in upd]
    ref = [sorted(v)[-2] for _, v in upd]
    ok = [ref[i] >= 0.75 * env.get(round(ha_min(ts[i]) / 5), 1e9) and ref[i] > 60 for i in range(len(upd))]
    has = [ha_min(t) for t in ts]
    prof = []
    for k in range(len(devs)):
        r = [upd[i][1][k] / ref[i] if ref[i] else 0 for i in range(len(upd))]
        g = [math.nan] * NB
        for i in range(len(upd) - 1):
            if not (ok[i] and ok[i + 1]) or ts[i + 1] - ts[i] > 400: continue
            a, b = has[i], has[i + 1]
            for m in range(math.ceil(a), math.floor(b) + 1):
                j = m - HA_LO
                if 0 <= j < NB:
                    g[j] = r[i] + (r[i + 1] - r[i]) * (m - a) / (b - a)
        prof.append(g)
    noon = solar.solar_noon_utc(upd[0][0] and date.fromtimestamp(upd[len(upd)//2][0]), LON)
    return prof, decl_deg(noon)

profiles = {d: day_profile(data[d]) for d in days if data[d]}

def template(train, decl0, sigma=1.5):
    """Local-linear (in declination) estimate of each panel's profile."""
    T = []
    for k in range(len(devs)):
        g = []
        for j in range(NB):
            pts = [(profiles[d][1] - decl0, profiles[d][0][k][j]) for d in train if not math.isnan(profiles[d][0][k][j])]
            if len(pts) < 3: g.append(math.nan); continue
            w = [math.exp(-(x / sigma) ** 2) for x, _ in pts]
            sw = sum(w)
            if sw < 0.5: g.append(math.nan); continue
            mx = sum(wi * x for wi, (x, _) in zip(w, pts)) / sw
            my = sum(wi * y for wi, (_, y) in zip(w, pts)) / sw
            sxx = sum(wi * (x - mx) ** 2 for wi, (x, _) in zip(w, pts))
            b = sum(wi * (x - mx) * (y - my) for wi, (x, y) in zip(w, pts)) / sxx if sxx > 1e-3 else 0
            g.append(my - b * mx)
        T.append(g)
    return T

def fit_shift(prof, T, search=15):
    best = None
    scores = []
    for s in range(-search, search + 1):       # minutes
        num = den = 0
        for k in range(len(devs)):
            p, t = prof[k], T[k]
            for j in range(NB):
                jj = j - s
                if 0 <= jj < NB and not math.isnan(p[j]) and not math.isnan(t[jj]):
                    # only where the template has structure (edges)
                    if 0 < jj < NB - 1 and not math.isnan(t[jj - 1]) and not math.isnan(t[jj + 1]) and abs(t[jj + 1] - t[jj - 1]) > 0.02:
                        num += (p[j] - t[jj]) ** 2; den += 1
        scores.append(num / den if den > 30 else math.inf)
    i = min(range(len(scores)), key=scores.__getitem__)
    if math.isinf(scores[i]): return None, 0
    sh = float(i - search)
    if 0 < i < len(scores) - 1 and all(math.isfinite(x) for x in scores[i-1:i+2]):
        r0, r1, r2 = scores[i - 1:i + 2]; den = r0 - 2 * r1 + r2
        if den > 0: sh += 0.5 * (r0 - r2) / den
    return sh * 60, scores[i]

def run(mode):
    print(f'\n=== template, {mode} ===')
    out = []
    ds = sorted(profiles)
    for i, d in enumerate(ds):
        train = [x for x in ds if x != d] if mode == 'leave-one-out' else ds[max(0, i - 21):i]
        if len(train) < 7: continue
        prof, decl0 = profiles[d]
        n_valid = sum(not math.isnan(v) for g in prof for v in g)
        if n_valid < 17 * 60: continue      # needs an hour of clear sky
        sh, sc = fit_shift(prof, template(train, decl0))
        if sh is None: continue
        out.append(sh)
        print(f'{d}  valid-min {n_valid // 17:4d}  shift {sh:+7.1f}s  mse {sc:.4f}')
    print(f'n={len(out)} mean {statistics.mean(out):+.1f}s rms {math.sqrt(sum(x*x for x in out)/len(out)):.1f}s max |{max(map(abs,out)):.1f}|s')

run('leave-one-out')
run('walk-forward')
