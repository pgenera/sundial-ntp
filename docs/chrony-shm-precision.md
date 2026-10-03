# chronyd ignores the SHM precision field

Notes from working out how to make the sundial tell its clients how wrong it is. They're kept in case this goes to the chrony-dev mailing list as a feature request or patch. Investigated 2026-10-02 against chrony 4.9 (Debian's `chronyd (chrony) version 4.9 (+CMDMON +REFCLOCK +RTC +PRIVDROP +SCFILTER +SIGND +NTS +SECHASH +IPV6 -DEBUG)`) and upstream master.

## The problem

The solar chronyd's only source is an SHM refclock fed by `solar-noon feed`. Its real error is tens of seconds: one evening's fix is good to about ±45 s, and the week's median to 10 to 25 s. But chronyd advertised a root dispersion of about 15 µs and a root delay of 15 µs, so clients were told the sun was accurate to microseconds while it was 18 s off.

NTP has a place for this: root dispersion, the server's estimate of its maximum error. The question was how to get the sun's error into it, ideally changing as fixes come and go.

## What chronyd does with the SHM segment

The `shmTime` segment has an `int precision` field (log2 seconds), the same as ntpd's. Our feed has always written to it. chronyd copies the segment and then ignores the field.

chrony 4.9, `refclock_shm.c`:

- line 53: `int precision;` is declared in `struct shmTime`. That's the only mention of precision in the file.
- line 139, the end of `shm_poll()`: `return RCL_AddSample(instance, &receive_ts, &clock_ts, t.leap, 1);`. Only the two timestamps and the leap flag get through.

`refclock.c`:

- lines 196–197, in `RCL_AddRefclock()`, once at startup:
  ```c
  inst->precision = LCL_GetSysPrecisionAsQuantum();
  inst->precision = MAX(inst->precision, params->precision);
  ```
  `params->precision` is the `precision` option on the `refclock` line.
- line 477, in `RCL_AddSample()`: `dispersion += instance->precision;`. It goes on to become the sample's `root_dispersion` in `accumulate_sample()` (line 457). `RCL_AddSample()` has no way for a driver to pass a dispersion of its own.

History: `git log -S precision -- refclock_shm.c` finds one commit, `75330fd` "Add SHM refclock driver" (2009-05-05). The field has been ignored since the driver was written.

Upstream master on 2026-10-02 is `8df4f12` "doc: update NEWS" (2026-08-27), the same commit as the `4.9` tag, on both gitlab.com/chrony/chrony and the github.com/mlichvar/chrony mirror. `git log 4.9..HEAD -- refclock_shm.c refclock.c` is empty.

## Measured

Each test ran a copy of `/usr/sbin/chronyd` (the copy escapes Debian's AppArmor profile) as an unprivileged user with `-x`, fed through SHM with an offset of 18 s, queried over NTP on `::1`.

| configuration | stratum | root delay | root dispersion | root distance |
|---|---|---|---|---|
| `refclock SHM n refid SUN` | 1 | 15 µs | 15 µs | ≈ 0 |
| same, SHM precision field = 5 (32 s) | 1 | 15 µs | 15 µs | ≈ 0 |
| same, SHM precision field = −20 | 1 | 15 µs | 15 µs | ≈ 0 |
| `... precision 30` | 10 (local) | — | — | — |
| `... precision 30`, `maxdistance 120` | 10 (local) | — | — | — |
| `... precision 30`, `maxdistance 120`, `local stratum 10 distance 300` | 1 | 15 µs | 30 s | 30 s |
| `... delay 60`, same two directives | 1 | 60 s | 15 µs | 30 s |

What the rows show:

- **The SHM field changes nothing.** Root dispersion is the same at 2^5 and 2^−20.
- **`precision` on the refclock line works**, and puts the error in root dispersion where it belongs. `delay` reaches the same root distance, but by claiming a 60 s path delay that doesn't exist.
- **An honest refclock gets rejected by its own chronyd.** Without `maxdistance`, `selectdata` marks the source `d`: its distance is over the 3 s default.
- **Then `local` takes over.** With `maxdistance` raised, SUN is selected (`*`), but chronyd still serves its local reference at stratum 10. `local`'s `distance` option, 1 s by default, activates the local reference whenever the real source's root distance is above it.
- **chronyd keeps the segment across restarts.** A feeder attached before a restart went on writing to the same segment afterwards, and SUN was selected again without reattaching.
- **Stopped feeds coast for years.** With `maxdistance 1000` and `local ... distance 1000`, when the feed stops posting, chronyd keeps serving SUN at stratum 1. Root dispersion grew from 30.0000 to 30.0001 s in 90 s, so it would take decades to reach 1000 s. With the default `distance 1`, the fallback took about a week.

## Other ways in, ruled out

- **Another refclock driver.** `SOCK` (gpsd's other interface) sends `struct sock_sample`: timestamp, offset, pulse flag, leap, padding and magic, but no error. `PHC`, `PPS` and `RTC` read hardware. `SENSOR` (`cff6f1c`, 2026-07-13) reads OpenBSD timedelta sensors. Every driver ends in `RCL_AddSample()` or `RCL_AddPulse()`, neither of which takes a dispersion.
- **Changing it at runtime.** `chronyc reload sources` re-reads only `sourcedir` files, which may contain only `server`, `pool` and `peer`. The per-source chronyc commands (`maxdelay`, `minstratum`, `add`, `delete` and so on) apply only to NTP sources. `chronyc offset` (via `RCL_ModifyOffset()`) can change a refclock's offset at runtime, but not its precision.
- **A local NTP server instead of a refclock.** chronyd passes an upstream server's root dispersion through as it changes, so the feed could answer NTP on `::1` and chronyd-solar could poll it with `server ::1`. But chronyd-solar would then be stratum 2, and its reference ID would be a hash of `::1`, not `SUN`.
- **Running a patched chronyd.** That would lose Debian's security updates on a server that answers the whole IPv6 internet. Debian's AppArmor profile is tied to `/usr/sbin/chronyd`, so a custom binary elsewhere would run unconfined.

## What other implementations do

- **NTPsec honours the field.** Its `ntpd/refclock_shm.c` copies it into the peer: `shm_stat->precision = shmcopy.precision;` (line 437) and `peer->precision = (int8_t)shm_stat.precision;` (line 559). At startup it writes `PRECISION` (−30) into the segment (lines 179–180).
- **gpsd writes a meaningful value**, so honouring it unconditionally would change behaviour for everyone using gpsd. From `gpsd/timehint.c`, `ntpshm_put()`'s comment (lines 228–240): "Any NMEA will be about -1 or -2. Garmin GPS-18/USB can be -6 or -7. PPS over USB, then precision = -10, 1 milli sec. PPS over serial, precision = -20, 1 micro sec, maybe better". Segments are initialised with −20 (line 198). An NMEA-only SHM refclock would suddenly gain 0.25–0.5 s of dispersion and could lose source selection to something else.

So any change in chrony would need to be opt-in.

## What we do instead

The refclock line lives in `/var/lib/solar-noon/refclock.conf`, which chronyd-solar `include`s. The feed rewrites it, atomically and only when the content changes, with `precision <bound>` or with `noselect` when there's no current fix. `chronyd-solar-refclock.path` restarts chronyd-solar on each change. That's at most once a day between fixes, and each restart costs about 16 s (`poll 4`) before SUN is selected again. chrony.conf has `maxdistance 1000` and `local stratum 10 distance 1000`. The bound itself is described in [how-it-works.md](how-it-works.md#telling-clients-how-wrong-it-is).

## Possible patch (sketch, not compiled or tested)

Add a driver option, so the SHM driver uses the segment's precision only when asked:

```
refclock SHM 7:perm=0660:useprecision refid SUN poll 4
```

`refclock.c`/`refclock.h`: give drivers a way to report a per-sample dispersion. The smallest change is a sibling of `RCL_AddSample()`, so the other drivers don't change:

```c
int
RCL_AddSampleWithPrecision(RCL_Instance instance, struct timespec *sample_time,
                           struct timespec *ref_time, int leap, double precision, int quality);
```

`RCL_AddSample()` would call it with a precision of 0. The body is the current `RCL_AddSample()` with `dispersion += instance->precision;` changed to `dispersion += MAX(instance->precision, precision);`, so a configured `precision` still acts as a floor.

`refclock_shm.c`: `RCL_SetDriverData()` currently stores the bare `shmTime` pointer, so it would need a small struct holding the pointer and a `use_precision` flag. Then:

```c
  const char *options[] = {"perm", "useprecision", NULL};
  ...
  data->use_precision = RCL_GetDriverOption(instance, "useprecision") ? 1 : 0;
```

and in `shm_poll()`, a range check (NTP precision is a signed byte; anything outside about −30..+10 is junk) before:

```c
  return RCL_AddSampleWithPrecision(instance, &receive_ts, &clock_ts, t.leap,
                                    data->use_precision ? ldexp(1.0, t.precision) : 0.0, 1);
```

Plus `doc/chrony.conf.adoc` for the option and a test in `test/simulation` or `test/system`.

Caveats to raise up front:

- **Power-of-two steps.** The field only allows 16, 32, 64 s and so on. That's fine for a sundial and for gpsd's uses, but it isn't a general-purpose error channel.
- **The local-reference interaction.** Large dispersions need `maxdistance` and `local ... distance` raised, and after the samples stop, chronyd coasts with the last dispersion growing about 1 ppm. A driver that sets precision dynamically probably wants that documented.
- **Clients drop anything over 16 s anyway.** chrony's client discards replies with root delay/2 + root dispersion ≥ `NTP_MAX_DISPERSION` (16 s, RFC 5905's MAXDISP), `test7` in `ntp_core.c:2061`, before `maxdistance` applies. Found after deploying: at 105 s, even `chronyd -Q 'server …' 'maxdistance 1000'` says "No suitable source". So an honest bound above 16 s is only visible to hand-rolled queries, and anything this patch passes through above 16 s is outside what NTP can express. That's a fair objection to raise before the maintainer does.
- **Likely objections.** The maintainer may prefer a configurable dispersion on `SOCK` instead (gpsd's preferred interface), or may regard "a refclock whose error changes" as something for an NTP source rather than a refclock.

## Draft for chrony-dev

> Subject: SHM refclock: optionally use the segment's precision field
>
> Hi,
>
> The SHM driver reads the shmTime segment but never uses its precision field; refclock_shm.c passes only the timestamps and leap to RCL_AddSample(), and the sample dispersion comes from the configured precision option alone. As far as I can tell that's been the case since the driver was added in 2009. NTPsec's SHM driver does copy the field into the peer's precision.
>
> My use case is an odd one, a refclock whose error is tens of seconds and changes from day to day (it's a sundial: https://github.com/pgenera/sundial-ntp). Today the only way to update the advertised dispersion is to rewrite the refclock line and restart chronyd.
>
> Since gpsd fills the field for every unit (around -1 for NMEA, -20 for serial PPS), using it unconditionally would change existing setups, so I'd suggest an opt-in driver option, e.g. `refclock SHM 0:useprecision`, with the configured precision still acting as a floor. I can send a patch if this is something you'd take. Would you prefer it done in the SHM driver, or some other way (SOCK, say)?
>
> Thanks,
> Phil

Before sending, decide whether the patch should be written and tested first.
