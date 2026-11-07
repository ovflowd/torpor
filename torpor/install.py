"""torporctl install, uninstall and update: files under your home and a systemd user unit. Never sudo."""

from __future__ import annotations

import importlib.resources
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipapp
import zipfile
from collections.abc import Mapping
from pathlib import Path

import torpor
from torpor import config as conf
from torpor.doctor import I2C_DEV_CONF, POLKIT_RULE, Doctor, Report, print_report, user_name
from torpor.paths import UNIT, Paths, atomic_write, ensure_dir
from torpor.runtime import Result, run, tool

ZIPAPP_MAIN = "import sys\nfrom torpor.__main__ import main\nsys.exit(main())\n"
VERSION = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)


class InstallError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def template(name: str) -> str:
    return importlib.resources.files("torpor").joinpath("units", name).read_text()


def unit_text(python: str) -> str:
    return template("torpord.service.in").replace("@PYTHON@", python)


def home_relative(path: Path, paths: Paths) -> str:
    try:
        return f"%h/{path.relative_to(paths.home)}"
    except ValueError:
        return str(path)


def hardening_text(paths: Paths) -> str:
    """The drop-in, with the config and state folders where this user's XDG settings put them."""
    text = template("50-hardening.conf")
    text = text.replace("%h/.config/torpor", home_relative(paths.config_dir, paths))
    return text.replace("%h/.local/state/torpor", home_relative(paths.state_dir, paths))


def hardening_properties(paths: Paths) -> list[str]:
    """The drop-in's settings as systemd-run -p arguments, with %h and %t filled in."""
    lines: list[str] = []
    for line in hardening_text(paths).splitlines():
        if "=" not in line or line.startswith(("#", "[")):
            continue
        value = line.replace("%h", str(paths.home)).replace("%t", str(paths.runtime))
        lines.append(value)
    return lines


def package_source() -> Path:
    """The torpor package this command runs from: a checkout folder, or the zipapp it was installed as."""
    here = Path(torpor.__file__).parent
    if here.is_dir():
        return here
    for parent in here.parents:
        if parent.is_file() and zipfile.is_zipfile(parent):
            return parent
    raise InstallError(f"can't find the torpor package to install from ({here})")


def build_zipapp(target: Path, source: Path | None = None) -> None:
    source = source or package_source()
    ensure_dir(target.parent, 0o755)
    if source.is_file():
        if source.resolve() != target.resolve():
            atomic_write(target, source.read_bytes(), 0o644)
        return
    with tempfile.TemporaryDirectory(prefix="torpor-build-") as staging:
        tree = Path(staging) / "tree"
        shutil.copytree(source, tree / "torpor", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (tree / "__main__.py").write_text(ZIPAPP_MAIN)
        built = Path(staging) / "torpor.pyz"
        zipapp.create_archive(tree, built, compressed=True)
        atomic_write(target, built.read_bytes(), 0o644)


def launcher_text(python: str, zipapp_path: Path) -> str:
    return f'#!/bin/sh\nexec {shlex.quote(python)} -I -B {shlex.quote(str(zipapp_path))} ctl "$@"\n'


def installed_python(paths: Paths) -> str | None:
    try:
        text = paths.unit_file.read_text()
    except OSError:
        return None
    match = re.search(r"^ExecStart=(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def installed_version(paths: Paths) -> str | None:
    try:
        with zipfile.ZipFile(paths.zipapp) as archive:
            text = archive.read("torpor/__init__.py").decode()
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    match = VERSION.search(text)
    return match.group(1) if match else None


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.+-]", text)[:3])


class Installer:
    def __init__(self, paths: Paths, env: Mapping[str, str], *, interactive: bool = False) -> None:
        self.paths = paths
        self.env = env
        self.interactive = interactive
        self.systemctl = tool(env, "TORPOR_SYSTEMCTL", "systemctl")
        self.systemd_run = tool(env, "TORPOR_SYSTEMD_RUN", "systemd-run")
        self.loginctl = tool(env, "TORPOR_LOGINCTL", "loginctl")

    def systemd(self, *args: str) -> Result:
        return run([self.systemctl, "--user", *args], timeout=60, env=self.env)

    @property
    def installed(self) -> bool:
        return self.paths.unit_file.exists() or self.paths.zipapp.exists()

    def install(self, *, observe_only: bool, start: bool, hardening: bool) -> int:
        report = Report()
        doctor = Doctor(self.paths, self.env)
        python = doctor.required(report)
        doctor.distro(report)
        if report.failed or python is None:
            print("torporctl install: a required check failed; nothing was written.\n", file=sys.stderr)
            print_report(Report([c for c in report.checks if c.level != "ok"]))
            return 4
        python = os.path.abspath(python)
        paths = self.paths
        for folder, mode in ((paths.config_dir, 0o700), (paths.state_dir, 0o700), (paths.runtime_dir, 0o700)):
            ensure_dir(folder, mode)
        ensure_dir(paths.unit_dir, 0o755)
        ensure_dir(paths.launcher.parent, 0o755)
        build_zipapp(paths.zipapp)
        atomic_write(paths.launcher, launcher_text(python, paths.zipapp), 0o755)
        atomic_write(paths.unit_file, unit_text(python), 0o644)
        print(f"Installed {paths.zipapp}, {paths.launcher} and {paths.unit_file}")
        if not paths.config_file.exists():
            atomic_write(paths.config_file, conf.DEFAULT_TEXT, 0o600)
            print(f"Wrote the default {paths.config_file}")
        if observe_only:
            text = conf.set_value(paths.config_file.read_text(), "agent", "observe-only", "yes")
            atomic_write(paths.config_file, text, 0o600)
            print("Observe-only: the agent reports but executes nothing (agent.observe-only = yes)")
        if hardening:
            paths.no_hardening_file.unlink(missing_ok=True)
            self.harden(python)
        else:
            atomic_write(paths.no_hardening_file, "torporctl install --no-hardening\n", 0o600)
            self.remove_hardening("skipped with --no-hardening")
        reload = self.systemd("daemon-reload")
        if not reload.ok:
            print(f"systemctl --user daemon-reload failed: {reload.output.strip()}", file=sys.stderr)
            return 1
        if start:
            for step in (("enable", UNIT), ("restart", UNIT)):
                result = self.systemd(*step)
                if not result.ok:
                    print(f"systemctl --user {' '.join(step)} failed: {result.output.strip()}", file=sys.stderr)
                    return 1
            print("Started torpord.service")
        else:
            print("Not started. When you are ready: systemctl --user enable --now torpord.service")
        self.linger()
        command = "torporctl"
        if not self.on_path(paths.launcher.parent):
            command = str(paths.launcher)
            print(f"{paths.launcher.parent} isn't on your PATH: add it in your shell's profile, or run {command}")
        if not paths.key_file.exists():
            print(f"Next: add the Torpor integration in Home Assistant, then run {command} pair --url <its URL>")
        return 0

    def on_path(self, folder: Path) -> bool:
        entries = self.env.get("PATH", "").split(os.pathsep)
        return any(entry and Path(os.path.expanduser(entry)) == folder for entry in entries)

    def harden(self, python: str) -> None:
        properties = hardening_properties(self.paths)
        argv = [self.systemd_run, "--user", "--wait", "--pipe", "--quiet", "--collect"]
        for prop in properties:
            argv += ["-p", prop]
        argv += [python, "-I", "-B", str(self.paths.zipapp), "agent", "--self-test"]
        result = run(argv, timeout=90, env=self.env)
        if result.ok and "ok" in result.output.split():
            atomic_write(self.paths.hardening_file, hardening_text(self.paths), 0o644)
            print(f"Hardening drop-in written: {self.paths.hardening_file}")
            return
        why = result.output.strip().splitlines()[-1] if result.output.strip() else f"exit {result.code}"
        self.remove_hardening(f"the probe under those settings failed ({why})")

    def remove_hardening(self, why: str) -> None:
        if self.paths.hardening_file.exists():
            self.paths.hardening_file.unlink()
        print(f"No hardening drop-in: {why}. torporctl doctor will warn about it.")

    def linger(self) -> None:
        user = user_name(os.getuid())
        result = run([self.loginctl, "show-user", user, "--property=Linger", "--value"], timeout=15, env=self.env)
        if result.ok and result.output.strip() == "yes":
            print("Linger is on: torpord keeps running while you are logged out.")
            return
        print("Linger is off: torpord stops when you log out, and Home Assistant loses the PC.")
        command = [self.loginctl, "enable-linger"]
        if self.interactive and sys.stdin.isatty():
            answer = input("Turn linger on now with `loginctl enable-linger`? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                done = run(command, timeout=30, env=self.env)
                print("Linger is on." if done.ok else f"loginctl enable-linger failed: {done.output.strip()}")
                return
        print("To turn it on: loginctl enable-linger")

    def uninstall(self, *, purge: bool) -> int:
        paths = self.paths
        if not self.installed and not (purge and (paths.config_dir.exists() or paths.state_dir.exists())):
            print("torpord is not installed.", file=sys.stderr)
            return 3
        status = 0
        if paths.unit_file.exists():
            result = self.systemd("disable", "--now", UNIT)
            if not result.ok:
                print(f"systemctl --user disable --now failed: {result.output.strip()}", file=sys.stderr)
                (paths.unit_dir / "default.target.wants" / UNIT).unlink(missing_ok=True)
                status = 1
        for path in (paths.unit_file, paths.hardening_file, paths.zipapp, paths.launcher):
            path.unlink(missing_ok=True)
        for folder in (paths.dropin_dir, paths.lib_dir):
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        shutil.rmtree(paths.runtime_dir, ignore_errors=True)
        self.systemd("daemon-reload")
        print("Removed torpord: the unit, its drop-in, the zipapp, the launcher and the runtime folder.")
        if purge:
            shutil.rmtree(paths.config_dir, ignore_errors=True)
            shutil.rmtree(paths.state_dir, ignore_errors=True)
            print(f"Purged {paths.config_dir} (the pairing key too) and {paths.state_dir}.")
            print("In Home Assistant, delete the PC's Torpor entry as well.")
        else:
            print(f"Kept {paths.config_dir} and {paths.state_dir}; --purge removes them.")
        print("Linger is left as it is. To turn it off: loginctl disable-linger")
        print(f"A polkit rule you installed keeps allowing what it allowed: sudo rm {POLKIT_RULE}")
        print(f"If nothing else needs i2c-dev at boot: sudo rm {I2C_DEV_CONF}")
        return status

    def update(self, source: Path, *, force: bool) -> int:
        if not self.installed:
            print("torpord is not installed; use torporctl install.", file=sys.stderr)
            return 3
        python = installed_python(self.paths) or sys.executable
        with tempfile.TemporaryDirectory(prefix="torpor-update-") as scratch:
            root = self.unpack(source, Path(scratch))
            new = VERSION.search((root / "torpor" / "__init__.py").read_text())
            if new is None:
                raise InstallError(f"{source} has no torpor/__init__.py with a version", 2)
            old = installed_version(self.paths)
            if old is not None and version_tuple(new.group(1)) < version_tuple(old) and not force:
                print(
                    f"{source} has torpor {new.group(1)}, older than the installed {old}; --force installs it anyway."
                )
                return 1
            print(f"Updating torpor {old or '?'} -> {new.group(1)} from {source}")
            argv = [python, "-I", "-B", str(root / "torporctl"), "install"]
            if self.paths.no_hardening_file.exists():
                argv.append("--no-hardening")
            done = subprocess.run(argv, env=dict(self.env), check=False)
            return done.returncode

    @staticmethod
    def unpack(source: Path, scratch: Path) -> Path:
        if source.is_dir():
            root = source
        elif source.is_file() and tarfile.is_tarfile(source):
            with tarfile.open(source) as archive:
                archive.extractall(scratch, filter="data")
            found = [*scratch.glob("*/torpor/__init__.py"), *scratch.glob("torpor/__init__.py")]
            if not found:
                raise InstallError(f"{source} doesn't hold a torpor checkout", 2)
            root = found[0].parent.parent
        else:
            raise InstallError(f"{source} is neither a torpor checkout nor a .tar.gz", 2)
        if not (root / "torpor" / "__init__.py").is_file() or not (root / "torporctl").is_file():
            raise InstallError(f"{source} doesn't look like a torpor checkout (no torpor/ or torporctl)", 2)
        return root
