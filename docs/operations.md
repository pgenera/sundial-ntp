# Running it

## Before you start

You need a Linux host running chrony 4.x (this was built on Debian with chrony 4.9) and Python 3.11+. There are no Python dependencies. You also need a Prometheus that has:

- per-panel power, one series per inverter, with a label that tells the panels apart. With [sunpower-pvs-exporter](https://github.com/pgenera/sunpower-pvs-exporter) that's `sunpower_pvs_inverter_ac_power_watts`, keyed by `device_id`.
- optionally, a W/m² irradiance series for the snow fallback.

It needs a few weeks of history before the model is worth anything. Two months is better.

If the host already runs chrony for its own clock, that's fine; the two instances don't share anything. The solar one listens on a single IPv6 address, or on every address of one interface with `binddevice`. If the main chronyd ever starts serving NTP too, give it a `bindaddress` so the two don't both try to own `[::]:123`.

## Configuration

Two files, kept in `site/` (git-ignored):

**`site/solar.toml`**, from `config.example.toml`:

- `[site]`: latitude, longitude (negative is west), time zone. The time zone only decides what "today" means.
- `[prometheus]`: URL, the per-panel selector, the label that identifies each panel, and the irradiance selector. Leave `sensor` empty if you don't have one.
- `[storage]`: where the model and the estimate log live, under `/var/lib/solar-noon/`.
- `[feed]`: the SHM unit (must match chrony's `refclock SHM <unit>`), how many days without a fix before it stops feeding, and the smoothing: the median (or mean) of the accepted fixes over the last `smoothing_days`. Set it to 1 to serve only the latest fix. `refclock_conf`, `error_factor` and `error_growth_per_day` set the error bound advertised to clients; see [how-it-works.md](how-it-works.md#telling-clients-how-wrong-it-is).
- `[apply]`: the sun elevation below which the evening estimate runs.
- `[model]`: the quality gates. The defaults came from about two months of real data, so leave them alone unless back-testing says otherwise.

**`site/chrony.conf`**, from `deploy/chrony-solar.conf`. Set `bindaddress` to the IPv6 address to serve on (chronyd takes only one; use `binddevice <interface>` instead to serve every address on an interface), and `allow` to whoever may use it, `::/0` for everyone. Everything else should be left as it is.

The public server is `2001:470:1f07:e17::1200`, served alongside the host's EUI-64 address on the tunnel VLAN `eno1.8` (hence `binddevice eno1.8`). With systemd-networkd, a fixed suffix like that is one more token in the interface's `.network` file, next to the SLAAC one:

```ini
[IPv6AcceptRA]
Token=eui64
Token=static:::1200
```

then `sudo networkctl reload`. Pick a suffix that survives being printed. Tools print addresses in RFC 5952 form, which collapses the longest run of zero groups, so `::12:0:0` comes out as `0:12::`. `::1200` prints as written.

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
   - `chronyd-solar-refclock.path`, which restarts it when the feed rewrites `/var/lib/solar-noon/refclock.conf` with a new error bound;
   - `solar-noon-feed.service`, the SHM feeder;
   - `solar-noon.timer`, the evening estimate, every 30 minutes from 16:00 to 23:30;
   - `solar-noon-learn.timer`, the daily re-learn, at 03:00;
   - `chrony-solar-exporter.service`, a [chrony_exporter](https://github.com/SuperQ/chrony_exporter) on port 9124, if `/opt/chrony-exporter/chrony_exporter` exists (see [Monitoring](#monitoring));
5. learns a first model.

Before that it writes the first `refclock.conf`, since chronyd won't start without it.

Until the first clear evening, the server answers at stratum 10 with plain system time. That's expected.

## Privileges

`chronyd-solar` runs as `_chrony` from the start, never as root. The only capability it gets is `CAP_NET_BIND_SERVICE`. `ProtectClock=yes` and `SystemCallFilter=~@clock` mean the kernel will refuse it if it ever tries to change the clock.

The feed runs as `chrony-solar` with no network access at all (`PrivateNetwork=yes`). The evening estimate and the learner run as `chrony-solar` and only need to reach Prometheus.

## Day to day

Anyone on the host can look at the solar clock, read-only:

```sh
chronyc -h ::1 -p 11323 -m tracking sources
```

With `SUN` selected you'll see `#* SUN`, stratum 1, and a "System time" that's the sun's current opinion of the system clock. "Root dispersion" is the error bound from `/var/lib/solar-noon/refclock.conf`.

The rest goes through the CLI as the service user:

```sh
cd /opt/chrony-solar
run() { sudo -u chrony-solar python3 -m solar_chrony.main -c /etc/chrony-solar/solar.toml "$@"; }

run backtest --gap 7          # how a week-old model would have done, per fix and as served
run backtest --without-panels # the same, pretending the panels are snowed over
run apply --dry-run           # tonight's estimate, without publishing it (after sunset)
run learn                     # re-learn now instead of waiting for 03:00
run status                    # tracking and sources over ::1 port 11323
run refclock                  # rewrite refclock.conf with the current error bound now
```

Every evening's result, accepted or not, goes in `/var/lib/solar-noon/log.db`:

```sh
sudo sqlite3 /var/lib/solar-noon/log.db \
  'select day, round(offset), round(sigma), used, applied, notes from estimates order by run_at desc limit 10'
```

`notes` says why a day was rejected. The usual reasons are "ambiguous fit", "not clear enough" or "no usable data", all of which mean clouds.

Logs are in the journal under `solar-noon`, `solar-noon-feed`, `solar-noon-learn` and `chronyd-solar`. chronyd writes no statistics files; the feed logs a line whenever the offset it serves changes.

## Monitoring

`chrony-solar-exporter.service` runs chrony_exporter against the solar chronyd's command socket, so it reports `serverstats` as well as tracking and sources. The installer doesn't fetch the binary. Put it in place once, then re-run the installer:

```sh
curl -LO https://github.com/SuperQ/chrony_exporter/releases/download/v0.14.0/chrony_exporter-0.14.0.linux-amd64.tar.gz
sudo install -D -m 0755 <(tar xzOf chrony_exporter-0.14.0.linux-amd64.tar.gz chrony_exporter-0.14.0.linux-amd64/chrony_exporter) \
        /opt/chrony-exporter/chrony_exporter
```

Then scrape `HOST:9124`. `chrony_up` says whether chronyd answers, and `rate(chrony_serverstats_ntp_packets_received_total[15m])` says whether NTP requests are reaching it.

## When things go wrong

**The feed logs "no NTP SHM segment".** chronyd isn't running, or its `refclock SHM` unit doesn't match `[feed] shm_unit`. Check `ipcs -m`: unit 7 is key `0x4e545037`, and it should be owned by `_chrony` with mode 660. If some other process created that key first, with the wrong owner or permissions, chronyd can't use it. Remove it with `ipcrm -M 0x4e545037` and restart `chronyd-solar`.

**`SUN` shows up in `sources` but never gets selected.** Normal until the first accepted fix; the feed deliberately posts nothing until then. After that, check `journalctl -u solar-noon-feed` for "too old" (no fix for `max_age_days`), in which case `/var/lib/solar-noon/refclock.conf` says `noselect`. If `sources -v` marks it `d` (distance too large), `chrony.conf` is missing the raised `maxdistance`.

**chronyd-solar restarts now and then.** That's the error bound changing: at each new fix, and once a day as the newest fix ages. `journalctl -u chronyd-solar-refclock` lists them. More often than that means something else is rewriting `refclock.conf`.

**`apply` says "model stale".** Today's sun path is more than 4° of declination from anything in the model. That means the daily re-learn hasn't been running. Run `learn`, then check `systemctl list-timers 'solar-noon*'`.

**`apply` says a model "was learned with a different day window".** The code changed the profile format. Run `learn` again.

**chronyd-solar won't start on Debian.** AppArmor probably. Look for `apparmor="DENIED"` in `journalctl -k`. The installer adds paths for `/etc/chrony-solar`, `/run/chrony-solar` and `/var/lib/chrony-solar`; anything else you add needs the same treatment.

**Accuracy got worse.** Probably leaf fall or new shade. Run `backtest --gap 7` and look at the per-day lines. A shift that sits consistently to one side means the shade has moved since the model was learned. Re-learning fixes that once enough new days are in.

## Removing it

```sh
sudo systemctl disable --now solar-noon.timer solar-noon-learn.timer chronyd-solar-refclock.path \
        solar-noon-feed.service chronyd-solar.service chrony-solar-exporter.service
sudo rm /etc/systemd/system/{chronyd-solar,chronyd-solar-refclock,solar-noon,solar-noon-feed,solar-noon-learn,chrony-solar-exporter}.service \
        /etc/systemd/system/{solar-noon,solar-noon-learn}.timer /etc/systemd/system/chronyd-solar-refclock.path
sudo systemctl daemon-reload
sudo rm -r /opt/chrony-solar /etc/chrony-solar /var/lib/solar-noon /var/lib/chrony-solar
sudo ipcrm -M 0x4e545037
sudo userdel chrony-solar
```

Then take the `chrony-solar` lines back out of `/etc/apparmor.d/local/usr.sbin.chronyd`.
