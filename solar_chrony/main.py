"""solar-noon: discipline a chronyd instance by where the shadows fall."""

import argparse
import logging
import math
import statistics
import sys
import time
import tomllib
from dataclasses import fields
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import solar
from .chrony import Chronyc
from .model import Model, ModelConfig, learn
from .promsource import Prometheus
from .shm import SegmentMissing, SHMWriter
from .state import Log, smooth

log = logging.getLogger("solar-noon")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    site = cfg["site"]
    cfg["tz"] = ZoneInfo(site["timezone"]) if site.get("timezone") else datetime.now().astimezone().tzinfo
    known = {f.name for f in fields(ModelConfig)}
    cfg["model_cfg"] = ModelConfig(**{k: v for k, v in cfg.get("model", {}).items() if k in known})
    return cfg


def smoothing(cfg) -> tuple[int, str]:
    feed = cfg.get("feed", {})
    return int(feed.get("smoothing_days", 7)), feed.get("smoothing", "median")


def day_bounds(day: date, tz) -> tuple[float, float]:
    start = datetime.combine(day, dtime(0), tz)
    end = datetime.combine(day + timedelta(days=1), dtime(0), tz)
    return start.timestamp(), end.timestamp()


def fetch_day(cfg, prom: Prometheus, day: date, until: float | None = None):
    s, e = day_bounds(day, cfg["tz"])
    if until is not None:
        e = min(e, until)
    p = cfg["prometheus"]
    panels = prom.raw(p["panels"], s, e, key_label=p.get("panel_label", "device_id"))
    sensor = prom.raw(p["sensor"], s, e) if p.get("sensor") else {}
    return panels, next(iter(sensor.values()), [])


def _fmt_fit(fit) -> str:
    return (f"{fit.shift:+6.0f}s ({len(fit.channels)}ch n={fit.points} sharp={fit.sharpness:.1f})"
            if fit else "     -")


def describe(est) -> str:
    head = f"{est.day}  panels {_fmt_fit(est.panels)}  sensor {_fmt_fit(est.sensor)}"
    if est.ok:
        head += f"  => offset {est.offset:+.0f}s ±{est.sigma:.0f} [{'+'.join(est.used)}]"
    else:
        head += "  => no estimate"
    return head + ("  (" + "; ".join(est.notes) + ")" if est.notes else "")


def served_series(results, days: int, stat: str, max_age_days: int = 14) -> list[float]:
    """What clients would have been served on each day after the first fix."""
    fixes = sorted((e.day, e.offset) for e in results if e.ok)
    out = []
    for e in sorted(results, key=lambda e: e.day):
        known = [f for f in fixes if f[0] <= e.day]
        if known and (e.day - known[-1][0]).days <= max_age_days:
            out.append(smooth(known, days, stat)[0])
    return out


def summarize(results, label: str, cfg=None) -> None:
    """Accuracy of the estimates that passed their quality gates."""
    for name in ("panels", "sensor", "combined"):
        vals = [e.offset for e in results if e.ok] if name == "combined" else \
               [getattr(e, name).shift for e in results if name in e.used]
        if vals:
            rms = math.sqrt(sum(v * v for v in vals) / len(vals))
            print(f"{label} {name:8}: {len(vals):3d} days  mean {statistics.mean(vals):+6.1f}s  "
                  f"rms {rms:6.1f}s  worst {max(map(abs, vals)):6.1f}s")
    if cfg is not None:
        days, stat = smoothing(cfg)
        vals = served_series(results, days, stat, cfg.get("feed", {}).get("max_age_days", 14))
        if vals:
            rms = math.sqrt(sum(v * v for v in vals) / len(vals))
            print(f"{label} served  : {len(vals):3d} days  mean {statistics.mean(vals):+6.1f}s  "
                  f"rms {rms:6.1f}s  worst {max(map(abs, vals)):6.1f}s  ({stat} of {days} days, every day after the first fix)")


def cmd_learn(cfg, args) -> int:
    """Build the shading model from recent history, timed by the system clock."""
    prom = Prometheus(cfg["prometheus"]["url"])
    lat, lon = cfg["site"]["latitude"], cfg["site"]["longitude"]
    end = date.fromisoformat(args.end) if args.end else datetime.now(cfg["tz"]).date() - timedelta(days=1)
    probe = Model(lat, lon, [], cfg["model_cfg"])
    profiles = []
    for k in range(args.days - 1, -1, -1):
        day = end - timedelta(days=k)
        panels, sensor = fetch_day(cfg, prom, day)
        profiles.append(probe.observe(day, panels, sensor))
        log.debug("%s: %d panels, %d sensor samples", day, len(panels), len(sensor))
    meta = {"learned_at": time.time(), "first_day": profiles[0].day.isoformat(),
            "last_day": profiles[-1].day.isoformat()}
    model = learn(lat, lon, profiles, cfg["model_cfg"], meta=meta)
    model.save(cfg["storage"]["model"])
    decls = [p.decl for p in profiles]
    print(f"learned {len(profiles)} days {meta['first_day']}..{meta['last_day']} "
          f"(declination {min(decls):+.1f}°..{max(decls):+.1f}°) -> {cfg['storage']['model']}")
    for name, st in model.stats.items():
        rms = f"{st['rms']:.0f}s" if st["rms"] is not None else "-"
        print(f"  {name:7}: back-test rms {rms} over {st['days']} days (median cost {st['median_cost']:.3f})")
    return 0


def cmd_backtest(cfg, args) -> int:
    model = Model.load(cfg["storage"]["model"], cfg["model_cfg"])
    results = model.backtest(gap_days=args.gap, without_panels=args.without_panels)
    for est in results:
        print(describe(est))
    print()
    summarize(results, f"gap={args.gap}{' no-panels' if args.without_panels else ''}", cfg)
    return 0


def cmd_apply(cfg, args) -> int:
    tz = cfg["tz"]
    lat, lon = cfg["site"]["latitude"], cfg["site"]["longitude"]
    now = time.time()
    today = datetime.now(tz).date()
    max_elev = cfg.get("apply", {}).get("max_sun_elevation", 5.0)
    elev = solar.elevation(now, lat, lon)
    if now < solar.solar_noon_utc(today, lon) or elev > max_elev:
        log.info("sun not yet down (elevation %.1f°); nothing to do", elev)
        return 0
    state = Log(cfg["storage"]["log"])
    if state.done_on(today) and not args.force and not args.dry_run:
        log.info("already done for %s", today)
        return 0
    model = Model.load(cfg["storage"]["model"], cfg["model_cfg"])
    panels, sensor = fetch_day(cfg, Prometheus(cfg["prometheus"]["url"]), today, until=now)
    est = model.estimate(model.observe(today, panels, sensor),
                         bias=cfg.get("apply", {}).get("bias_seconds", 0.0))
    log.info("%s", describe(est))
    if args.dry_run:
        if est.ok:
            log.info("dry run: would publish offset %+.1fs (solar clock = system %+.1fs)", est.offset, -est.offset)
        return 0
    state.record(est, now, applied=est.ok)
    if est.ok:
        days, stat = smoothing(cfg)
        served, used = smooth([(d, o) for d, o, _ in state.published()], days, stat)
        log.info("published %+.1fs; the feed now serves the %s of %d fix(es): system time %+.1fs",
                 est.offset, stat, len(used), -served)
    return 0


def cmd_feed(cfg, args) -> int:
    """Serve the smoothed published offset to chronyd's SHM refclock, forever."""
    feed = cfg.get("feed", {})
    unit = feed.get("shm_unit", 7)
    max_age = feed.get("max_age_days", 14) * 86400
    days, stat = smoothing(cfg)
    state = Log(cfg["storage"]["log"])
    writer, served, newest, last_check, said = None, None, None, 0.0, None
    while True:
        now = time.time()
        if writer is None:
            try:
                writer = SHMWriter(unit)
                log.info("attached to NTP SHM unit %d", unit)
            except SegmentMissing as e:
                if said != "missing":
                    log.warning("%s; waiting for chronyd", e)
                    said = "missing"
                time.sleep(5)
                continue
        if now - last_check >= 60:
            fixes = state.published()
            newest = fixes[-1] if fixes else None
            served, used = smooth([(d, o) for d, o, _ in fixes], days, stat)
            last_check = now
        if newest is None or now - newest[2] > max_age:
            msg = "no solar fix yet" if newest is None else f"last solar fix ({newest[0]}) is too old"
            if said != msg:
                log.warning("%s; not feeding chronyd", msg)
                said = msg
            writer.invalidate()
        else:
            msg = f"{served:.3f}"
            if said != msg:
                log.info("serving the %s of %d fix(es) up to %s: system time %+.1fs",
                         stat, len(used), newest[0], -served)
                said = msg
            writer.put(now, now - served)
        if args.once:
            return 0
        time.sleep(1)


def cmd_status(cfg, args) -> int:
    print(Chronyc(cfg["chrony"]["socket"], cfg["chrony"].get("chronyc", "chronyc")).status(), end="")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="solar-noon", description=__doc__)
    p.add_argument("-c", "--config", default="/etc/chrony-solar/solar.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("learn", help="build the shading model from history (uses the system clock)")
    l.add_argument("--days", type=int, default=60)
    l.add_argument("--end", help="last day to learn from, YYYY-MM-DD (default: yesterday)")
    l.set_defaults(func=cmd_learn)

    b = sub.add_parser("backtest", help="estimate each learned day from the others")
    b.add_argument("--gap", type=int, default=0, help="also withhold this many preceding days")
    b.add_argument("--without-panels", action="store_true", help="simulate snow-covered panels")
    b.set_defaults(func=cmd_backtest)

    a = sub.add_parser("apply", help="estimate today and publish it to the feed (run after sunset)")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true", help="apply even if already applied today")
    a.set_defaults(func=cmd_apply)

    f = sub.add_parser("feed", help="serve the latest published offset to chronyd's SHM refclock")
    f.add_argument("--once", action="store_true", help="post one sample and exit")
    f.set_defaults(func=cmd_feed)

    s = sub.add_parser("status", help="show the solar chronyd's tracking and sources")
    s.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    return args.func(load_config(args.config), args)


if __name__ == "__main__":
    sys.exit(main())
