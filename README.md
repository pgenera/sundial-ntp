# chrony-solar

A stratum-1 NTP server whose time of day comes from where the shadows fall.

A second `chronyd` runs with `-x`: it never touches the system clock. It
uses the (already well-disciplined) system clock as a free-running baseline
and keeps its own offset and frequency relative to it. Once each evening,
`solar-noon apply` works out how far off the solar clock is from today's
sunlight and tells that chronyd what time it is, via `chronyc manual on` /
`settime`.

## Why shadows, not solar noon

The site is heavily shaded, so "when did the sunlight peak?" has no clean
answer:

- the weather-station irradiance sensor only gets direct sun from about
  −95 to +125 minutes around noon;
- each of the 17 roof panels has its own window of direct sun, bounded by
  trees and roofline.

A curve fit to the day's light was off by 8–30 minutes. The shade edges,
though, happen at fixed sun positions and repeat day after day. That makes
about 34 timed events per clear day across the panels, plus two on the
sensor.

## How it works

Every channel (each panel, and the sensor) becomes a per-minute
**lit-fraction profile** against the hour angle (minutes from solar noon):

- **Panels:** each panel's power divided by the second-brightest panel at
  the same poll. Clouds dim every panel alike and cancel out. Samples count
  only while the brightest panel shows direct sun.
- **Sensor:** W/m² divided by a clear-sky model. It counts only on days
  when at least 70% of its usual sunny window was actually sunny.

The two phases:

- **Learn** (`solar-noon learn`, weekly timer): keeps about 60 days of
  profiles, timed by the **system clock**. This is the one place a clock is
  consulted, so it amounts to calibrating a sundial. It also back-tests the
  model as if it were a week old, which sets the quality gates and the
  expected error of each group.
- **Serve** (`solar-noon apply`, evenings): builds each channel's expected
  profile for today's solar declination from the frozen model. It then finds
  the single time shift that best lines today's observations up with them.
  The panels and the sensor are fitted as separate groups. Each group must
  have enough edge data and a clearly defined best shift; the two results
  are combined, weighted by their back-tested accuracy.
  - **Snow on all panels:** the panels show no direct sun, so the sensor
    carries the estimate alone.
  - **One panel covered or dead:** it's dropped for the day.
  - **Overcast day:** no estimate; chronyd runs on until tomorrow.

## Accuracy

These numbers come from 60 days of data (2026-07-26 to 09-23; per-panel
data starts 2026-08-21). The true answer is 0, because the system clock is
right:

| Model age | Days with an estimate | Mean | RMS | Worst |
|---|---|---|---|---|
| fresh (leave-one-out) | 21 | 0 s | 34 s | 101 s |
| 7 days old | 15 | +9 s | 47 s | 94 s |
| 14 days old | 11 | +5 s | 43 s | 114 s |
| panels snowed over (sensor only), fresh | 5 | +16 s | 37 s | 55 s |

The sensor-only fallback has little history to learn from so far: only a
handful of days were clear across its whole sunny window. It improves as
the archive grows, and it's calibrated as a week-old model, so its weight
starts conservative (120 s) until at least 5 days back it up.

The server advertises stratum 1 (`local stratum 1`; manual mode on its own
serves "unsynchronised"). chronyd keeps up to 16 daily samples and regresses
a frequency from them, so expect a few ppm of wander.

## Privileges

The solar chronyd runs as `_chrony` from the start and never has root. Its
only capability is `CAP_NET_BIND_SERVICE`, for binding :123.
`ProtectClock=yes` and `SystemCallFilter=~@clock` make the kernel refuse
any clock adjustment. It listens only on
the IPv6 address set by `bindaddress` (port 123), with
no IPv4 sockets.

## Layout

```
solar_chrony/solar.py       NOAA solar position (all UTC)
solar_chrony/model.py       profiles, learning, templates, shift fit, back-test
solar_chrony/promsource.py  raw samples from Prometheus
solar_chrony/chrony.py      chronyc wrapper (manual on / settime)
solar_chrony/state.py       log of daily estimates
solar_chrony/main.py        CLI: learn | backtest | apply | status
deploy/                     chrony config, systemd units, AppArmor snippet
analysis/                   the exploration that led here (charts, prototypes)
```

Data sources, both read from Prometheus (the `[prometheus] url` in the config):

- `sunpower_pvs_inverter_ac_power_watts`: per panel, refreshed every 5 min.
- `ha_sensor_unit_watts_per_square_meter{entity="sensor.brightness"}`: the
  weather station via Home Assistant's exporter, scraped every 30 s.

## Install

Make this site's config from the examples, in `site/`, which git ignores:

```sh
mkdir -p site
cp config.example.toml site/solar.toml         # set latitude, longitude, Prometheus url
cp deploy/chrony-solar.conf site/chrony.conf   # set bindaddress and allow
```

Then install, or update in place:

```sh
sudo deploy/install.sh
```

The script:
- creates the `chrony-solar` user;
- copies the code to `/opt/chrony-solar` and the config to `/etc/chrony-solar`;
- adds the AppArmor rules (Debian confines `/usr/sbin/chronyd` to its usual paths);
- installs the systemd units and starts the solar chronyd;
- learns the first model;
- enables the evening `apply` timer and the weekly `learn` timer.

Things to check:

- **Allowed clients.** `allow` in `chrony.conf` defaults to the local /64.
  Widen it if others should use the server, and open UDP/123 on IPv6.
- **Primary chrony.** If it ever gets `allow`, give it a `bindaddress` too
  so the two don't fight over `[::]:123`.

## Use

```sh
cd /opt/chrony-solar
run() { sudo -u chrony-solar python3 -m solar_chrony.main -c /etc/chrony-solar/solar.toml "$@"; }
run learn                        # rebuild the model (the weekly timer does this)
run backtest --gap 7             # how well would a week-old model have done?
run backtest --without-panels    # ...with the panels snowed over?
run apply --dry-run              # tonight's estimate, without touching chronyd
run status                       # the solar chronyd's tracking and manual samples
journalctl -u solar-noon -u solar-noon-learn
```

Each evening's result, applied or rejected with a reason, goes into
`/var/lib/solar-noon/log.db`.

## Tests

```sh
python3 -m pytest -q
```

- `test_model.py` builds a synthetic site: 12 panels and a sensor, each
  with its own shade window that drifts with the season. It checks that
  known time shifts are recovered, that snow falls back to the sensor, that
  a covered panel is dropped, and that overcast days and a stale model are
  refused.
- `test_chrony.py` starts a throwaway, unprivileged `chronyd -x` and checks
  that `settime` moves the served time and not the system clock.
