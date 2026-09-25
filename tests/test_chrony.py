"""Integration test: `solar-noon feed` -> SHM refclock -> an unprivileged `chronyd -x`."""

import argparse
import ctypes
import os
import pwd
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from datetime import date, timedelta

import pytest

from solar_chrony.main import cmd_feed
from solar_chrony.model import Estimate
from solar_chrony.shm import NTP_SHM_BASE
from solar_chrony.state import Log

CHRONYD = shutil.which("chronyd") or "/usr/sbin/chronyd"
pytestmark = pytest.mark.skipif(not os.path.exists(CHRONYD), reason="chrony not installed")


def ntp_query(port):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.settimeout(2)
    t0 = time.time()
    s.sendto(b"\x23" + b"\0" * 47, ("::1", port))
    data, _ = s.recvfrom(48)
    t1 = time.time()
    sec, frac = struct.unpack("!II", data[40:48])
    offset = sec - 2208988800 + frac / 2**32 - (t0 + t1) / 2
    return offset, data[1], data[0] >> 6, data[12:16]


def remove_segment(unit):
    libc = ctypes.CDLL(None)
    shmid = libc.shmget(NTP_SHM_BASE + unit, 0, 0)
    if shmid >= 0:
        libc.shmctl(shmid, 0, None)          # IPC_RMID


@pytest.fixture
def solar_chronyd():
    # Unix socket paths are length-limited; keep the directory short.
    d = tempfile.mkdtemp(prefix="cs", dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    os.chmod(d, 0o700)
    # A copy of the binary escapes Debian's AppArmor profile for /usr/sbin/chronyd.
    binary = os.path.join(d, "chronyd")
    shutil.copy(CHRONYD, binary)
    port = 11000 + os.getpid() % 1000
    unit = 50 + os.getpid() % 40             # away from real deployments' units
    conf = os.path.join(d, "c.conf")
    with open(conf, "w") as f:
        f.write(f"port {port}\nbindaddress ::1\nallow ::1\ncmdport 0\n"
                f"bindcmdaddress {d}/chronyd.sock\npidfile {d}/chronyd.pid\n"
                f"refclock SHM {unit}:perm=0600 refid SUN poll 0\nlocal stratum 10\n")
    user = pwd.getpwuid(os.getuid()).pw_name
    proc = subprocess.Popen([binary, "-d", "-6", "-x", "-U", "-u", user, "-f", conf, "-L", "2"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(f"{d}/chronyd.sock"):
            break
        time.sleep(0.1)
    try:
        yield d, port, unit
    finally:
        proc.terminate()
        proc.wait(5)
        remove_segment(unit)
        shutil.rmtree(d, ignore_errors=True)


def feed_until(cfg, port, want_stratum, seconds=20):
    args = argparse.Namespace(once=True)
    for _ in range(seconds):
        cmd_feed(cfg, args)
        time.sleep(1)
        reply = ntp_query(port)
        if reply[1] == want_stratum:
            return reply
    return reply


def test_feed_serves_the_sun(solar_chronyd):
    d, port, unit = solar_chronyd
    cfg = {"feed": {"shm_unit": unit, "max_age_days": 14}, "storage": {"log": f"{d}/log.db"}}

    # No published fix yet: chronyd stays on its own clock at stratum 10.
    offset, stratum, leap, refid = feed_until(cfg, port, want_stratum=1, seconds=5)
    assert (stratum, refid) == (10, b"\x7f\x7f\x01\x01")
    assert abs(offset) < 0.01

    # The shadows say the sun runs 90 s behind the system clock.
    before = time.time()
    Log(f"{d}/log.db").record(Estimate(date.today(), offset=90.0, sigma=30.0, used=["panels"]),
                              run_at=time.time(), applied=True)
    offset, stratum, leap, refid = feed_until(cfg, port, want_stratum=1)
    assert (stratum, leap, refid) == (1, 0, b"SUN\x00")
    assert offset == pytest.approx(-90.0, abs=0.05)
    # The system clock itself is untouched.
    assert abs(time.time() - before) < 60


def test_stale_fix_is_not_fed(solar_chronyd):
    d, port, unit = solar_chronyd
    cfg = {"feed": {"shm_unit": unit, "max_age_days": 14}, "storage": {"log": f"{d}/log.db"}}
    Log(f"{d}/log.db").record(Estimate(date(2020, 1, 1), offset=90.0, sigma=30.0, used=["panels"]),
                              run_at=time.time() - 30 * 86400, applied=True)
    offset, stratum, leap, refid = feed_until(cfg, port, want_stratum=1, seconds=5)
    assert (stratum, refid) == (10, b"\x7f\x7f\x01\x01")


def test_feed_serves_the_median_of_recent_fixes(solar_chronyd):
    d, port, unit = solar_chronyd
    cfg = {"feed": {"shm_unit": unit, "max_age_days": 14, "smoothing_days": 7, "smoothing": "median"},
           "storage": {"log": f"{d}/log.db"}}
    state = Log(f"{d}/log.db")
    today = date.today()
    for back, off in ((2, 30.0), (1, 60.0), (0, 400.0)):   # one bad day
        state.record(Estimate(today - timedelta(days=back), offset=off, sigma=30.0, used=["panels"]),
                     run_at=time.time() - back * 86400, applied=True)
    offset, stratum, leap, refid = feed_until(cfg, port, want_stratum=1)
    assert (stratum, refid) == (1, b"SUN\x00")
    assert offset == pytest.approx(-60.0, abs=0.05)
