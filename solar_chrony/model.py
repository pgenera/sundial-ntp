"""Learned shading model: tell the time from where shadows fall.

Every channel (each solar panel, and the weather-station irradiance sensor)
is reduced to a per-minute "lit fraction" profile against the hour angle,
that is, minutes from solar noon.  Trees and roof edges shade each channel
at fixed sun positions, so on any given day these profiles have sharp,
repeatable edges.

Learning (against system-clock timestamps) keeps a set of daily profiles.
Serving builds today's expected profile for each channel by interpolating
the learned days to today's solar declination, then finds the single time
shift that best lines today's observations up with them.  Panels and the
sensor are fitted as separate groups so either can stand in when the other
is unusable (snow on the panels, a dead sensor).
"""

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date

from . import solar

NAN = float("nan")
HA_LO, HA_HI = -360, 360          # minutes from solar noon kept per day
NB = HA_HI - HA_LO + 1


def hour_angle_min(t: float, lon: float) -> float:
    """Minutes from local apparent (solar) noon at POSIX time t."""
    eqtime, _ = solar.sun_params(t)
    ha = (t % 86400) / 60 + eqtime + 4 * lon - 720
    return (ha + 720) % 1440 - 720


def declination_deg(day: date, lon: float) -> float:
    return math.degrees(solar.sun_params(solar.solar_noon_utc(day, lon))[1])


def clear_sky_ghi(t: float, lat: float, lon: float) -> float:
    """Haurwitz clear-sky global horizontal irradiance, W/m²."""
    cz = solar.cos_zenith(t, lat, lon)
    return 1098 * cz * math.exp(-0.057 / cz) if cz > 0.08 else NAN


def _to_grid(points: list[tuple[float, float]], max_gap_min: float) -> list[float]:
    """Linearly interpolate (hour-angle-minutes, value) points onto the grid."""
    grid = [NAN] * NB
    for (a, va), (b, vb) in zip(points, points[1:]):
        if b <= a or b - a > max_gap_min or math.isnan(va) or math.isnan(vb):
            continue
        for m in range(math.ceil(a), math.floor(b) + 1):
            j = m - HA_LO
            if 0 <= j < NB:
                grid[j] = va + (vb - va) * (m - a) / (b - a)
    return grid


def panel_profiles(series: dict[str, list[tuple[float, float]]], lon: float):
    """Per-panel lit fraction and the brightest-panel reference, on the grid.

    Panels are read together (one gateway poll every few minutes), so each
    panel is divided by the second-brightest panel at the same instant.
    Clouds dim every panel alike and cancel; shade does not.
    """
    devs = sorted(series)
    if not devs:
        return {}, [NAN] * NB
    instants: dict[int, dict[str, float]] = {}
    for dev in devs:
        for t, w in series[dev]:
            instants.setdefault(int(round(t / 5)), {})[dev] = w
    need = max(2, int(0.8 * len(devs)))
    rows, prev = [], None
    for key in sorted(instants):
        vals = instants[key]
        if len(vals) < need:
            continue
        vec = tuple(vals.get(d, NAN) for d in devs)
        # Keep only instants where the gateway actually refreshed its data.
        if prev is None or sum(a != b for a, b in zip(vec, prev)) > len(devs) // 2:
            rows.append((key * 5.0, vec))
        prev = vec
    if len(devs) < 2 or not rows:
        return {}, [NAN] * NB
    ha = [hour_angle_min(t, lon) for t, _ in rows]
    refs = [sorted(v for v in vec if not math.isnan(v))[-2] for _, vec in rows]
    profiles = {}
    for k, dev in enumerate(devs):
        pts = [(h, vec[k] / ref if ref > 0 else NAN) for h, (_, vec), ref in zip(ha, rows, refs)]
        profiles[dev] = _to_grid(pts, max_gap_min=7)
    return profiles, _to_grid(list(zip(ha, refs)), max_gap_min=7)


def sensor_profile(samples: list[tuple[float, float]], lat: float, lon: float) -> list[float]:
    """Irradiance sensor as a fraction of clear-sky irradiance, on the grid."""
    pts = []
    for t, v in samples:
        cs = clear_sky_ghi(t, lat, lon)
        pts.append((hour_angle_min(t, lon), NAN if math.isnan(cs) else max(0.0, min(2.0, v / cs))))
    return _to_grid(pts, max_gap_min=5)


@dataclass
class DayProfile:
    day: date
    decl: float
    panels: dict[str, list[float]]
    panel_ref: list[float]
    sensor: list[float]

    def to_json(self):
        enc = lambda g: [None if math.isnan(v) else round(v, 4) for v in g]
        return {"day": self.day.isoformat(), "decl": self.decl,
                "panels": {k: enc(g) for k, g in self.panels.items()},
                "panel_ref": enc(self.panel_ref), "sensor": enc(self.sensor)}

    @classmethod
    def from_json(cls, d):
        dec = lambda g: [NAN if v is None else v for v in g]
        return cls(date.fromisoformat(d["day"]), d["decl"],
                   {k: dec(g) for k, g in d["panels"].items()}, dec(d["panel_ref"]), dec(d["sensor"]))


@dataclass
class ModelConfig:
    sigma_decl: float = 1.5            # template smoothing across days, degrees
    max_extrapolation_deg: float = 4.0 # refuse days this far outside training
    env_window_deg: float = 4.0
    env_fraction: float = 0.75         # brightest panel vs its clear-sky envelope
    min_ref_watts: float = 60.0
    search_minutes: int = 20
    trim: float = 0.3                  # robust loss cap (lit-fraction units)
    min_points_panels: int = 400       # edge-minutes needed across all panels
    min_points_sensor: int = 60
    min_sharpness: float = 1.8         # how sharply the best shift stands out
    sensor_clear_min: float = 0.7      # share of its sunny window that must be sunny
    max_cost_factor: float = 2.5       # vs the median back-test cost
    calibration_gap_days: int = 7      # back-test as if the model were this old
    max_disagreement_sigma: float = 3.0


@dataclass
class GroupFit:
    shift: float                       # seconds; + means events happen late
    cost: float
    points: int
    channels: list[str]
    sharpness: float = 0.0             # cost ≥5 min away ÷ best cost


@dataclass
class Estimate:
    day: date
    panels: GroupFit | None = None
    sensor: GroupFit | None = None
    offset: float | None = None        # seconds, solar clock = system − offset
    sigma: float | None = None
    used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.offset is not None


class Model:
    def __init__(self, lat, lon, days: list[DayProfile], cfg: ModelConfig | None = None, stats=None, meta=None):
        self.lat, self.lon = lat, lon
        self.days = sorted(days, key=lambda d: d.day)
        self.cfg = cfg or ModelConfig()
        self.stats = stats or {}
        self.meta = meta or {}
        self._windows: dict[float, list[bool]] = {}
        self._masked = {d.day: self._mask(d, self.days) for d in self.days}

    # ---- persistence -------------------------------------------------
    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({"version": 1, "lat": self.lat, "lon": self.lon, "meta": self.meta,
                       "stats": self.stats, "config": self.cfg.__dict__,
                       "days": [d.to_json() for d in self.days]}, f)

    @classmethod
    def load(cls, path: str, cfg: ModelConfig | None = None) -> "Model":
        with open(path) as f:
            d = json.load(f)
        return cls(d["lat"], d["lon"], [DayProfile.from_json(x) for x in d["days"]],
                   cfg or ModelConfig(**d.get("config", {})), d.get("stats"), d.get("meta"))

    # ---- profiles ----------------------------------------------------
    def _envelope(self, decl: float, pool: list[DayProfile]) -> list[float]:
        near = [d.panel_ref for d in pool if abs(d.decl - decl) <= self.cfg.env_window_deg] or \
               [d.panel_ref for d in pool]
        env = []
        for j in range(NB):
            vals = sorted(g[j] for g in near if not math.isnan(g[j]))
            env.append(vals[int(0.95 * (len(vals) - 1))] if len(vals) >= 3 else NAN)
        return env

    def _sensor_window(self, decl: float) -> list[bool]:
        """Minutes when the sensor is normally in direct sun at this time of year."""
        key = round(decl, 1)
        if key not in self._windows:
            near = [d for d in self.days if abs(d.decl - decl) <= self.cfg.env_window_deg] or self.days
            window = []
            for j in range(NB):
                vals = sorted(d.sensor[j] for d in near if not math.isnan(d.sensor[j]))
                window.append(len(vals) >= 3 and vals[int(0.8 * (len(vals) - 1))] > 0.7)
            self._windows[key] = window
        return self._windows[key]

    def sensor_clear_score(self, prof: DayProfile) -> float:
        """Share of the sensor's usual sunny window that was actually sunny."""
        window = self._sensor_window(prof.decl)
        vals = [prof.sensor[j] for j in range(NB) if window[j] and not math.isnan(prof.sensor[j])]
        if len(vals) < 0.5 * max(1, sum(window)):
            return 0.0
        return sum(v > 0.75 for v in vals) / len(vals)

    def _mask(self, prof: DayProfile, pool: list[DayProfile]) -> dict[str, list[float]]:
        """Channel grids with cloudy samples blanked out.

        Panel samples are kept only while the brightest panel shows direct
        sun; clouds dim every panel alike, so this is a clear-sky test.  The
        sensor is judged by whole days instead (see sensor_clear_score).
        """
        env = self._envelope(prof.decl, pool)
        ok = [not math.isnan(r) and not math.isnan(e) and r >= self.cfg.min_ref_watts
              and r >= self.cfg.env_fraction * e for r, e in zip(prof.panel_ref, env)]
        out = {"panel:" + k: [v if ok[j] else NAN for j, v in enumerate(g)] for k, g in prof.panels.items()}
        clear = self.sensor_clear_score(prof) >= self.cfg.sensor_clear_min
        out["sensor"] = list(prof.sensor) if clear else [NAN] * NB
        return out

    def _templates(self, decl0: float, pool: list[DayProfile]) -> dict[str, list[float]]:
        """Each channel's expected profile at declination decl0."""
        s = self.cfg.sigma_decl
        w = {d.day: math.exp(-((d.decl - decl0) / s) ** 2) for d in pool}
        channels = {}
        for d in pool:
            for ch, g in self._masked[d.day].items():
                channels.setdefault(ch, []).append((d, g))
        out = {}
        for ch, days in channels.items():
            grid = []
            for j in range(NB):
                pts = [(d.decl - decl0, g[j], w[d.day]) for d, g in days if not math.isnan(g[j])]
                if len(pts) < 3:
                    grid.append(NAN)
                    continue
                sw = sum(p[2] for p in pts)
                mx = sum(p[0] * p[2] for p in pts) / sw
                my = sum(p[1] * p[2] for p in pts) / sw
                sxx = sum(p[2] * (p[0] - mx) ** 2 for p in pts)
                b = sum(p[2] * (p[0] - mx) * (p[1] - my) for p in pts) / sxx if sxx > 1e-4 else 0.0
                grid.append(my - b * mx)
            out[ch] = grid
        return out

    # ---- fitting -----------------------------------------------------
    def _fit(self, obs: dict[str, list[float]], tmpl: dict[str, list[float]], min_points: int) -> GroupFit | None:
        cfg = self.cfg
        chans = []
        for ch, o in obs.items():
            t = tmpl.get(ch)
            if t is None:
                continue
            # Drop channels that never light up where they should (snow, dead).
            lit = [(o[j], t[j]) for j in range(NB) if not math.isnan(o[j]) and not math.isnan(t[j]) and t[j] > 0.7]
            if len(lit) >= 30 and sum(ov > 0.5 for ov, _ in lit) < 0.3 * len(lit):
                continue
            chans.append(ch)
        if not chans:
            return None
        grads = {ch: [j for j in range(1, NB - 1)
                      if not any(math.isnan(tmpl[ch][x]) for x in (j - 1, j, j + 1))
                      and abs(tmpl[ch][j + 1] - tmpl[ch][j - 1]) > 0.02] for ch in chans}
        trim2 = cfg.trim ** 2
        scores = []
        for s in range(-cfg.search_minutes, cfg.search_minutes + 1):
            total, n = 0.0, 0
            for ch in chans:
                o, t = obs[ch], tmpl[ch]
                for jj in grads[ch]:
                    j = jj + s
                    if 0 <= j < NB and not math.isnan(o[j]):
                        total += min((o[j] - t[jj]) ** 2, trim2)
                        n += 1
            scores.append((total / n if n >= min_points else math.inf, n))
        i = min(range(len(scores)), key=lambda k: scores[k][0])
        if math.isinf(scores[i][0]) or i in (0, len(scores) - 1):
            return None
        shift = float(i - cfg.search_minutes)
        r0, r1, r2 = (scores[k][0] for k in (i - 1, i, i + 1))
        if all(math.isfinite(x) for x in (r0, r2)) and r0 - 2 * r1 + r2 > 0:
            shift += 0.5 * (r0 - r2) / (r0 - 2 * r1 + r2)
        far = [c for k, (c, _) in enumerate(scores) if abs(k - i) >= 5 and math.isfinite(c)]
        sharp = (statistics.median(far) / scores[i][0]) if far and scores[i][0] > 0 else 0.0
        return GroupFit(shift * 60, scores[i][0], scores[i][1], chans, sharp)

    def observe(self, day: date, panel_series, sensor_samples) -> DayProfile:
        panels, ref = panel_profiles(panel_series, self.lon)
        sensor = sensor_profile(sensor_samples, self.lat, self.lon) if sensor_samples else [NAN] * NB
        return DayProfile(day, declination_deg(day, self.lon), panels, ref, sensor)

    def estimate(self, prof: DayProfile, exclude: set[date] = frozenset(), bias: float = 0.0) -> Estimate:
        cfg = self.cfg
        est = Estimate(prof.day)
        pool = [d for d in self.days if d.day not in exclude]
        if len(pool) < 5:
            est.notes.append("fewer than 5 training days")
            return est
        nearest = min(abs(d.decl - prof.decl) for d in pool)
        if nearest > cfg.max_extrapolation_deg:
            est.notes.append(f"model stale: nearest training declination {nearest:.1f}° away; re-run learn")
            return est
        tmpl = self._templates(prof.decl, pool)
        masked = self._mask(prof, pool)
        est.panels = self._fit({k: g for k, g in masked.items() if k.startswith("panel:")}, tmpl,
                               cfg.min_points_panels)
        score = self.sensor_clear_score(prof)
        if score >= cfg.sensor_clear_min:
            est.sensor = self._fit({"sensor": masked["sensor"]}, tmpl, cfg.min_points_sensor)
        else:
            est.notes.append(f"sensor: not clear enough ({score:.0%} of its sunny window)")

        good = {}
        for name, fit in (("panels", est.panels), ("sensor", est.sensor)):
            st = self.stats.get(name, {})
            if fit is None:
                if not any(n.startswith(name + ":") for n in est.notes):
                    est.notes.append(f"{name}: no usable data")
            elif fit.sharpness < cfg.min_sharpness:
                est.notes.append(f"{name}: ambiguous fit (sharpness {fit.sharpness:.1f})")
            elif st and fit.cost > cfg.max_cost_factor * st["median_cost"]:
                est.notes.append(f"{name}: poor match (cost {fit.cost:.3f} > {cfg.max_cost_factor}× {st['median_cost']:.3f})")
            else:
                good[name] = (fit.shift, st.get("rms") or 120.0)
        if "panels" in good and "sensor" in good:
            (sp, ep), (ss, es) = good["panels"], good["sensor"]
            if abs(sp - ss) > cfg.max_disagreement_sigma * math.hypot(ep, es):
                est.notes.append(f"panels and sensor disagree by {abs(sp - ss):.0f}s; using panels")
                del good["sensor"]
        if not good:
            return est
        wsum = sum(1 / e ** 2 for _, e in good.values())
        est.offset = sum(s / e ** 2 for s, e in good.values()) / wsum - bias
        est.sigma = math.sqrt(1 / wsum)
        est.used = sorted(good)
        return est

    # ---- learning ----------------------------------------------------
    def backtest(self, gap_days: int = 0, without_panels: bool = False) -> list[Estimate]:
        """Estimate each training day from the others (true answer: 0).

        gap_days > 0 also withholds the days just before the test day, to
        mimic serving from a model that is that many days old.
        without_panels simulates snow: the test day has no panel data.
        """
        out = []
        for d in self.days:
            excl = {x.day for x in self.days if 0 <= (d.day - x.day).days <= gap_days}
            if without_panels:
                d = DayProfile(d.day, d.decl, {}, [NAN] * NB, d.sensor)
            out.append(self.estimate(d, exclude=excl))
        return out

    def calibrate(self) -> None:
        """Fill in per-group cost and accuracy statistics from a back-test.

        The back-test withholds the week before each test day, so the
        statistics describe a model as stale as it gets between re-learns.
        """
        self.stats = {}
        results = self.backtest(gap_days=self.cfg.calibration_gap_days)
        for name in ("panels", "sensor"):
            fits = [getattr(e, name) for e in results if getattr(e, name)]
            if len(fits) < 3:
                continue
            med = statistics.median(f.cost for f in fits)
            ok = [f.shift for f in fits
                  if f.cost <= self.cfg.max_cost_factor * med and f.sharpness >= self.cfg.min_sharpness]
            # Too few days to trust: fall back to a conservative 120 s.
            rms = max(20.0, math.sqrt(sum(s * s for s in ok) / len(ok))) if len(ok) >= 5 else None
            self.stats[name] = {"median_cost": med, "rms": rms, "days": len(ok)}


def learn(lat, lon, profiles: list[DayProfile], cfg: ModelConfig | None = None, meta=None) -> Model:
    model = Model(lat, lon, profiles, cfg, meta=meta)
    model.calibrate()
    return model
