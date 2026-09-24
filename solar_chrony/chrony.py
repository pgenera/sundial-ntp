"""Read-only queries to the solar chronyd through chronyc."""

import os
import subprocess


class Chronyc:
    def __init__(self, socket: str, chronyc: str = "chronyc"):
        self.socket = socket
        self.chronyc = chronyc

    def run(self, *commands: str) -> str:
        proc = subprocess.run([self.chronyc, "-h", self.socket, "-m", *commands],
                              env=dict(os.environ, TZ="UTC"), capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"chronyc failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def status(self) -> str:
        return self.run("tracking", "sources")
