"""Read-only queries to the solar chronyd through chronyc."""

import os
import subprocess


class Chronyc:
    # The command port, not the Unix socket: chronyd answers read-only
    # queries there for any local user, while the socket needs root or _chrony.
    def __init__(self, host: str = "::1", port: int = 11323, chronyc: str = "chronyc"):
        self.host = host
        self.port = port
        self.chronyc = chronyc

    def run(self, *commands: str) -> str:
        proc = subprocess.run([self.chronyc, "-h", self.host, "-p", str(self.port), "-m", *commands],
                              env=dict(os.environ, TZ="UTC"), capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"chronyc failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def status(self) -> str:
        return self.run("tracking", "sources")
