"""`solar-noon status` asks chronyc over the command port, not the Unix socket."""

import os
import sys

from solar_chrony.chrony import Chronyc


def fake_chronyc(tmp_path):
    path = tmp_path / "chronyc"
    path.write_text(f"#!{sys.executable}\nimport sys\nprint(' '.join(sys.argv[1:]))\n")
    os.chmod(path, 0o755)
    return str(path)


def test_status_uses_the_command_port(tmp_path):
    out = Chronyc(chronyc=fake_chronyc(tmp_path)).status()
    assert out.split() == ["-h", "::1", "-p", "11323", "-m", "tracking", "sources"]


def test_host_and_port_come_from_config(tmp_path):
    out = Chronyc("127.0.0.1", 12345, fake_chronyc(tmp_path)).run("tracking")
    assert out.split() == ["-h", "127.0.0.1", "-p", "12345", "-m", "tracking"]
