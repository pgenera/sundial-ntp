import sqlite3, sys
from datetime import date, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, '.')
from solar_chrony import solar
from solar_chrony.main import day_bounds
SP = sys.argv[1]; LAT, LON = float(sys.argv[2]), float(sys.argv[3])
tz = ZoneInfo('America/New_York'); db = sqlite3.connect(f'{SP}/real.db')
panels = [r[0] for r in db.execute("select distinct entity from samples where entity like 'panel.%' order by entity")]
days = [date(2026, 9, 17) + timedelta(d) for d in range(7)]
W, H, PAD = 560, 220, 40
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{2*W}" height="{4*H+40}" font-family="sans-serif" font-size="11">',
       '<rect width="100%" height="100%" fill="#fcfcfb"/>',
       '<text x="10" y="22" font-size="14" fill="#0b0b0b">Each normalised: 17 panels (thin orange), back_flood lux S (blue), driveway lux N (violet), gray = clear-sky cos z; x = min from predicted noon</text>']
def line(rows, X, Y, col, w, op=1):
    mx = max((v for _, v in rows), default=1) or 1
    return f'<polyline fill="none" stroke="{col}" stroke-width="{w}" stroke-opacity="{op}" points="' + ' '.join(f'{X(t):.1f},{Y(max(0,v)/mx):.1f}' for t, v in rows) + '"/>'
for n, d in enumerate(days):
    ox, oy = (n % 2) * W, 40 + (n // 2) * H
    s, e = day_bounds(d, tz); noon = solar.solar_noon_utc(d, LON)
    X = lambda t: ox + PAD + (t - noon + 7*3600) / (14*3600) * (W - PAD - 10)
    Y = lambda v: oy + H - 25 - v * (H - 45)
    svg.append(f'<text x="{ox+PAD}" y="{oy+14}" fill="#0b0b0b">{d}</text>')
    for gv in (0, .5, 1):
        svg.append(f'<line x1="{ox+PAD}" x2="{ox+W-10}" y1="{Y(gv):.1f}" y2="{Y(gv):.1f}" stroke="#e4e3df"/>')
    for m in (-360, -240, -120, 0, 120, 240, 360):
        svg.append(f'<text x="{X(noon+m*60):.1f}" y="{oy+H-10}" text-anchor="middle" fill="#52514e">{m:+d}</text>')
    svg.append(f'<line x1="{X(noon):.1f}" x2="{X(noon):.1f}" y1="{oy+20}" y2="{Y(0):.1f}" stroke="#52514e" stroke-dasharray="3 3"/>')
    model = [(noon+m*60, max(0, solar.cos_zenith(noon+m*60, LAT, LON))) for m in range(-420, 421, 5)]
    svg.append(line(model, X, Y, '#a3a29c', 2))
    get = lambda ent: [(t, v) for t, v in db.execute('select ts,value from samples where entity=? and ts>=? and ts<? order by ts', (ent, s, e)) if v is not None and abs(t-noon) < 7*3600]
    for p in panels:
        svg.append(line(get(p), X, Y, '#eb6834', 1, .35))
    svg.append(line(get('sensor.back_flood_illuminance'), X, Y, '#2a78d6', 1.5))
    svg.append(line(get('sensor.driveway_illuminance'), X, Y, '#4a3aa7', 1.5))
svg.append('</svg>')
open(f'{SP}/days3.svg', 'w').write('\n'.join(svg))
