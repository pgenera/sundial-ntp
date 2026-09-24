import json, sqlite3, sys, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
SP, PROM = sys.argv[1], sys.argv[2].rstrip('/')  # scratch dir, Prometheus URL
tz = ZoneInfo('America/New_York')
db = sqlite3.connect(f'{SP}/panels.db')
db.execute('CREATE TABLE IF NOT EXISTS p (dev TEXT, ts REAL, w REAL, PRIMARY KEY (dev, ts))')
db.execute('CREATE TABLE IF NOT EXISTS e (dev TEXT, ts REAL, wh REAL, PRIMARY KEY (dev, ts))')
day = date(2026, 8, 20)
while day <= date(2026, 9, 24):
    end = datetime.combine(day + timedelta(1), datetime.min.time(), tz).timestamp()
    for table, metric in (('p', 'sunpower_pvs_inverter_ac_power_watts'), ('e', 'sunpower_pvs_inverter_energy_total_watt_hours')):
        q = urllib.parse.urlencode({'query': f'{metric}[86400s]', 'time': end})
        d = json.load(urllib.request.urlopen(PROM + '/api/v1/query?' + q, timeout=120))
        rows = [(s['metric']['device_id'], float(t), float(v)) for s in d['data']['result'] for t, v in s['values']]
        db.executemany(f'INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)', rows)
    db.commit()
    day += timedelta(1)
print(db.execute('select count(*), count(distinct dev), min(ts), max(ts) from p').fetchone())
print(db.execute('select count(*) from e').fetchone())
