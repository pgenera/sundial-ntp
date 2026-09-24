"""Drive the solar chronyd's manual mode through chronyc."""

import math
import os
import subprocess
import time
from datetime import datetime, timezone


class Chronyc:
    def __init__(self, socket: str, chronyc: str = "chronyc"):
        self.socket = socket
        self.chronyc = chronyc

    def run(self, *commands: str) -> str:
        # settime is parsed in chronyc's local zone; pin it to UTC.
        env = dict(os.environ, TZ="UTC")
        proc = subprocess.run([self.chronyc, "-h", self.socket, "-m", *commands],
                              env=env, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"chronyc failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def set_solar_offset(self, offset: float, clock=time.time, sleep=time.sleep) -> str:
        """Tell chronyd that true time is system time minus `offset` seconds.

        settime only accepts whole seconds, so wait until the solar time is
        exactly on a second boundary and send that.
        """
        solar_now = clock() - offset
        target = math.floor(solar_now) + 1
        if target - solar_now < 0.2:
            target += 1
        sleep(max(0.0, target + offset - clock()))
        stamp = datetime.fromtimestamp(target, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return self.run("manual on", f"settime {stamp}")

    def status(self) -> str:
        return self.run("tracking", "manual list")
