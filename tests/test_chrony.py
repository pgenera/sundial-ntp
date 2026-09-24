"""Integration test against a throwaway, unprivileged `chronyd -x`."""

import os
import pwd
import shutil
import socket
import struct
import subprocess
import tempfile
import time

import pytest

from solar_chrony.chrony import Chronyc

CHRONYD = shutil.which("chronyd") or "/usr/sbin/chronyd"
pytestmark = pytest.mark.skipif(not (os.path.exists(CHRONYD) and shutil.which("chronyc")),
                                reason="chrony not installed")


def ntp_query(port):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.settimeout(2)
    t0 = time.time()
    s.sendto(b"\x23" + b"\0" * 47, ("::1", port))
    data, _ = s.recvfrom(48)
    t1 = time.time()
    sec, frac = struct.unpack("!II", data[40:48])
    return sec - 2208988800 + frac / 2**32 - (t0 + t1) / 2, data[1], data[0] >> 6


@pytest.fixture
def solar_chronyd():
    # Unix socket paths are length-limited; keep the directory short.
    d = tempfile.mkdtemp(prefix="cs", dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    os.chmod(d, 0o700)
    # A copy of the binary escapes Debian's AppArmor profile for /usr/sbin/chronyd.
    binary = os.path.join(d, "chronyd")
    shutil.copy(CHRONYD, binary)
    port = 11000 + os.getpid() % 1000
    conf = os.path.join(d, "c.conf")
    with open(conf, "w") as f:
        f.write(f"port {port}\nbindaddress ::1\nallow ::1\ncmdport 0\n"
                f"bindcmdaddress {d}/chronyd.sock\npidfile {d}/chronyd.pid\n"
                "manual\nlocal stratum 1\n")
    user = pwd.getpwuid(os.getuid()).pw_name
    proc = subprocess.Popen([binary, "-d", "-6", "-x", "-U", "-u", user, "-f", conf, "-L", "2"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(f"{d}/chronyd.sock"):
            break
        time.sleep(0.1)
    try:
        yield Chronyc(f"{d}/chronyd.sock"), port
    finally:
        proc.terminate()
        proc.wait(5)
        shutil.rmtree(d, ignore_errors=True)


def test_settime_shifts_served_time_only(solar_chronyd):
    chronyc, port = solar_chronyd
    before = time.time()
    # Observed noon 90 s late ⇒ the solar clock runs 90 s behind.
    out = chronyc.set_solar_offset(90.0)
    assert "Clock was" in out
    offset, stratum, leap = ntp_query(port)
    assert offset == pytest.approx(-90.0, abs=0.1)
    assert stratum == 1
    assert leap == 0
    # The system clock itself is untouched (still tracks real time).
    assert abs(time.time() - before) < 5
    assert "n_samples = 1" in chronyc.status()
