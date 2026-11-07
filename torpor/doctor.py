"""torporctl doctor: check what Torpor needs, and print, never run, the commands that fix it."""

from __future__ import annotations

import base64
import json
import os
import pwd
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from torpor import codes
from torpor.backends import net, sysfs
from torpor.backends.ddcutil import MODE_MIN_VERSION, Ddcutil
from torpor.backends.games import GameMode, PowerProfiles
from torpor.bus import Bus, BusError
from torpor.codes import meaning
from torpor.config import Config, Issue, load
from torpor.keyfile import read_key
from torpor.paths import Paths
from torpor.runtime import run, tool
from torpor.status import read as read_status

DOCS = "https://github.com/ovflowd/torpor/blob/main/docs"
MIN_PYTHON = (3, 12)
POLKIT_RULE = "/etc/polkit-1/rules.d/50-torpor.rules"
I2C_DEV_CONF = "/etc/modules-load.d/i2c-dev.conf"

PROBE = r"""
import json, sys
r = {"python": "%d.%d.%d" % sys.version_info[:3], "executable": sys.executable}
try:
    import gi
    r["pygobject"] = gi.__version__
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    r["glib"] = "%d.%d.%d" % (GLib.MAJOR_VERSION, GLib.MINOR_VERSION, GLib.MICRO_VERSION)
    gi.require_version("Soup", "3.0")
    from gi.repository import Soup
    r["soup"] = "%d.%d.%d" % (Soup.get_major_version(), Soup.get_minor_version(), Soup.get_micro_version())
except Exception as e:
    r["gi_error"] = "%s: %s" % (type(e).__name__, e)
try:
    import cryptography
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    a, b = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    s = a.exchange(b.public_key())
    assert s == b.exchange(a.public_key())
    c = ChaCha20Poly1305(s)
    assert c.decrypt(bytes(12), c.encrypt(bytes(12), b"torpor", b"1"), b"1") == b"torpor"
    r["cryptography"] = cryptography.__version__
except Exception as e:
    r["cryptography_error"] = "%s: %s" % (type(e).__name__, e)
print(json.dumps(r))
"""

PACKAGES: dict[str, dict[str, str]] = {
    "arch": {
        "python": "python",
        "pygobject": "python-gobject",
        "glib": "glib2",
        "soup": "libsoup3",
        "cryptography": "python-cryptography",
        "ddcutil": "ddcutil",
        "wol": "networkmanager",
        "tests": "python-pytest",
    },
    "debian": {
        "python": "python3",
        "pygobject": "python3-gi",
        "glib": "gir1.2-glib-2.0",
        "soup": "gir1.2-soup-3.0",
        "cryptography": "python3-cryptography",
        "ddcutil": "ddcutil",
        "wol": "network-manager",
        "tests": "python3-pytest",
    },
    "fedora": {
        "python": "python3",
        "pygobject": "python3-gobject-base",
        "glib": "glib2",
        "soup": "libsoup3",
        "cryptography": "python3-cryptography",
        "ddcutil": "ddcutil",
        "wol": "NetworkManager",
        "tests": "python3-pytest",
    },
}
REQUIRED = {
    "arch": ("python", "pygobject", "soup", "cryptography"),
    "debian": ("python", "pygobject", "glib", "soup", "cryptography"),
    "fedora": ("python", "pygobject", "soup", "cryptography"),
}
INSTALLER = {"arch": "sudo pacman -S --needed", "debian": "sudo apt install", "fedora": "sudo dnf install"}
POLKIT_ACTIONS = {
    "suspend": "org.freedesktop.login1.suspend",
    "hibernate": "org.freedesktop.login1.hibernate",
    "poweroff": "org.freedesktop.login1.power-off",
    "reboot": "org.freedesktop.login1.reboot",
}


@dataclass(frozen=True)
class Check:
    id: str
    level: str
    message: str
    hint: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    distro: dict[str, Any] = field(default_factory=dict)
    polkit_rule: str = ""
    python: str = ""

    def add(self, id: str, level: str, message: str, hint: str | None = None) -> None:
        self.checks.append(Check(id, level, message, hint))

    @property
    def failed(self) -> bool:
        return any(check.level == "fail" for check in self.checks)

    def wire(self) -> dict[str, Any]:
        return {
            "ok": not self.failed,
            "checks": [asdict(check) for check in self.checks],
            "distro": self.distro,
            "polkit_rule": self.polkit_rule,
        }


def user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def polkit_rule(config: Config, user: str) -> str:
    actions: list[str] = []
    for name, action in POLKIT_ACTIONS.items():
        if config.allow.get(name):
            actions += [action, f"{action}-multiple-sessions"]
    if not actions:
        return ""
    allowed = ", ".join(json.dumps(a) for a in actions)
    names = ", ".join(n for n in POLKIT_ACTIONS if config.allow.get(n))
    return (
        f"// {POLKIT_RULE}: lets {user} {names} with no active session\n"
        "polkit.addRule(function (action, subject) {\n"
        f"    var allowed = [{allowed}];\n"
        f"    if (subject.user == {json.dumps(user)} && allowed.indexOf(action.id) >= 0) {{\n"
        "        return polkit.Result.YES;\n"
        "    }\n"
        "});\n"
    )


def probe_python(python: str) -> dict[str, Any]:
    try:
        done = subprocess.run(
            [python, "-I", "-B", "-c", PROBE], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": f"cannot run {python}: {error}"}
    lines = done.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    if not isinstance(result, dict) or "python" not in result:
        return {"error": f"{python} did not answer the probe: {(done.stderr or done.stdout).strip()[-200:]}"}
    return result


def python_candidates(env: Mapping[str, str]) -> list[str]:
    if env.get("TORPOR_PYTHON"):
        return [env["TORPOR_PYTHON"]]
    found: list[str] = []
    if sys.prefix == sys.base_prefix and sys.executable:
        found.append(sys.executable)
    for path in ("/usr/bin/python3",):
        if path not in found and Path(path).exists():
            found.append(path)
    return found


def new_enough(version: str) -> bool:
    return tuple(int(part) for part in version.split(".")[:2]) >= MIN_PYTHON


def check_python(report: Report, probe: dict[str, Any], python: str) -> None:
    if "error" in probe:
        report.add("python", "fail", probe["error"])
        return
    report.python = str(probe["python"])
    if not new_enough(report.python):
        report.add("python", "fail", f"{python} is Python {probe['python']}; Torpor needs 3.12 or later")
    else:
        report.add("python", "ok", f"{python} is Python {probe['python']}")
    error = str(probe.get("gi_error", ""))
    if "pygobject" not in probe:
        report.add("pygobject", "fail", f"PyGObject is missing ({error})")
        return
    report.add("pygobject", "ok", f"PyGObject {probe['pygobject']}")
    if "glib" not in probe:
        report.add("glib", "fail", f"the GLib and Gio typelibs are missing ({error})")
        return
    report.add("glib", "ok", f"GLib {probe['glib']}")
    if "soup" not in probe:
        report.add("soup", "fail", f"the libsoup 3 typelib is missing ({error})")
    else:
        report.add("soup", "ok", f"libsoup {probe['soup']}")


def check_cryptography(report: Report, probe: dict[str, Any]) -> None:
    if "error" in probe:
        return
    if "cryptography" in probe:
        report.add("cryptography", "ok", f"cryptography {probe['cryptography']}, X25519 and ChaCha20-Poly1305 work")
    else:
        report.add("cryptography", "fail", f"cryptography is missing or broken ({probe.get('cryptography_error')})")


class Doctor:
    def __init__(
        self,
        paths: Paths,
        env: Mapping[str, str],
        *,
        buses: Callable[[], tuple[Bus | None, Bus | None]] | None = None,
        python: str | None = None,
    ) -> None:
        self.paths = paths
        self.env = env
        self.buses = buses
        self.python = python
        self.sysfs, self.proc = Path(env.get("TORPOR_SYSFS") or "/sys"), Path(env.get("TORPOR_PROC") or "/proc")
        self.dev = Path(env.get("TORPOR_DEV") or "/dev")
        self.run_root = Path(env.get("TORPOR_RUN") or "/run")
        self.os_release = Path(env.get("TORPOR_OS_RELEASE") or "/etc/os-release")
        self.uid = os.getuid()
        self.systemctl = tool(env, "TORPOR_SYSTEMCTL", "systemctl")
        self.loginctl = tool(env, "TORPOR_LOGINCTL", "loginctl")
        self.config, self.issues = load(paths.config_file)

    def systemd(self, *args: str) -> Any:
        return run([self.systemctl, "--user", *args], timeout=15, env=self.env)

    def required(self, report: Report) -> str | None:
        """The checks `install` needs. Returns the interpreter that passed them."""
        chosen: str | None = None
        candidates = [self.python] if self.python else python_candidates(self.env)
        probes = [(python, probe_python(python)) for python in candidates]
        for python, probe in probes:
            if "error" not in probe and "soup" in probe and "cryptography" in probe and new_enough(probe["python"]):
                chosen = python
                check_python(report, probe, python)
                check_cryptography(report, probe)
                break
        if chosen is None:
            if probes:
                python, probe = probes[0]
                check_python(report, probe, python)
                check_cryptography(report, probe)
            else:
                report.add("python", "fail", "no system python3 found")
        version = self.systemd("show", "--property=Version", "--value")
        if version.ok:
            report.add("systemd-user", "ok", f"systemd user manager {version.output.strip()}")
        else:
            report.add("systemd-user", "fail", "the systemd user manager isn't reachable", "systemctl --user status")
        sessions = run([self.loginctl, "--no-legend", "list-sessions"], timeout=15, env=self.env)
        if sessions.ok:
            report.add("logind", "ok", "systemd-logind answers")
        else:
            report.add("logind", "fail", "org.freedesktop.login1 isn't reachable on the system bus")
        self.config_check(report, self.issues)
        return chosen

    def config_check(self, report: Report, issues: list[Issue]) -> None:
        fails = [issue.message for issue in issues if issue.level == "fail"]
        others = [issue.message for issue in issues if issue.level != "fail"]
        if fails:
            report.add("config", "fail", fails[0], f"edit {self.paths.config_file}")
        elif others:
            report.add("config", "warn", "; ".join(others), f"edit {self.paths.config_file}")
        else:
            if self.paths.config_file.exists():
                report.add("config", "ok", f"{self.paths.config_file} parses")
            else:
                report.add("config", "ok", "no torpor.conf yet: every default")

    def run_all(self) -> Report:
        report = Report()
        self.required(report)
        self.install_checks(report)
        self.pairing_checks(report)
        self.linger(report)
        system, session = self.buses() if self.buses else (None, None)
        user = user_name(self.uid)
        report.polkit_rule = polkit_rule(self.config, user)
        self.power(report, system)
        self.keep_awake(report, session)
        self.wake(report)
        self.brightness(report, session)
        self.modes(report)
        self.games(report, system, session)
        self.distro(report)
        return report

    def install_checks(self, report: Report) -> None:
        if not self.paths.unit_file.exists() or not self.paths.zipapp.exists():
            report.add("installed", "warn", "torpord is not installed", "torporctl install")
            return
        report.add("installed", "ok", f"installed in {self.paths.zipapp}")
        enabled = self.systemd("is-enabled", "torpord.service").output.strip()
        active = self.systemd("is-active", "torpord.service").output.strip()
        if enabled != "enabled":
            report.add(
                "agent",
                "warn",
                f"torpord.service is {enabled or 'not enabled'}",
                "systemctl --user enable --now torpord.service",
            )
        elif active != "active":
            report.add("agent", "warn", f"torpord.service is {active or 'not running'}", "torporctl logs")
        else:
            report.add("agent", "ok", "torpord.service is enabled and running")
        if self.paths.hardening_file.exists():
            report.add("hardening", "ok", "the hardening drop-in is in place")
        else:
            report.add(
                "hardening",
                "warn",
                "no hardening drop-in: the probe failed or --no-hardening was used",
                "torporctl install (it probes again)",
            )

    def pairing_checks(self, report: Report) -> None:
        key = read_key(self.paths.key_file)
        if key is None or not self.config.ha_url:
            missing = "no pairing key" if key is None else "no ha-url"
            report.add("paired", "warn", f"not paired: {missing}", "torporctl pair --url http://<home assistant>:8123")
        else:
            report.add("paired", "ok", f"paired with {self.config.ha_url}")
        status = read_status(self.paths.status_file) or {}
        last = (status.get("connection") or {}).get("last_close") or {}
        code, http = last.get("code"), last.get("http")
        if code == codes.AUTH_FAILED:
            report.add("connection", "warn", "re-pair needed: Home Assistant refused the key", "torporctl pair")
        elif code in (codes.NOT_LOCAL, codes.UNSUPPORTED) or http is not None:
            report.add("connection", "warn", meaning(code, http), f"{DOCS}/troubleshooting.md")
        elif (status.get("connection") or {}).get("state") == "connected":
            report.add("connection", "ok", "connected to Home Assistant")
        if self.config.ha_url:
            self.endpoint(report, self.config.ha_url)

    def endpoint(self, report: Report, ha_url: str) -> None:
        status, error = upgrade_status(ha_url, tls_verify=self.config.tls_verify)
        if status == 101:
            report.add("ha-endpoint", "ok", "Home Assistant upgrades /api/torpor/v1/ws to a WebSocket")
        elif status == 404:
            report.add(
                "ha-endpoint",
                "warn",
                "Home Assistant answers 404: the Torpor integration isn't installed or loaded",
                "install ha-torpor in Home Assistant and add the Torpor integration",
            )
        elif status is not None:
            report.add("ha-endpoint", "warn", f"the WebSocket upgrade got HTTP {status}: a proxy in the way?")
        else:
            report.add("ha-endpoint", "warn", f"Home Assistant isn't reachable at {ha_url}: {error}")

    def linger(self, report: Report) -> None:
        user = user_name(self.uid)
        result = run([self.loginctl, "show-user", user, "--property=Linger", "--value"], timeout=15, env=self.env)
        if result.ok and result.output.strip() == "yes":
            report.add("linger", "ok", "linger is on: torpord runs while you are logged out")
        else:
            report.add(
                "linger",
                "warn",
                "linger is off: torpord stops when you log out",
                f"loginctl enable-linger {shlex.quote(user)}",
            )

    def power(self, report: Report, system: Bus | None) -> None:
        if system is None:
            report.add("power", "warn", "can't ask logind without the system bus and PyGObject")
            return
        from torpor.backends.logind import Logind

        try:
            can = Logind(system).can("suspend")
        except BusError as error:
            report.add("power", "warn", f"asking logind failed: {error.message}")
            return
        if can == "yes":
            report.add("power", "ok", "logind allows suspend from here")
        else:
            report.add(
                "power",
                "warn",
                f"logind says CanSuspend={can}; with no active session polkit asks for an admin password",
                "install the polkit rule below (docs/modules/power.md)",
            )

    def keep_awake(self, report: Report, session: Bus | None) -> None:
        names = {
            "kde": "org.freedesktop.PowerManagement",
            "gnome": "org.gnome.SessionManager",
            "portal": "org.freedesktop.portal.Desktop",
        }
        if session is None:
            report.add("keep-awake", "warn", "no session bus: no desktop to keep awake right now")
            return
        present = [name for name, bus_name in names.items() if session.has_owner(bus_name)]
        if present:
            report.add("keep-awake", "ok", f"idle inhibit through {present[0]}")
        else:
            report.add("keep-awake", "warn", "no desktop idle inhibit is running now (log in to a desktop)")

    def wake(self, report: Report) -> None:
        nmcli = tool(self.env, "TORPOR_NMCLI", "nmcli")
        nics = net.scan(
            self.sysfs, self.proc, nmcli=nmcli, run=lambda argv: run(argv, timeout=10, env=self.env), ha_address=None
        )
        armed = [nic.name for nic in nics if nic.wol == "armed"]
        if armed:
            report.add("wake", "ok", f"Wake-on-LAN armed on {', '.join(armed)}")
        elif nics:
            listing = ", ".join(f"{nic.name} {nic.wol}" for nic in nics)
            report.add("wake", "warn", f"no interface has Wake-on-LAN armed ({listing})", f"{DOCS}/wake-on-lan.md")
        else:
            report.add("wake", "warn", "no physical network interface found")

    def brightness(self, report: Report, session: Bus | None) -> None:
        if session is not None and session.has_owner("org.kde.ScreenBrightness"):
            report.add("brightness", "ok", "Plasma's org.kde.ScreenBrightness handles every display")
            return
        found: list[str] = []
        lights = sysfs.backlights(self.sysfs)
        if lights:
            found.append(f"backlight {', '.join(light.name for light in lights)} (logind)")
        binary = shutil.which(self.env.get("TORPOR_DDCUTIL") or self.config.ddcutil)
        version = Ddcutil(binary, env=self.env).version() if binary else None
        problems: list[tuple[str, str | None]] = []
        if version is None:
            problems.append(("ddcutil is not installed (needed for external monitors)", self.package_hint("ddcutil")))
        else:
            found.append(f"ddcutil {'.'.join(map(str, version))}")
        loaded = sysfs.i2c_dev_loaded(self.sysfs) or (self.sysfs / "class" / "i2c-dev").is_dir()
        if not loaded:
            problems.append(("the i2c-dev module isn't loaded", f"echo i2c-dev | sudo tee {I2C_DEV_CONF}"))
        devices = sorted(self.dev.glob("i2c-*"))
        if loaded and devices and not any(os.access(d, os.R_OK | os.W_OK) for d in devices):
            problems.append((f"no access to {devices[0]} and the other /dev/i2c-*", f"{DOCS}/modules/brightness.md"))
        for message, hint in problems:
            report.add("brightness", "warn", message, hint)
        if found and not problems:
            report.add("brightness", "ok", "; ".join(found))
        elif not found and not problems:
            report.add("brightness", "warn", "no display with a brightness control found")

    def modes(self, report: Report) -> None:
        """Whether this PC can read monitor modes, asked before any display has a mode code too, and how the
        reads go for each display that has one, from status.json."""
        binary = shutil.which(self.env.get("TORPOR_DDCUTIL") or self.config.ddcutil)
        version = Ddcutil(binary, env=self.env).version() if binary else None
        found = ".".join(map(str, version)) if version is not None else ""
        devices = sorted(self.dev.glob("i2c-*"))
        missing: tuple[str, str | None] | None = None
        if version is None:
            missing = ("reading monitor modes needs ddcutil 2.2 or later", self.package_hint("ddcutil"))
        elif version < MODE_MIN_VERSION:
            missing = (
                f"ddcutil {found} can't share the bus with PowerDevil safely; monitor modes need 2.2 or later",
                f"{DOCS}/modules/brightness.md",
            )
        elif not any(os.access(device, os.R_OK | os.W_OK) for device in devices):
            missing = ("monitor modes need access to /dev/i2c-*", f"{DOCS}/modules/brightness.md")
        status = read_status(self.paths.status_file)
        displays = [d for d in (status or {}).get("displays") or [] if isinstance(d.get("mode"), dict)]
        if not displays:
            why = "the agent isn't running" if status is None else "no display has a mode code set in Home Assistant"
            if missing is None:
                report.add("modes", "ok", f"{why}; mode reads would work with ddcutil {found}")
            else:
                report.add("modes", "ok", f"{why}; {missing[0]}", missing[1])
            return
        if missing is not None:
            report.add("modes", "warn", missing[0], missing[1])
            return
        working: list[str] = []
        for display in displays:
            label = str(display.get("label") or display.get("id"))
            error = display["mode"].get("error")
            if isinstance(error, str) and error:
                report.add("modes", "warn", f"{label}: {error}", f"{DOCS}/troubleshooting.md")
            else:
                working.append(label)
        if working:
            report.add("modes", "ok", f"modes of {', '.join(working)} read with ddcutil {found}")

    def games(self, report: Report, system: Bus | None, session: Bus | None) -> None:
        found: list[str] = []
        none: list[str] = []
        pattern = self.config.performance_hold
        try:
            if not self.config.gamemode:
                none.append("GameMode is turned off in torpor.conf")
            elif session is None:
                none.append("no session bus to find GameMode on")
            elif GameMode(session).installed():
                found.append("GameMode")
            else:
                none.append("GameMode isn't installed")
            if not pattern:
                none.append("[games] performance-hold is empty")
            elif system is not None and PowerProfiles(system).installed():
                found.append(f"power-profiles-daemon holds matching {pattern!r}")
            else:
                report.add(
                    "games",
                    "warn",
                    "[games] performance-hold is set, but power-profiles-daemon isn't installed",
                    f"{DOCS}/modules/brightness.md#game-boost",
                )
        except BusError as error:
            report.add("games", "warn", f"asking for games failed: {error.message}")
            return
        if found:
            report.add("games", "ok", f"games show through {' and '.join(found)}")
        else:
            report.add("games", "ok", f"no game detection: {' and '.join(none)}")

    def family(self) -> tuple[str | None, dict[str, str]]:
        release = sysfs.os_release(self.os_release)
        ids = [release.get("ID", ""), *release.get("ID_LIKE", "").split()]
        for family in ("arch", "fedora", "debian"):
            if family in ids or (family == "debian" and "ubuntu" in ids):
                return family, release
        return None, release

    def package_hint(self, need: str) -> str | None:
        family, _ = self.family()
        if family is None:
            return None
        return f"{self.installer(family)} {PACKAGES[family][need]}"

    def installer(self, family: str) -> str:
        if family == "fedora" and (self.run_root / "ostree-booted").exists():
            return "sudo rpm-ostree install"
        return INSTALLER[family]

    def distro(self, report: Report) -> None:
        family, release = self.family()
        distro_id = release.get("ID", "unknown")
        version = release.get("VERSION_ID", "")
        info: dict[str, Any] = {"id": distro_id, "version": version, "family": family, "supported": True}
        old_python = bool(report.python) and not new_enough(report.python)
        unsupported = None
        if not (self.run_root / "systemd" / "system").is_dir():
            unsupported = "this system doesn't run systemd"
        elif distro_id == "steamos":
            unsupported = "SteamOS has a read-only root: the packages can't be installed"
        elif distro_id == "debian" and version.split(".")[0].isdigit() and int(version.split(".")[0]) < 13:
            unsupported = "Debian 12 and older ship Python 3.11"
        elif distro_id == "ubuntu" and version and tuple(map(int, version.split(".")[:2])) < (24, 4):
            unsupported = "Ubuntu before 24.04 is too old"
        elif old_python:
            unsupported = f"this distribution's Python {report.python} is too old; Torpor needs 3.12 or later"
        if unsupported:
            info["supported"] = False
            report.add("distro", "warn", f"unsupported: {unsupported}")
        if family is None:
            report.distro = info
            if not unsupported:
                report.add(
                    "distro",
                    "warn",
                    f"{release.get('PRETTY_NAME', distro_id)}: no package list for it, see docs/install.md",
                )
            return
        packages = PACKAGES[family]
        failed = {check.id for check in report.checks if check.level == "fail"}
        if "pygobject" in failed:
            failed |= {"glib", "soup"} & set(REQUIRED[family])
        missing: list[str] = []
        for need in ("python", "pygobject", "glib", "soup", "cryptography"):
            if need in failed and packages[need] not in missing and not old_python:
                missing.append(packages[need])
        info["packages"] = {
            "required": [packages[need] for need in REQUIRED[family]],
            "ddcutil": [packages["ddcutil"]],
            "wol": [packages["wol"]],
            "tests": [packages["tests"]],
        }
        info["installer"] = self.installer(family)
        info["missing"] = missing
        if missing:
            command = f"{self.installer(family)} {' '.join(missing)}"
            info["command"] = command
            report.add(
                "distro", "fail" if not unsupported else "warn", f"missing packages: {' '.join(missing)}", command
            )
        elif not unsupported:
            report.add("distro", "ok", f"{release.get('PRETTY_NAME', distro_id)}: required packages present")
        report.distro = info


def upgrade_status(ha_url: str, *, tls_verify: bool, timeout: float = 5) -> tuple[int | None, str]:
    """The HTTP status of a WebSocket upgrade on Torpor's endpoint. The socket closes before any hello."""
    parts = urlsplit(ha_url)
    secure = parts.scheme == "https"
    host = parts.hostname or ""
    path = parts.path.rstrip("/") + codes.WS_PATH
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    try:
        port = parts.port or (443 if secure else 80)
    except ValueError as error:
        return None, str(error)
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            stream: socket.socket = raw
            if secure:
                context = ssl.create_default_context()
                if not tls_verify:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                stream = context.wrap_socket(raw, server_hostname=host)
            stream.sendall(request.encode())
            head = stream.recv(1024).decode("latin-1")
    except (OSError, ssl.SSLError) as error:
        return None, str(error)
    first = head.split("\r\n", 1)[0].split()
    if len(first) >= 2 and first[1].isdigit():
        return int(first[1]), ""
    return None, "no HTTP answer"


def print_report(report: Report) -> None:
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    for check in report.checks:
        print(f"{marks[check.level]}  {check.id:<13} {check.message}")
        if check.hint and check.level != "ok":
            print(f"      {'':<13} fix: {check.hint}")
    if report.polkit_rule:
        print("\nWith nobody logged in, polkit asks for a password. This rule allows what [allow] permits")
        print("(install it as root):\n")
        print(report.polkit_rule)
