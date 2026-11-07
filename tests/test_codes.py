"""torporctl must run before `cryptography` is installed, to say it is missing; its copied constants must match."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import Env
from torpor import codes, protocol

REPO = Path(__file__).resolve().parent.parent
WITHOUT_CRYPTOGRAPHY = """
import sys
sys.modules["cryptography"] = None
sys.path.insert(0, sys.argv[1])
from torpor.cli import main
sys.exit(main(sys.argv[2:]))
"""


def test_the_copied_constants_match_the_protocol() -> None:
    assert (codes.PROTO, codes.WS_PATH, codes.PAIRING_TIMEOUT_S) == (protocol.PROTO, protocol.WS_PATH, 900)
    assert (codes.MAX_HOSTNAME, codes.MAX_OS, codes.KEY_BYTES) == (
        protocol.MAX_HOSTNAME,
        protocol.MAX_OS,
        protocol.KEY_BYTES,
    )
    for close in protocol.Close:
        assert getattr(codes, close.name) == close.value
        assert close.value in codes.MEANINGS
    assert protocol.PAIRING_TIMEOUT_S == codes.PAIRING_TIMEOUT_S


def ctl(env: Env, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-c", WITHOUT_CRYPTOGRAPHY, str(REPO), *argv],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env.env},
        timeout=60,
    )


def test_torporctl_runs_without_cryptography(env: Env) -> None:
    env.tools.set(
        probe={
            "python": "3.14.0",
            "pygobject": "3.58.1",
            "glib": "2.86.0",
            "soup": "3.8.1",
            "cryptography_error": "ModuleNotFoundError",
        }
    )
    version = ctl(env, "--version")
    assert version.returncode == 0 and version.stdout.strip().endswith("(proto 1)"), version.stderr
    assert ctl(env, "config", "list").returncode == 0
    assert ctl(env, "status").returncode == 3
    doctor = ctl(env, "doctor", "--json")
    assert doctor.returncode == 4, doctor.stderr
    report = json.loads(doctor.stdout)
    assert [c["level"] for c in report["checks"] if c["id"] == "cryptography"] == ["fail"]
    assert report["distro"]["command"] == "sudo pacman -S --needed python-cryptography"
    install = ctl(env, "install")
    assert install.returncode == 4 and "Traceback" not in install.stderr
    assert "sudo pacman -S --needed python-cryptography" in install.stdout
