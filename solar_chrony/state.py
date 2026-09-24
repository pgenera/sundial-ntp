"""Log of daily estimates; the latest accepted one is what the feed serves."""

import sqlite3
from datetime import date

_SCHEMA = """
CREATE TABLE IF NOT EXISTS estimates (
    day          TEXT NOT NULL,
    run_at       REAL NOT NULL,
    offset       REAL,
    sigma        REAL,
    used         TEXT,
    panels_shift REAL,
    panels_cost  REAL,
    sensor_shift REAL,
    sensor_cost  REAL,
    applied      INTEGER NOT NULL,   -- 1: accepted and published to the feed
    notes        TEXT
);
"""


class Log:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)

    def record(self, est, run_at: float, applied: bool) -> None:
        p, s = est.panels, est.sensor
        with self.db:
            self.db.execute(
                "INSERT INTO estimates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (est.day.isoformat(), run_at, est.offset, est.sigma, ",".join(est.used),
                 p.shift if p else None, p.cost if p else None,
                 s.shift if s else None, s.cost if s else None,
                 int(applied), "; ".join(est.notes)))

    def done_on(self, day: date) -> bool:
        """True once today has a final result, applied or rejected."""
        return self.db.execute("SELECT 1 FROM estimates WHERE day = ? AND (applied = 1 OR offset IS NULL)",
                               (day.isoformat(),)).fetchone() is not None

    def latest_published(self) -> tuple[str, float, float] | None:
        """(day, offset, run_at) of the most recent accepted estimate."""
        return self.db.execute(
            "SELECT day, offset, run_at FROM estimates WHERE applied = 1 AND offset IS NOT NULL "
            "ORDER BY run_at DESC LIMIT 1").fetchone()
