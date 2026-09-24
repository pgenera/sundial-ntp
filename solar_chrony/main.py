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
from .state import Log

log = logging.getLogger("solar-noon")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    site = cfg["site"]
    cfg["tz"] = ZoneInfo(site["timezone"]) if site.get("timezone") else datetime.now().astimezone().tzinfo
    known = {f.name for f in fields(ModelConfig)}
    cfg["model_cfg"] = ModelConfig(**{k: v for k, v in cfg.get("model", {}).items() if k in known})
    return cfg


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


def summarize(results, label: str) -> None:
    """Accuracy of the estimates that passed their quality gates."""
    for name in ("panels", "sensor", "combined"):
        vals = [e.offset for e in results if e.ok] if name == "combined" else \
               [getattr(e, name).shift for e in results if name in e.used]
        if vals:
            rms = math.sqrt(sum(v * v for v in vals) / len(vals))
            print(f"{label} {name:8}: {len(vals):3d} days  mean {statistics.mean(vals):+6.1f}s  "
                  f"rms {rms:6.1f}s  worst {max(map(abs, vals)):6.1f}s")


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
    summarize(results, f"gap={args.gap}{' no-panels' if args.without_panels else ''}")
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
    if state.done_on(today) and not args.force:
        log.info("already done for %s", today)
        return 0
    model = Model.load(cfg["storage"]["model"], cfg["model_cfg"])
    panels, sensor = fetch_day(cfg, Prometheus(cfg["prometheus"]["url"]), today, until=now)
    est = model.estimate(model.observe(today, panels, sensor),
                         bias=cfg.get("apply", {}).get("bias_seconds", 0.0))
    log.info("%s", describe(est))
    if not est.ok or args.dry_run:
        if est.ok:
            log.info("dry run: would set solar clock to system time %+.1fs", -est.offset)
        else:
            state.record(est, now, applied=False)
        return 0
    chronyc = Chronyc(cfg["chrony"]["socket"], cfg["chrony"].get("chronyc", "chronyc"))
    out = chronyc.set_solar_offset(est.offset)
    state.record(est, now, applied=True)
    log.info("chronyc: %s", " | ".join(line for line in out.splitlines() if line.strip()))
    return 0


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

    a = sub.add_parser("apply", help="estimate today and feed it to chronyd (run after sunset)")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true", help="apply even if already applied today")
    a.set_defaults(func=cmd_apply)

    s = sub.add_parser("status", help="show the solar chronyd's tracking and manual samples")
    s.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    return args.func(load_config(args.config), args)


if __name__ == "__main__":
    sys.exit(main())
