# analysis

Scratch work from figuring out what the data could and couldn't do. None of it is used at runtime. It's kept because the charts explain the design better than prose does.

- `sensor-week.png`: the weather station's irradiance against a clear-sky curve for a week. It shows why "find solar noon" was a dead end: the sensor is in shade for most of the day.
- `sensor-vs-panels-week.png`: the same week, with whole-array production and a second meter overlaid.
- `panels-and-lux-week.png`: all 17 panels, each scaled to its own maximum, plus two lux sensors that saturate at 900 lux. The staggered shade windows are what the model times.
- `fetch_panels.py SCRATCH_DIR PROMETHEUS_URL`: pulls raw per-panel samples into SQLite.
- `edges.py SCRATCH_DIR LAT LON`: first prototype, timing each shade edge by where it crosses 0.5. About 85 s RMS.
- `template.py SCRATCH_DIR LAT LON`: second prototype, fitting whole profiles. About 33 s RMS; this became `solar_chrony/model.py`.
- `plot3.py SCRATCH_DIR LAT LON`: draws the panel chart as SVG.

Expect hard-coded dates.
