# Running it

## Before you start

You need a Linux host running chrony 4.x (this was built on Debian with chrony 4.9) and Python 3.11+. There are no Python dependencies. You also need a Prometheus that has:

- per-panel power, one series per inverter, with a label that tells the panels apart. With [sunpower-pvs-exporter](https://github.com/pgenera/sunpower-pvs-exporter) that's `sunpower_pvs_inverter_ac_power_watts`, keyed by `device_id`.
- optionally, a W/m² irradiance series for the snow fallback.

It needs a few weeks of history before the model is worth anything. Two months is better.

If the host already runs chrony for its own clock, that's fine; the two instances don't share anything. The solar one listens on a single IPv6 address. If the main chronyd ever starts serving NTP too, give it a `bindaddress` so the two don't both try to own `[::]:123`.

## Configuration

Two files, kept in `site/` (git-ignored):

**`site/solar.toml`**, from `config.example.toml`:

- `[site]`: latitude, longitude (negative is west), time zone. The time zone only decides what "today" means.
- `[prometheus]`: URL, the per-panel selector, the label that identifies each panel, and the irradiance selector. Leave `sensor` empty if you don't have one.
- `[storage]`: where the model and the estimate log live, under `/var/lib/solar-noon/`.
- `[feed]`: the SHM unit (must match chrony's `refclock SHM <unit>`), and how many days without a fix before it stops feeding.
- `[apply]`: the sun elevation below which the evening estimate runs.
- `[model]`: the quality gates. The defaults came from about two months of real data, so leave them alone unless back-testing says otherwise.

**`site/chrony.conf`**, from `deploy/chrony-solar.conf`. Set `bindaddress` to the IPv6 address to serve on, and `allow` to whoever may use it. Everything else should be left as it is.

## Installing and updating

```sh
sudo deploy/install.sh
```

Run it from the repository root. It's idempotent, so re-run it after pulling changes or editing `site/`. It:

1. creates a `chrony-solar` system user in the `_chrony` group;
2. copies the code to `/opt/chrony-solar` and the config to `/etc/chrony-solar`;
3. appends rules to `/etc/apparmor.d/local/usr.sbin.chronyd` (Debian confines chronyd to its usual paths);
4. installs and starts the units:
   - `chronyd-solar.service`, the second chronyd;
   - `solar-noon-feed.service`, the SHM feeder;
   - `solar-noon.timer`, the evening estimate, every 30 minutes from 16:00 to 23:30;
   - `solar-noon-learn.timer`, the weekly re-learn, Sundays at 03:00;
5. learns a first model.

Until the first clear evening, the server answers at stratum 10 with plain system time. That's expected.

## Privileges

`chronyd-solar` runs as `_chrony` from the start, never as root. The only capability it gets is `CAP_NET_BIND_SERVICE`. `ProtectClock=yes` and `SystemCallFilter=~@clock` mean the kernel will refuse it if it ever tries to change the clock.

The feed runs as `chrony-solar` with no network access at all (`PrivateNetwork=yes`). The evening estimate and the learner run as `chrony-solar` and only need to reach Prometheus.

## Day to day

Anyone on the host can look at the solar clock, read-only:

```sh
chronyc -h ::1 -p 11323 -m tracking sources
```

With `SUN` selected you'll see `#* SUN`, stratum 1, and a "System time" that's the sun's current opinion of the system clock.

The rest goes through the CLI as the service user:

```sh
cd /opt/chrony-solar
run() { sudo -u chrony-solar python3 -m solar_chrony.main -c /etc/chrony-solar/solar.toml "$@"; }

run backtest --gap 7          # how a week-old model would have done on each learned day
run backtest --without-panels # the same, pretending the panels are snowed over
run apply --dry-run           # tonight's estimate, without publishing it (after sunset)
run learn                     # re-learn now instead of waiting for Sunday
run status                    # tracking and sources over the Unix socket
```

Every evening's result, accepted or not, goes in `/var/lib/solar-noon/log.db`:

```sh
sudo sqlite3 /var/lib/solar-noon/log.db \
  'select day, round(offset), round(sigma), used, applied, notes from estimates order by run_at desc limit 10'
```

`notes` says why a day was rejected. The usual reasons are "ambiguous fit", "not clear enough" or "no usable data", all of which mean clouds.

Logs are in the journal under `solar-noon`, `solar-noon-feed`, `solar-noon-learn` and `chronyd-solar`. chronyd also writes `tracking.log` and `refclocks.log` to `/var/log/chrony-solar/`.

## When things go wrong

**The feed logs "no NTP SHM segment".** chronyd isn't running, or its `refclock SHM` unit doesn't match `[feed] shm_unit`. Check `ipcs -m`: unit 7 is key `0x4e545037`, and it should be owned by `_chrony` with mode 660. If some other process created that key first, with the wrong owner or permissions, chronyd can't use it. Remove it with `ipcrm -M 0x4e545037` and restart `chronyd-solar`.

**`SUN` shows up in `sources` but never gets selected.** Normal until the first accepted fix; the feed deliberately posts nothing until then. After that, check `journalctl -u solar-noon-feed` for "too old" (no fix for `max_age_days`).

**`apply` says "model stale".** Today's sun path is more than 4° of declination from anything in the model. That means the weekly re-learn hasn't been running. Run `learn`, then check `systemctl list-timers 'solar-noon*'`.

**`apply` says a model "was learned with a different day window".** The code changed the profile format. Run `learn` again.

**chronyd-solar won't start on Debian.** AppArmor probably. Look for `apparmor="DENIED"` in `journalctl -k`. The installer adds paths for `/etc/chrony-solar`, `/run/chrony-solar`, `/var/lib/chrony-solar` and `/var/log/chrony-solar`; anything else you add needs the same treatment.

**Accuracy got worse.** Probably leaf fall or new shade. Run `backtest --gap 7` and look at the per-day lines. A shift that sits consistently to one side means the shade has moved since the model was learned. Re-learning fixes that once enough new days are in.

## Removing it

```sh
sudo systemctl disable --now solar-noon.timer solar-noon-learn.timer solar-noon-feed.service chronyd-solar.service
sudo rm /etc/systemd/system/{chronyd-solar,solar-noon,solar-noon-feed,solar-noon-learn}.service \
        /etc/systemd/system/{solar-noon,solar-noon-learn}.timer
sudo systemctl daemon-reload
sudo rm -r /opt/chrony-solar /etc/chrony-solar /var/lib/solar-noon /var/lib/chrony-solar /var/log/chrony-solar
sudo ipcrm -M 0x4e545037
sudo userdel chrony-solar
```

Then take the `chrony-solar` lines back out of `/etc/apparmor.d/local/usr.sbin.chronyd`.
