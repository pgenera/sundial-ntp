# sundial-ntp

A stratum-1 NTP server whose reference clock is the sun. More precisely, it's the shadows that trees and the roofline cast on a set of rooftop solar panels. Clients see the reference ID `SUN`.

It's good to about ±45 seconds per day, which is terrible for an NTP server and excellent for a sundial.

## What it is

The host already has an ordinary chronyd keeping its system clock right. This project runs a second chronyd next to it that never touches the system clock (`-x`, and systemd blocks clock syscalls outright). It uses the system clock as a free-running baseline and takes its idea of the time of day from the sun. That second chronyd is the one that answers NTP queries.

The original plan was to find solar noon from a weather station's irradiance sensor and set the clock by that. The sensor turned out to be in shade for most of the day, so its peak says more about the neighbour's tree than about the sun. That approach was off by 8 to 30 minutes.

What does work is the shade itself. Each of the 17 panels on the roof has its own window of direct sun, bounded by fixed obstacles. The edges of those windows happen at the same sun positions every day. So each evening the code compares today's per-panel production against what it has learned about where the shade edges fall, finds the time shift that lines them up best, and publishes that offset. A small feeder hands it to chronyd through the same shared-memory refclock interface gpsd uses for GPS. The weather station stays on as a backup for days when the panels are covered in snow.

[docs/how-it-works.md](docs/how-it-works.md) has the details, including why the model is calibrated against the system clock once a week and why that doesn't make the whole thing a fraud.

## How well it works

Back-tested over 60 days of real data. The system clock is correct, so the right answer is always 0:

| model age | days with a fix | mean | RMS | worst |
|---|---|---|---|---|
| fresh | 21 | 0 s | 34 s | 101 s |
| a week old | 15 | +9 s | 47 s | 94 s |
| two weeks old | 11 | +5 s | 43 s | 114 s |
| panels "snowed over" (sensor only) | 5 | +16 s | 37 s | 55 s |

Cloudy days produce no fix at all rather than a bad one, and chronyd coasts until the next clear evening.

## What you need

- chrony 4.x on Linux. It's tested with 4.9 on Debian; the hardening assumes systemd.
- Python 3.11 or newer. It uses only the standard library.
- Prometheus with per-panel AC power. Ours comes from [sunpower-pvs-exporter](https://github.com/pgenera/sunpower-pvs-exporter) (`sunpower_pvs_inverter_ac_power_watts`), but any per-panel power series will do.
- Optionally, an irradiance sensor in Prometheus for the snow fallback. Ours reaches Prometheus through Home Assistant's exporter.
- A few weeks of history, so there's something to learn from.

[docs/operations.md](docs/operations.md) covers installing, configuring, watching and fixing it.

## Quick start

```sh
mkdir -p site
cp config.example.toml site/solar.toml          # latitude, longitude, Prometheus URL
cp deploy/chrony-solar.conf site/chrony.conf    # bindaddress, allow
sudo deploy/install.sh
```

`site/` is git-ignored, so your coordinates stay out of the repo.

## Layout

```
solar_chrony/    the program: solar.py (NOAA sun position), model.py (the shade model),
                 shm.py (NTP SHM writer), promsource.py, state.py, chrony.py, main.py
deploy/          chrony config, systemd units, AppArmor snippet, install.sh
tests/           pytest; test_chrony.py runs a throwaway chronyd
analysis/        the exploration that led here, charts included
docs/            how it works, and how to run it
```

## License

MIT. See [LICENSE](LICENSE).
