"""torporctl in a temporary HOME with fake systemctl, systemd-run, loginctl, journalctl and python (A1, A11-A13)."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import Env
from fakes.bus import FakeBus
from torpor import __version__, cli, protocol
from torpor import config as conf
from torpor.keyfile import read_key

CODE = bytes(range(20))


def torporctl(*argv: str) -> int:
    return cli.main(list(argv))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# pair (A1)


@pytest.fixture
def pairing(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(cli, "RUNTIME_FACTORY", cli_env.runtime)
    cli_env.ha.pending[protocol.pair_id(CODE)] = CODE
    return cli_env


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_pair_refuses_a_code_on_the_command_line(pairing: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", protocol.format_code(CODE)) == 2
    assert "never taken from the command line" in capsys.readouterr().err
    assert pairing.ha.connections == []


def test_pair_needs_a_terminal_or_code_stdin(pairing: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    stdin(monkeypatch, protocol.format_code(CODE))
    assert torporctl("pair", "--url", "http://192.0.2.10:8123") == 2


def test_pair_reads_the_code_hidden_at_a_terminal(pairing: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: asked.append(prompt) or protocol.format_code(CODE))
    assert torporctl("pair", "--url", "http://192.0.2.10:8123") == 0
    assert asked == ["Pairing code from Home Assistant: "]


def test_a_malformed_code_fails_before_any_connection(pairing: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    stdin(monkeypatch, "trp1-not-a-code\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", "--code-stdin") == 2
    assert pairing.ha.connections == []


def test_pair_writes_the_key_only_on_paired_and_prints_the_fingerprint(
    pairing: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen_before_paired: list[bool] = []
    original = pairing.ha.pairing_message

    def watch(conn: Any, message: dict[str, Any]) -> None:
        if message["type"] == "capabilities":
            seen_before_paired.append(pairing.paths.key_file.exists())
        original(conn, message)

    pairing.ha.pairing_message = watch  # type: ignore[method-assign]
    stdin(monkeypatch, protocol.format_code(CODE).upper() + "\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123/", "--code-stdin") == 0
    assert seen_before_paired == [False]
    conn = pairing.ha.connections[0]
    assert conn.handshake is not None
    assert read_key(pairing.paths.key_file) == conn.handshake.pairing_key == pairing.ha.entries[pairing.agent_id]
    assert mode(pairing.paths.key_file) == 0o600 and mode(pairing.paths.config_dir) == 0o700
    assert conf.load(pairing.paths.config_file)[0].ha_url == "http://192.0.2.10:8123"
    out = capsys.readouterr().out
    assert f"fingerprint {conn.handshake.fingerprint}" in out
    assert "torporctl install" in out
    assert pairing.ha.paired_acks == 1
    capabilities = conn.received_of("capabilities")[0]
    assert set(capabilities["modules"]) == {"core", "power", "keep_awake", "wake", "brightness"}


@pytest.mark.parametrize("plasma", [True, False])
def test_pair_leaves_the_running_agents_files_and_ddcutil_alone(
    pairing: Env, monkeypatch: pytest.MonkeyPatch, plasma: bool
) -> None:
    pairing.world.login("3", desktop="KDE" if plasma else "GNOME")
    if plasma:
        pairing.world.plasma()
        pairing.world.displays["display0"] = {"Label": "Example 27Q", "Brightness": 50, "MaxBrightness": 100}
    pairing.tree.connector("card0-DP-2")
    monitor = {"bus": 4, "mfg": "EXA", "model": "Example 24", "serial": "SYN0001", "brightness": 40}
    pairing.tools.set_ddc(monitors=[monitor])
    pairing.paths.runtime_dir.mkdir(parents=True)
    running = json.dumps({"pid": 777, "marker": "written by the running agent"})
    pairing.paths.status_file.write_text(running)
    stdin(monkeypatch, protocol.format_code(CODE) + "\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", "--code-stdin") == 0
    assert pairing.paths.status_file.read_text() == running
    assert not pairing.paths.known_file.exists()
    assert pairing.tools.ddc().get("calls", []) == [["--version"]], "ddcutil reached a monitor"
    displays = pairing.ha.connections[0].received_of("capabilities")[0]["modules"]["brightness"]["displays"]
    assert [d["label"] for d in displays] == (["Example 27Q"] if plasma else [])


@pytest.mark.parametrize(("action", "code"), [("cancel", 5), ("timeout", 6)])
def test_nothing_is_written_when_home_assistant_closes_the_pairing(
    pairing: Env, monkeypatch: pytest.MonkeyPatch, action: str, code: int
) -> None:
    pairing.ha.pair_action = action
    stdin(monkeypatch, protocol.format_code(CODE) + "\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", "--code-stdin") == code
    assert not pairing.paths.key_file.exists()
    assert not pairing.paths.config_file.exists()


def test_a_wrong_code_never_sends_a_proof(
    pairing: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, protocol.format_code(bytes(20)) + "\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", "--code-stdin") == 5
    assert pairing.ha.connections[0].texts == 1
    assert pairing.ha.proofs == 0 and pairing.ha.bans == 0
    assert "wrong, expired or already used" in capsys.readouterr().err
    assert not pairing.paths.key_file.exists()


def test_an_unreachable_home_assistant_exits_6(pairing: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pairing.ha.reachable = False
    stdin(monkeypatch, protocol.format_code(CODE) + "\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", "--code-stdin") == 6


def test_pairing_restarts_an_installed_agent_and_waits_for_it(pairing: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pairing.paths.unit_file.parent.mkdir(parents=True)
    pairing.paths.unit_file.write_text("[Service]\n")
    status = {"started": 9_999_999_999, "connection": {"state": "connected"}, "paired": True}
    pairing.paths.runtime_dir.mkdir(parents=True)
    pairing.tools.set(on_restart=[str(pairing.paths.status_file), json.dumps(status)])
    stdin(monkeypatch, protocol.format_code(CODE) + "\n")
    assert torporctl("pair", "--url", "http://192.0.2.10:8123", "--code-stdin") == 0
    assert ["systemctl", "--user", "restart", "torpord.service"] in pairing.tools.calls("systemctl")


def test_pair_needs_a_url(pairing: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    stdin(monkeypatch, protocol.format_code(CODE) + "\n")
    assert torporctl("pair", "--code-stdin") == 2


# install, uninstall, update (A11)


def installed(env: Env, *args: str) -> int:
    return torporctl("install", *args)


def test_install_writes_its_files_with_their_modes(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    paths = cli_env.paths
    assert installed(cli_env) == 0
    python = cli_env.env["TORPOR_PYTHON"]
    assert mode(paths.zipapp) == 0o644 and mode(paths.launcher) == 0o755
    assert mode(paths.unit_file) == 0o644 and mode(paths.config_file) == 0o600
    assert mode(paths.config_dir) == 0o700 and mode(paths.state_dir) == 0o700
    unit = paths.unit_file.read_text()
    assert f"ExecStart={python} -I -B %h/.local/lib/torpor/torpor.pyz agent\n" in unit
    assert os.path.isabs(python)
    assert "Environment=GSETTINGS_BACKEND=memory" in unit and "WantedBy=default.target" in unit
    assert paths.launcher.read_text() == f'#!/bin/sh\nexec {python} -I -B {paths.zipapp} ctl "$@"\n'
    assert paths.config_file.read_text() == conf.DEFAULT_TEXT
    calls = [call[1:] for call in cli_env.tools.calls("systemctl")]
    assert ["--user", "daemon-reload"] in calls
    assert calls.index(["--user", "enable", "torpord.service"]) < calls.index(["--user", "restart", "torpord.service"])
    assert "torporctl pair" in capsys.readouterr().out


def test_install_says_when_its_bin_folder_is_not_on_path(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = cli_env.paths.launcher
    assert installed(cli_env) == 0
    out = capsys.readouterr().out
    assert f"{launcher.parent} isn't on your PATH" in out
    assert f"then run {launcher} pair --url" in out
    cli_env.paths.key_file.unlink(missing_ok=True)
    monkeypatch.setenv("PATH", f"{launcher.parent}{os.pathsep}{os.environ['PATH']}")
    assert installed(cli_env) == 0
    out = capsys.readouterr().out
    assert "isn't on your PATH" not in out and "then run torporctl pair --url" in out


def test_the_built_zipapp_runs(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(cli_env.paths.zipapp), "ctl", "--version"], capture_output=True, text=True
    )
    assert done.stdout.strip() == f"torporctl {__version__} (proto 1)"
    agent = subprocess.run(
        [sys.executable, "-I", "-B", str(cli_env.paths.zipapp), "agent", "--version"], capture_output=True, text=True
    )
    assert agent.stdout.strip() == f"torpord {__version__} (proto 1)"


def test_hardening_is_written_only_after_the_probe_passes(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    paths = cli_env.paths
    assert mode(paths.hardening_file) == 0o644
    probe = cli_env.tools.calls("systemd-run")[0]
    assert probe[1:7] == ["--user", "--wait", "--pipe", "--quiet", "--collect", "-p"]
    assert "MemoryDenyWriteExecute=yes" in probe
    assert (
        f"ReadWritePaths=-{paths.home}/.config/torpor -{paths.home}/.local/state/torpor {paths.runtime}/torpor" in probe
    )
    assert probe[-6:] == [cli_env.env["TORPOR_PYTHON"], "-I", "-B", str(paths.zipapp), "agent", "--self-test"]
    assert "PrivateDevices" not in paths.hardening_file.read_text()


def test_a_failed_probe_skips_the_hardening_and_says_why(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    cli_env.tools.set(probe_ok=False)
    assert installed(cli_env) == 0
    assert not cli_env.paths.hardening_file.exists()
    assert "the probe under those settings failed" in capsys.readouterr().out


def test_the_drop_in_follows_moved_xdg_folders(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cli_env.home / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(cli_env.tmp / "elsewhere"))
    assert installed(cli_env) == 0
    text = (cli_env.home / "cfg" / "systemd" / "user" / "torpord.service.d" / "50-hardening.conf").read_text()
    assert f"ReadWritePaths=-%h/cfg/torpor -{cli_env.tmp}/elsewhere/torpor %t/torpor" in text


def test_no_hardening_skips_the_probe_and_removes_an_old_drop_in(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    assert installed(cli_env, "--no-hardening") == 0
    assert not cli_env.paths.hardening_file.exists()
    assert len(cli_env.tools.calls("systemd-run")) == 1


def test_no_start_neither_enables_nor_starts(cli_env: Env) -> None:
    assert installed(cli_env, "--no-start") == 0
    verbs = [call[2] for call in cli_env.tools.calls("systemctl")]
    assert "enable" not in verbs and "restart" not in verbs


def test_observe_only_is_set_in_the_config(cli_env: Env) -> None:
    assert installed(cli_env, "--observe-only") == 0
    assert conf.load(cli_env.paths.config_file)[0].observe_only is True


def test_an_existing_config_is_kept(cli_env: Env) -> None:
    cli_env.write_config("[allow]\nlock = no\n")
    assert installed(cli_env) == 0
    assert cli_env.paths.config_file.read_text() == "[allow]\nlock = no\n"


def test_a_failed_required_check_writes_nothing(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    cli_env.tools.set(
        probe={"python": "3.14.0", "pygobject": "3.58.1", "glib": "2.86.0", "gi_error": "Namespace Soup not available"}
    )
    before = files_under(cli_env.home)
    assert installed(cli_env) == 4
    assert files_under(cli_env.home) == before
    captured = capsys.readouterr()
    assert "nothing was written" in captured.err
    assert "sudo pacman -S --needed libsoup3 python-cryptography" in captured.out


def test_an_old_python_is_refused(cli_env: Env) -> None:
    cli_env.tools.set(
        probe={"python": "3.11.2", "pygobject": "3.42.2", "glib": "2.74.0", "soup": "3.2.2", "cryptography": "38.0.4"}
    )
    assert installed(cli_env) == 4


def test_install_reports_linger_and_how_to_turn_it_on(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    cli_env.tools.set(linger=False)
    assert installed(cli_env) == 0
    out = capsys.readouterr().out
    assert "Linger is off" in out and "loginctl enable-linger" in out
    assert not [c for c in cli_env.tools.calls("loginctl") if c[1] == "enable-linger"]


def test_a_failing_systemctl_exits_1(cli_env: Env) -> None:
    cli_env.tools.set(fail=["daemon-reload"])
    assert installed(cli_env) == 1


def test_uninstall_removes_the_files_and_keeps_the_config(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    cli_env.pair()
    assert torporctl("uninstall") == 0
    paths = cli_env.paths
    for path in (paths.unit_file, paths.hardening_file, paths.zipapp, paths.launcher, paths.runtime_dir):
        assert not path.exists(), path
    assert paths.key_file.exists() and paths.config_file.exists()
    assert ["systemctl", "--user", "disable", "--now", "torpord.service"] in cli_env.tools.calls("systemctl")


def test_uninstall_names_the_root_files_it_leaves(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert installed(cli_env) == 0
    capsys.readouterr()
    assert torporctl("uninstall") == 0
    out = capsys.readouterr().out
    assert "sudo rm /etc/polkit-1/rules.d/50-torpor.rules" in out
    assert "sudo rm /etc/modules-load.d/i2c-dev.conf" in out


def test_uninstall_removes_the_enable_link_when_disable_fails(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    wants = cli_env.paths.unit_dir / "default.target.wants"
    wants.mkdir()
    (wants / "torpord.service").symlink_to(cli_env.paths.unit_file)
    cli_env.tools.set(fail=["disable"])
    assert torporctl("uninstall") == 1
    assert not (wants / "torpord.service").is_symlink()
    assert not cli_env.paths.unit_file.exists()


def test_uninstall_purge_removes_the_key_and_state(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    cli_env.pair()
    assert torporctl("uninstall", "--purge") == 0
    assert not cli_env.paths.config_dir.exists() and not cli_env.paths.state_dir.exists()


def test_uninstall_when_not_installed_exits_3(cli_env: Env) -> None:
    assert torporctl("uninstall") == 3


def fake_checkout(root: Path, version: str) -> Path:
    (root / "torpor").mkdir(parents=True)
    (root / "torpor" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    launcher = root / "torporctl"
    launcher.write_text("import os, sys\nopen(os.environ['FAKE_UPDATE_LOG'], 'w').write(' '.join(sys.argv[1:]))\n")
    return root


def test_update_refuses_an_older_version_unless_forced(
    cli_env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert installed(cli_env) == 0
    log = tmp_path / "update.log"
    monkeypatch.setenv("FAKE_UPDATE_LOG", str(log))
    source = fake_checkout(tmp_path / "old", "0.0.1")
    assert torporctl("update", str(source)) == 1
    assert not log.exists()
    assert torporctl("update", str(source), "--force") == 0
    assert log.read_text() == "install"


def test_update_from_a_tarball_keeps_the_no_hardening_choice(
    cli_env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert installed(cli_env, "--no-hardening") == 0
    log = tmp_path / "update.log"
    monkeypatch.setenv("FAKE_UPDATE_LOG", str(log))
    source = fake_checkout(tmp_path / "src" / "torpor-0.2.0", "0.2.0")
    tarball = tmp_path / "torpor-0.2.0.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(source, arcname="torpor-0.2.0")
    assert torporctl("update", str(tarball)) == 0
    assert log.read_text() == "install --no-hardening"


def test_update_probes_again_after_a_failed_probe(
    cli_env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_env.tools.set(probe_ok=False)
    assert installed(cli_env) == 0
    assert not cli_env.paths.hardening_file.exists()
    log = tmp_path / "update.log"
    monkeypatch.setenv("FAKE_UPDATE_LOG", str(log))
    assert torporctl("update", str(fake_checkout(tmp_path / "new", "0.2.0"))) == 0
    assert log.read_text() == "install"


def test_update_needs_an_install_and_a_checkout(cli_env: Env, tmp_path: Path) -> None:
    assert torporctl("update", str(tmp_path)) == 3
    assert installed(cli_env) == 0
    assert torporctl("update", str(tmp_path / "nothing")) == 2


def test_update_from_this_checkout_reinstalls(cli_env: Env) -> None:
    assert installed(cli_env) == 0
    checkout = Path(__file__).resolve().parent.parent
    assert torporctl("update", str(checkout)) == 0
    assert cli_env.paths.zipapp.exists()


# doctor (A12)


@pytest.fixture
def doctor_buses(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(
        cli, "DOCTOR_BUSES", lambda: (FakeBus(cli_env.world, "system"), FakeBus(cli_env.world, "session"))
    )
    return cli_env


def doctor_json(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = torporctl("doctor", "--json")
    return code, json.loads(capsys.readouterr().out)


def check(report: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [c for c in report["checks"] if c["id"] == name]


MISSING_GI = {"python": "3.12.3", "gi_error": "ModuleNotFoundError: No module named 'gi'", "cryptography_error": "x"}
DEBIAN_GIR = "gir1.2-glib-2.0 gir1.2-soup-3.0"


@pytest.mark.parametrize(
    ("distro", "like", "version", "atomic", "command"),
    [
        ("arch", "", "", False, "sudo pacman -S --needed python-gobject libsoup3 python-cryptography"),
        ("cachyos", "arch", "", False, "sudo pacman -S --needed python-gobject libsoup3 python-cryptography"),
        ("debian", "", "13", False, f"sudo apt install python3-gi {DEBIAN_GIR} python3-cryptography"),
        ("ubuntu", "debian", "24.04", False, f"sudo apt install python3-gi {DEBIAN_GIR} python3-cryptography"),
        ("fedora", "", "45", False, "sudo dnf install python3-gobject-base libsoup3 python3-cryptography"),
        ("fedora", "", "45", True, "sudo rpm-ostree install python3-gobject-base libsoup3 python3-cryptography"),
    ],
)
def test_doctor_prints_one_install_line_for_what_is_missing(
    doctor_buses: Env,
    capsys: pytest.CaptureFixture[str],
    distro: str,
    like: str,
    version: str,
    atomic: bool,
    command: str,
) -> None:
    doctor_buses.tools.set(probe=MISSING_GI)
    doctor_buses.tree.os_release(distro, f"{distro} test", like=like, version=version)
    if atomic:
        (doctor_buses.tree.run / "ostree-booted").write_text("")
    code, report = doctor_json(capsys)
    assert code == 4
    assert report["distro"]["command"] == command
    assert check(report, "pygobject")[0]["level"] == "fail"


def test_doctor_calls_a_distribution_with_an_old_python_too_old(
    doctor_buses: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = {"python": "3.10.12", "pygobject": "3.42.1", "glib": "2.72.4", "soup": "3.0.7", "cryptography": "3.4.8"}
    doctor_buses.tools.set(probe=probe)
    doctor_buses.tree.os_release("linuxmint", "Linux Mint 21.3", like="ubuntu debian", version="21.3")
    code, report = doctor_json(capsys)
    assert code == 4
    assert report["distro"]["supported"] is False and "command" not in report["distro"]
    assert any("Python 3.10.12 is too old" in c["message"] for c in check(report, "distro"))


def test_doctor_lists_the_full_package_sets_for_ci(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    doctor_buses.tree.os_release("debian", "Debian GNU/Linux 13 (trixie)", version="13")
    _, report = doctor_json(capsys)
    assert report["distro"]["packages"]["required"] == [
        "python3",
        "python3-gi",
        "gir1.2-glib-2.0",
        "gir1.2-soup-3.0",
        "python3-cryptography",
    ]
    assert report["distro"]["missing"] == []


@pytest.mark.parametrize(
    ("distro", "version", "why"),
    [("debian", "12", "Debian 12"), ("steamos", "3.6", "SteamOS"), ("ubuntu", "22.04", "Ubuntu before 24.04")],
)
def test_doctor_reports_unsupported_systems(
    doctor_buses: Env, capsys: pytest.CaptureFixture[str], distro: str, version: str, why: str
) -> None:
    doctor_buses.tree.os_release(distro, distro, version=version, like="debian" if distro == "ubuntu" else "")
    _, report = doctor_json(capsys)
    assert any(why in c["message"] for c in check(report, "distro"))
    assert report["distro"]["supported"] is False


def test_doctor_reports_a_system_without_systemd(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    (doctor_buses.tree.run / "systemd" / "system").rmdir()
    _, report = doctor_json(capsys)
    assert any("doesn't run systemd" in c["message"] for c in check(report, "distro"))


def test_doctor_passes_on_a_healthy_machine_and_prints_text(
    doctor_buses: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    doctor_buses.world.login("3")
    doctor_buses.world.plasma()
    assert torporctl("install") == 0
    capsys.readouterr()
    doctor_buses.tools.set(active="active", enabled="enabled")
    assert torporctl("doctor") == 0
    out = capsys.readouterr().out
    for line in ("ok    python", "ok    systemd-user", "ok    hardening", "ok    keep-awake", "ok    brightness"):
        assert line in out, line
    assert "warn  paired" in out
    assert "ok    power" in out and "polkit.addRule" in out


def test_doctor_prints_the_polkit_rule_from_allow(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    doctor_buses.write_config("[allow]\nreboot = yes\n")
    doctor_buses.world.can["suspend"] = "challenge"
    _, report = doctor_json(capsys)
    rule = report["polkit_rule"]
    assert '"org.freedesktop.login1.suspend", "org.freedesktop.login1.suspend-multiple-sessions"' in rule
    assert '"org.freedesktop.login1.reboot-multiple-sessions"' in rule
    assert "power-off" not in rule and "hibernate" not in rule and "ignore-inhibit" not in rule
    assert check(report, "power")[0]["level"] == "warn"
    torporctl("doctor")
    assert "polkit.addRule" in capsys.readouterr().out


def test_doctor_checks_i2c_for_ddcutil(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    _, report = doctor_json(capsys)
    assert any("i2c-dev module isn't loaded" in c["message"] for c in check(report, "brightness"))
    doctor_buses.tree.i2c(2, access=os.getuid() == 0)
    _, report = doctor_json(capsys)
    messages = [c["message"] for c in check(report, "brightness")]
    if os.getuid() != 0:
        assert any("no access to" in m for m in messages)


def test_doctor_says_whether_mode_reads_would_work_before_any_mode_code(
    doctor_buses: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recipe checks `modes` before adding a row: it must answer on Plasma too, with the agent stopped."""
    doctor_buses.world.plasma()
    doctor_buses.tree.i2c(2)
    doctor_buses.tools.set_ddc(version="1.4.1")
    _, report = doctor_json(capsys)
    assert check(report, "brightness")[0]["message"] == "Plasma's org.kde.ScreenBrightness handles every display"
    old = "ddcutil 1.4.1 can't share the bus with PowerDevil safely; monitor modes need 2.2 or later"
    assert check(report, "modes") == [
        {
            "id": "modes",
            "level": "ok",
            "message": f"the agent isn't running; {old}",
            "hint": "https://github.com/ovflowd/torpor/blob/main/docs/modules/brightness.md",
        }
    ]
    doctor_buses.tools.set_ddc(version="3.0.2")
    write_status(doctor_buses, displays=[{"id": "3f2a9c1b04de", "label": "Example 27Q", "backend": "kde"}])
    _, report = doctor_json(capsys)
    assert check(report, "modes")[0]["message"] == (
        "no display has a mode code set in Home Assistant; mode reads would work with ddcutil 3.0.2"
    )


def test_doctor_checks_mode_reads_once_home_assistant_set_a_mode_code(
    doctor_buses: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    display = {"id": "3f2a9c1b04de", "label": "Example 27Q", "backend": "kde", "mode": {"value": None, "error": None}}
    write_status(doctor_buses, displays=[display])
    doctor_buses.tools.set_ddc(version="1.4.1")
    _, report = doctor_json(capsys)
    assert check(report, "modes")[0]["level"] == "warn"
    assert "ddcutil 1.4.1 can't share the bus" in check(report, "modes")[0]["message"]
    doctor_buses.tools.set_ddc(version="3.0.2")
    doctor_buses.tree.i2c(2, access=os.getuid() == 0)
    _, report = doctor_json(capsys)
    if os.getuid() != 0:
        assert check(report, "modes")[0]["message"] == "monitor modes need access to /dev/i2c-*"
    doctor_buses.tree.i2c(2)
    _, report = doctor_json(capsys)
    assert check(report, "modes")[0] == {
        "id": "modes",
        "level": "ok",
        "message": "modes of Example 27Q read with ddcutil 3.0.2",
        "hint": None,
    }
    failing = {**display, "id": "0123456789ab", "label": "Example 32U"}
    failing["mode"] = {"value": None, "error": "ddcutil detect: 2 DDC/CI displays match Example 32U"}
    write_status(doctor_buses, displays=[display, failing])
    _, report = doctor_json(capsys)
    assert [(c["level"], c["message"]) for c in check(report, "modes")] == [
        ("warn", "Example 32U: ddcutil detect: 2 DDC/CI displays match Example 32U"),
        ("ok", "modes of Example 27Q read with ddcutil 3.0.2"),
    ]


def test_doctor_reports_how_games_show(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    _, report = doctor_json(capsys)
    assert check(report, "games")[0]["message"] == (
        "no game detection: GameMode isn't installed and [games] performance-hold is empty"
    )
    doctor_buses.world.gamemode(running=False)
    _, report = doctor_json(capsys)
    assert check(report, "games")[0]["message"] == "games show through GameMode"
    doctor_buses.set("games.performance-hold", "game-performance")
    _, report = doctor_json(capsys)
    assert [(c["level"], c["message"]) for c in check(report, "games")] == [
        ("warn", "[games] performance-hold is set, but power-profiles-daemon isn't installed"),
        ("ok", "games show through GameMode"),
    ]
    doctor_buses.world.activatable["system"].add("org.freedesktop.UPower.PowerProfiles")
    _, report = doctor_json(capsys)
    expected = "games show through GameMode and power-profiles-daemon holds matching 'game-performance'"
    assert [c["message"] for c in check(report, "games")] == [expected]
    doctor_buses.set("games.gamemode", "no")
    doctor_buses.set("games.performance-hold", "")
    _, report = doctor_json(capsys)
    assert check(report, "games")[0]["message"] == (
        "no game detection: GameMode is turned off in torpor.conf and [games] performance-hold is empty"
    )


def test_doctor_reports_wol_and_linger(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    doctor_buses.tree.nic("enp5s0", "02:00:5e:10:00:01")
    doctor_buses.tools.set(linger=False)
    _, report = doctor_json(capsys)
    assert check(report, "wake")[0]["level"] == "warn" and "wake-on-lan.md" in check(report, "wake")[0]["hint"]
    assert check(report, "linger")[0]["hint"].startswith("loginctl enable-linger")


def test_doctor_says_re_pair_needed_after_a_4401(doctor_buses: Env, capsys: pytest.CaptureFixture[str]) -> None:
    doctor_buses.paths.runtime_dir.mkdir(parents=True)
    doctor_buses.paths.status_file.write_text(
        json.dumps({"connection": {"state": "rejected", "last_close": {"code": 4401}}})
    )
    _, report = doctor_json(capsys)
    assert check(report, "connection")[0]["message"].startswith("re-pair needed")


def valid_key(request: str) -> bool:
    """RFC 6455's check, as aiohttp makes it: the key is base64 of 16 bytes."""
    for line in request.split("\r\n"):
        name, _, value = line.partition(":")
        if name.lower() == "sec-websocket-key":
            try:
                return len(base64.b64decode(value.strip(), validate=True)) == 16
            except ValueError:
                return False
    return False


@pytest.fixture
def http_server() -> Iterator[tuple[str, list[int]]]:
    answers = [101]
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                request = conn.recv(4096).decode("latin-1")
                status = answers[0] if valid_key(request) else 400
                text = {101: "Switching Protocols", 400: "Bad Request"}.get(status, "Not Found")
                conn.sendall(f"HTTP/1.1 {status} {text}\r\nContent-Length: 0\r\n\r\n".encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", answers
    server.close()


def test_doctor_checks_the_endpoint_without_a_hello(
    doctor_buses: Env, capsys: pytest.CaptureFixture[str], http_server: tuple[str, list[int]]
) -> None:
    url, answers = http_server
    doctor_buses.pair(url=url)
    _, report = doctor_json(capsys)
    assert check(report, "ha-endpoint")[0]["level"] == "ok"
    answers[0] = 404
    _, report = doctor_json(capsys)
    assert "404" in check(report, "ha-endpoint")[0]["message"]


# status (A13)


def write_status(env: Env, **values: Any) -> None:
    status = {"agent": __version__, "proto": 1, "pid": 4242, "paired": True, "ha_url": "http://192.0.2.10:8123"}
    status.update(values)
    env.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    env.paths.status_file.write_text(json.dumps(status))


def install_unit(env: Env) -> None:
    env.paths.unit_file.parent.mkdir(parents=True, exist_ok=True)
    env.paths.unit_file.write_text("[Service]\n")


@pytest.mark.parametrize(
    ("installed_", "active", "status", "code"),
    [
        (False, "inactive", None, 3),
        (True, "inactive", {"connection": {"state": "connected"}}, 1),
        (True, "active", {"paired": False, "connection": {"state": "not_paired"}}, 3),
        (True, "active", {"connection": {"state": "rejected", "last_close": {"code": 4401}}}, 5),
        (True, "active", {"connection": {"state": "connected"}}, 0),
        (True, "active", {"connection": {"state": "backoff", "last_close": {"code": None, "http": 404}}}, 6),
        (True, "active", {"connection": {"state": "rejected", "last_close": {"code": 4403}}}, 6),
    ],
)
def test_status_exit_codes(
    cli_env: Env, installed_: bool, active: str, status: dict[str, Any] | None, code: int
) -> None:
    if installed_:
        install_unit(cli_env)
    cli_env.tools.set(active=active)
    if status is not None:
        write_status(cli_env, **status)
    assert torporctl("status") == code
    assert torporctl("status", "--json") == code


def test_status_explains_the_connection(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    install_unit(cli_env)
    cli_env.tools.set(active="active")
    write_status(
        cli_env,
        connection={"state": "rejected", "last_close": {"code": 4401, "http": None}, "retry_at": 1_800_000_900},
        displays=[
            {"id": "3f2a9c1b04de", "label": "Example 27Q", "backend": "kde"},
            {
                "id": "a1b2c3d4e5f6",
                "label": "PG39WCDM",
                "backend": "kde",
                "mode": {"value": 1, "error": None, "name": "0x01 (Power Saving)"},
            },
        ],
        leases=["render"],
        state={"state": "busy", "busy": ["keep_awake"], "keep_awake": {"on": True, "applied": False}, "game": True},
        modules={
            "keep_awake": {
                "enabled": True,
                "available": False,
                "backend": None,
                "problems": [{"message": "no desktop", "hint": None}],
            }
        },
    )
    assert torporctl("status") == 5
    out = capsys.readouterr().out
    assert "the key was refused: re-pair needed" in out
    assert "re-pair needed: Reconfigure the PC in Home Assistant" in out
    assert "display 3f2a9c1b04de: Example 27Q (kde)" in out and "lease: render" in out
    assert "module keep_awake: on, unavailable" in out
    assert "keep awake: on, not applied: no desktop to inhibit" in out
    assert "display a1b2c3d4e5f6: PG39WCDM (kde)\n  mode: 0x01 (Power Saving)\n" in out
    assert "game: running" in out


def test_a_dev_agent_without_a_unit_counts_by_its_pid(cli_env: Env) -> None:
    cli_env.tree.process(4242, "python3", "-m", "torpor", "agent")
    write_status(cli_env, connection={"state": "connected"})
    assert torporctl("status") == 0


# hold, release, config, logs, restart, version


def test_hold_for_a_time_writes_until(cli_env: Env) -> None:
    assert torporctl("hold", "render", "--for", "1h30m") == 0
    text = (cli_env.paths.leases_dir / "render.lease").read_text()
    until = int(text.removeprefix("until=").strip())
    assert abs(until - (time.time() + 5400)) < 5


def test_hold_while_a_command_runs(cli_env: Env, tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    lease = cli_env.paths.leases_dir / "job.lease"
    script = f"import pathlib; pathlib.Path({str(seen)!r}).write_text(open({str(lease)!r}).read()); raise SystemExit(3)"
    assert torporctl("hold", "job", "--", sys.executable, "-c", script) == 3
    assert seen.read_text() == f"pid={os.getpid()}\n"
    assert not (cli_env.paths.leases_dir / "job.lease").exists()


@pytest.mark.parametrize("args", [["bad name"], ["ok", "--for", "soon"], ["ok", "--for", "0"]])
def test_hold_refuses_bad_names_and_durations(cli_env: Env, args: list[str]) -> None:
    assert torporctl("hold", *args) == 2


def test_durations() -> None:
    assert [cli.parse_duration(t) for t in ("90", "90s", "20m", "2h", "1h30m")] == [90, 90, 1200, 7200, 5400]


def test_release(cli_env: Env) -> None:
    assert torporctl("hold", "x", "--for", "5m") == 0
    assert torporctl("release", "x") == 0
    assert torporctl("release", "x") == 3


def test_config_get_set_unset_and_list(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert torporctl("config", "set", "ha-url", "http://10.0.0.5:8123") == 0
    assert torporctl("config", "set", "busy.processes", "rsync\\nborg create") == 0
    capsys.readouterr()
    assert torporctl("config", "get", "agent.ha-url") == 0
    assert capsys.readouterr().out == "http://10.0.0.5:8123\n"
    assert torporctl("config", "get", "busy.processes") == 0
    assert capsys.readouterr().out == "rsync borg create\n"
    assert torporctl("config", "unset", "ha-url") == 0
    assert torporctl("config", "list") == 0
    out = capsys.readouterr().out
    assert "agent.ha-url = \n" in out and "allow.hibernate = no\n" in out
    assert mode(cli_env.paths.config_file) == 0o600


def test_config_set_refuses_bad_keys_and_values(cli_env: Env) -> None:
    assert torporctl("config", "set", "allow.everything", "yes") == 2
    assert torporctl("config", "set", "allow.suspend", "perhaps") == 2
    assert torporctl("config", "set", "ha-url", "homeassistant.local") == 2
    assert not cli_env.paths.config_file.exists()


def test_config_set_repairs_a_url_that_does_not_parse(cli_env: Env) -> None:
    cli_env.write_config("[agent]\nha-url = http://[homeassistant\n")
    assert torporctl("config", "list") == 0
    assert torporctl("config", "set", "ha-url", "http://192.0.2.10:8123") == 0
    assert conf.load(cli_env.paths.config_file) == (conf.parse("[agent]\nha-url = http://192.0.2.10:8123\n")[0], [])


def test_config_set_restarts_a_running_agent(cli_env: Env) -> None:
    install_unit(cli_env)
    cli_env.tools.set(active="active")
    assert torporctl("config", "set", "allow.poweroff", "yes") == 0
    assert ["systemctl", "--user", "restart", "torpord.service"] in cli_env.tools.calls("systemctl")


def test_logs_wraps_journalctl(cli_env: Env) -> None:
    assert torporctl("logs", "-f", "-n", "50") == 0
    assert cli_env.tools.calls("journalctl") == [["journalctl", "--user", "-u", "torpord.service", "-f", "-n", "50"]]


def test_restart(cli_env: Env) -> None:
    assert torporctl("restart") == 3
    install_unit(cli_env)
    assert torporctl("restart") == 0
    cli_env.tools.set(fail=["restart"])
    assert torporctl("restart") == 1


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as done:
        torporctl("--version")
    assert done.value.code == 0
    assert capsys.readouterr().out == f"torporctl {__version__} (proto 1)\n"


def test_ctrl_c_exits_130(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(_path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "read_status", interrupted)
    assert torporctl("status") == 130


def test_usage_errors_exit_2(cli_env: Env) -> None:
    with pytest.raises(SystemExit) as done:
        torporctl("frobnicate")
    assert done.value.code == 2
