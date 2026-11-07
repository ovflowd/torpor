"""torporctl: install, pair, configure and inspect the Torpor agent."""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from torpor import __version__, codes, config, host, leases
from torpor.codes import meaning
from torpor.keyfile import write_key
from torpor.paths import UNIT, Paths, atomic_write, ensure_dir
from torpor.runtime import Result, Runtime, run, tool
from torpor.status import read as read_status

CONNECT_WAIT_S = 30
DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?")


class Usage(Exception):
    pass


def default_runtime() -> Runtime:
    from torpor.glib import build_runtime

    return build_runtime()


RUNTIME_FACTORY: Callable[[], Runtime] = default_runtime


def parse_duration(text: str) -> int:
    match = DURATION.fullmatch(text.strip())
    if not text.strip() or match is None or not any(match.groups()):
        raise Usage(f"{text!r} is not a duration such as 90, 90s, 20m, 2h or 1h30m")
    hours, minutes, seconds = (int(group or 0) for group in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise Usage("a duration must be longer than 0 s")
    return total


def systemctl(env: dict[str, str], *args: str) -> Result:
    return run([tool(env, "TORPOR_SYSTEMCTL", "systemctl"), "--user", *args], timeout=60, env=env)


def agent_active(env: dict[str, str]) -> bool:
    return systemctl(env, "is-active", UNIT).output.strip() == "active"


def restart_agent(paths: Paths, env: dict[str, str], *, quiet: bool = False) -> int:
    if not paths.unit_file.exists():
        if not quiet:
            print("torpord is not installed (torporctl install).", file=sys.stderr)
        return 3
    result = systemctl(env, "restart", UNIT)
    if not result.ok:
        print(f"systemctl --user restart {UNIT} failed: {result.output.strip()}", file=sys.stderr)
        return 1
    if not quiet:
        print("Restarted torpord.")
    return 0


def cmd_install(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    from torpor.install import Installer

    installer = Installer(paths, env, interactive=True)
    return installer.install(observe_only=args.observe_only, start=not args.no_start, hardening=not args.no_hardening)


def cmd_uninstall(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    from torpor.install import Installer

    return Installer(paths, env).uninstall(purge=args.purge)


def cmd_update(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    from torpor.install import Installer, InstallError

    try:
        return Installer(paths, env).update(Path(args.source), force=args.force)
    except InstallError as error:
        print(f"torporctl update: {error}", file=sys.stderr)
        return error.code


def read_code(args: argparse.Namespace) -> bytes:
    if args.code_stdin:
        text = sys.stdin.readline()
    elif sys.stdin.isatty():
        text = getpass.getpass("Pairing code from Home Assistant: ")
    else:
        raise Usage("stdin is not a terminal: pass the code on stdin with --code-stdin")
    from torpor import protocol

    try:
        return protocol.parse_code(text)
    except ValueError as error:
        raise Usage(f"{error}: copy the code exactly as Home Assistant shows it (trp1-…)") from error


def write_pairing(paths: Paths, key: bytes, url: str) -> None:
    write_key(paths.key_file, key)
    text = paths.config_file.read_text() if paths.config_file.exists() else config.DEFAULT_TEXT
    atomic_write(paths.config_file, config.set_value(text, "agent", "ha-url", url), 0o600)


def wait_connected(paths: Paths, rt: Runtime, after: float) -> bool:
    def connected() -> bool:
        status = read_status(paths.status_file) or {}
        connection = status.get("connection") or {}
        return connection.get("state") == "connected" and float(status.get("started") or 0) >= after - 1

    return rt.loop.run_until(connected, CONNECT_WAIT_S)


def cmd_pair(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    if args.stray:
        raise Usage("the pairing code is never taken from the command line; torporctl pair asks for it")
    current, _issues = config.load(paths.config_file)
    try:
        url = config.parse_url(args.url or current.ha_url)
    except config.ConfigError as error:
        raise Usage(str(error)) from error
    if not url:
        raise Usage("pass --url with Home Assistant's address, for example --url http://homeassistant.local:8123")
    code = read_code(args)
    rt = RUNTIME_FACTORY()
    from torpor.agent import Agent
    from torpor.client import Identity, Pairing

    agent = Agent(rt, current, paths, observe_only=current.observe_only, readonly=True)
    identity = Identity(host.agent_id(rt.env), __version__)
    name = host.hostname()
    print(f"Pairing {name} with {url} ...")

    def fingerprint(text: str) -> None:
        print(f"\nIn Home Assistant, check that it shows this PC as {name} with the fingerprint {text}.")
        print("If they match, confirm there and pick the features. If not, cancel there.\n")

    def paired(key: bytes) -> None:
        write_pairing(paths, key, url)
        print(f"Paired. The key is in {paths.key_file}.")

    pairing = Pairing(
        rt.loop,
        rt.transport,
        url=url,
        code=code,
        identity=identity,
        hostname=name,
        os_name=host.os_name(rt.env),
        capabilities=agent.snapshot,
        tls_verify=current.tls_verify,
        fingerprint=fingerprint,
        paired=paired,
    )
    pairing.start()
    rt.loop.run_until(lambda: pairing.finished, args.timeout)
    if pairing.outcome != "paired":
        outcome = pairing.outcome or "timeout"
        detail = pairing.detail or "no answer from Home Assistant in time"
        print(f"Pairing failed: {detail}.", file=sys.stderr)
        return {"wrong_code": 5, "cancelled": 5, "timeout": 6, "unreachable": 6}.get(outcome, 1)
    if not paths.unit_file.exists():
        print("Next: torporctl install, which starts the agent.")
        return 0
    started = time.time()
    if restart_agent(paths, env, quiet=True) != 0:
        return 1
    if wait_connected(paths, rt, started):
        print("The agent is connected.")
        return 0
    print("The agent hasn't connected within 30 s; torporctl status shows why.", file=sys.stderr)
    return 6


def config_text(paths: Paths) -> str:
    return paths.config_file.read_text() if paths.config_file.exists() else config.DEFAULT_TEXT


def cmd_config(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    loaded, issues = config.load(paths.config_file)
    if args.action == "list":
        for issue in issues:
            print(f"# {issue.level}: {issue.message}")
        for key, value in config.all_values(loaded):
            print(f"{key} = {config.format_value(value)}")
        return 0
    try:
        section, key = config.split_key(args.key)
    except config.ConfigError as error:
        raise Usage(str(error)) from error
    if args.action == "get":
        print(config.format_value(config.current(loaded, section, key)))
        return 0
    if args.action == "set":
        if args.value is None:
            raise Usage("config set needs a value")
        try:
            section, key, value = config.validate(args.key, args.value.replace("\\n", "\n"))
        except config.ConfigError as error:
            raise Usage(f"{args.key}: {error}") from error
        text = config.set_value(config_text(paths), section, key, value)
    else:
        text = config.unset_value(config_text(paths), section, key)
    ensure_dir(paths.config_dir, 0o700)
    atomic_write(paths.config_file, text, 0o600)
    print(f"{'Set' if args.action == 'set' else 'Unset'} {section}.{key} in {paths.config_file}")
    if paths.unit_file.exists() and agent_active(env):
        return 0 if restart_agent(paths, env) == 0 else 1
    return 0


def pid_alive(pid: object, env: dict[str, str]) -> bool:
    proc = Path(env.get("TORPOR_PROC") or "/proc")
    return isinstance(pid, int) and pid > 0 and (proc / str(pid)).is_dir()


def status_code(installed: bool, running: bool, status: dict[str, Any] | None) -> int:
    if not installed and status is None:
        return 3
    if not running:
        return 1
    if status is None or not status.get("paired"):
        return 3
    connection = status.get("connection") or {}
    code = (connection.get("last_close") or {}).get("code")
    if connection.get("state") == "connected":
        return 0
    if code == codes.AUTH_FAILED and connection.get("state") == "rejected":
        return 5
    return 6


def when(stamp: object) -> str:
    if not isinstance(stamp, int | float):
        return "?"
    return datetime.datetime.fromtimestamp(stamp).strftime("%H:%M:%S")


def cmd_status(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    installed = paths.unit_file.exists()
    status = read_status(paths.status_file)
    alive = status is not None and pid_alive(status.get("pid"), env)
    running = agent_active(env) if installed else alive
    code = status_code(installed, running, status)
    if args.json:
        print(json.dumps({"installed": installed, "running": running, "status": status, "exit": code}, indent=2))
        return code
    if not installed and status is None:
        print("torpord is not installed (torporctl install).")
        return code
    print(f"torpord: {'running' if running else 'not running'}{'' if installed else ' (not installed)'}")
    if status is None:
        return code
    print(f"agent {status.get('agent')}, proto {status.get('proto')}, pid {status.get('pid')}")
    if status.get("observe_only"):
        print("observe-only: it reports but executes nothing")
    print(f"paired: {'yes' if status.get('paired') else 'no (torporctl pair)'}")
    print(f"Home Assistant: {status.get('ha_url') or '-'}")
    connection = status.get("connection") or {}
    line = f"connection: {connection.get('state')} since {when(connection.get('since'))}"
    retry_at = connection.get("retry_at")
    if isinstance(retry_at, int | float):
        line += f", retry in {max(0, round(retry_at - time.time()))} s"
    print(line)
    last = connection.get("last_close")
    if last:
        print(
            f"last close: {last.get('code') or last.get('http') or '-'} {meaning(last.get('code'), last.get('http'))}"
        )
    if connection.get("state") == "rejected" and (last or {}).get("code") == codes.AUTH_FAILED:
        print("re-pair needed: Reconfigure the PC in Home Assistant, then run torporctl pair")
    state = status.get("state") or {}
    if state:
        print(f"state: {state.get('state')} (session {state.get('session')}, display {state.get('display_on')})")
        if state.get("remote"):
            print(f"remote: {', '.join(state['remote'])}")
        if state.get("busy"):
            print(f"busy: {', '.join(state['busy'])}")
        keep_awake = state.get("keep_awake") or {}
        if keep_awake.get("on"):
            print(f"keep awake: on, {'applied' if keep_awake.get('applied') else 'not applied: no desktop to inhibit'}")
        if state.get("game") is not None:
            print(f"game: {'running' if state['game'] else 'none'}")
    for name, module in (status.get("modules") or {}).items():
        flags = "on" if module.get("enabled") else "off"
        flags += "" if module.get("available") else ", unavailable"
        backend = f" ({module['backend']})" if module.get("backend") else ""
        print(f"module {name}: {flags}{backend}")
        for item in module.get("problems") or []:
            print(f"  - {item.get('message')}" + (f" (fix: {item['hint']})" if item.get("hint") else ""))
    for display in status.get("displays") or []:
        print(f"display {display['id']}: {display['label']} ({display['backend']})")
        mode = display.get("mode") or {}
        if mode:
            print(
                f"  mode: {mode.get('name') or 'not read yet'}" + (f" ({mode['error']})" if mode.get("error") else "")
            )
    for lease in status.get("leases") or []:
        print(f"lease: {lease}")
    return code


def cmd_logs(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    argv = [tool(env, "TORPOR_JOURNALCTL", "journalctl"), "--user", "-u", UNIT]
    if args.follow:
        argv.append("-f")
    if args.lines is not None:
        argv += ["-n", str(args.lines)]
    try:
        return subprocess.call(argv)
    except OSError as error:
        print(f"cannot run journalctl: {error.strerror}", file=sys.stderr)
        return 1


def cmd_doctor(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    from torpor.doctor import Doctor, print_report

    def buses() -> tuple[Any, Any]:
        try:
            from torpor import glib
        except (ImportError, ValueError):
            return None, None
        return glib.buses()

    factory = DOCTOR_BUSES or buses
    report = Doctor(paths, env, buses=factory).run_all()
    if args.json:
        print(json.dumps(report.wire(), indent=2))
    else:
        print_report(report)
    return 4 if report.failed else 0


DOCTOR_BUSES: Callable[[], tuple[Any, Any]] | None = None


def cmd_hold(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    try:
        name = leases.check_name(args.name)
    except leases.LeaseError as error:
        raise Usage(str(error)) from error
    length = parse_duration(args.length) if args.length else None
    until = time.time() + length if length else None
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        path = leases.write(paths.leases_dir, name, until=until)
        how = f"until {when(until)}" if until else "for lease-ttl seconds after its last touch (touch it to renew)"
        print(f"Holding {name} {how}: {path}")
        return 0
    leases.write(paths.leases_dir, name, until=until, pid=os.getpid())
    try:
        child = subprocess.Popen(command)
        try:
            return child.wait()
        except KeyboardInterrupt:
            child.send_signal(signal.SIGINT)
            child.wait()
            return 130
    except OSError as error:
        print(f"cannot run {command[0]}: {error.strerror}", file=sys.stderr)
        return 127
    finally:
        leases.remove(paths.leases_dir, name)


def cmd_release(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    try:
        removed = leases.remove(paths.leases_dir, args.name)
    except leases.LeaseError as error:
        raise Usage(str(error)) from error
    if not removed:
        print(f"No lease named {args.name}.", file=sys.stderr)
        return 3
    print(f"Released {args.name}.")
    return 0


def cmd_restart(args: argparse.Namespace, paths: Paths, env: dict[str, str]) -> int:
    return restart_agent(paths, env)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="torporctl", description="Install, pair and inspect the Torpor agent.")
    top.add_argument("--version", action="version", version=f"torporctl {__version__} (proto {codes.PROTO})")
    sub = top.add_subparsers(dest="command", required=True, metavar="COMMAND")

    install = sub.add_parser("install", help="install torpord as a systemd user service")
    install.add_argument("--observe-only", action="store_true", help="report, but execute nothing")
    install.add_argument("--no-start", action="store_true", help="install without enabling or starting")
    install.add_argument("--no-hardening", action="store_true", help="skip the hardening probe and drop-in")
    install.set_defaults(run=cmd_install)

    uninstall = sub.add_parser("uninstall", help="stop torpord and remove its files")
    uninstall.add_argument("--purge", action="store_true", help="also remove the config, the key and the state")
    uninstall.set_defaults(run=cmd_uninstall)

    update = sub.add_parser("update", help="re-install from a newer checkout or release tarball")
    update.add_argument("source", help="a torpor checkout folder or a release .tar.gz")
    update.add_argument("--force", action="store_true", help="install even an older version")
    update.set_defaults(run=cmd_update)

    pair = sub.add_parser("pair", help="pair with Home Assistant using the code it shows")
    pair.add_argument("--url", help="Home Assistant's address, e.g. http://homeassistant.local:8123")
    pair.add_argument("--code-stdin", action="store_true", help="read the code from one line of stdin")
    pair.add_argument("--timeout", type=float, default=codes.PAIRING_TIMEOUT_S, help="seconds to wait (900)")
    pair.add_argument("stray", nargs="*", help=argparse.SUPPRESS)
    pair.set_defaults(run=cmd_pair)

    conf = sub.add_parser("config", help="read or change torpor.conf")
    conf_sub = conf.add_subparsers(dest="action", required=True, metavar="ACTION")
    conf_sub.add_parser("list", help="every key and its value")
    get = conf_sub.add_parser("get", help="one key, as SECTION.KEY")
    get.add_argument("key")
    set_ = conf_sub.add_parser("set", help="change one key and restart a running agent")
    set_.add_argument("key")
    set_.add_argument("value", nargs="?")
    unset = conf_sub.add_parser("unset", help="remove one key, back to its default")
    unset.add_argument("key")
    conf.set_defaults(run=cmd_config)

    status = sub.add_parser("status", help="the agent, its connection and the PC's state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(run=cmd_status)

    logs = sub.add_parser("logs", help="the agent's journal")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int)
    logs.set_defaults(run=cmd_logs)

    doctor = sub.add_parser("doctor", help="check dependencies and setup, print fixes")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(run=cmd_doctor)

    hold = sub.add_parser("hold", help="keep the PC busy: for a time, or while a command runs")
    hold.add_argument("name")
    hold.add_argument("--for", dest="length", metavar="DURATION", help="90, 90s, 20m, 2h, 1h30m")
    hold.add_argument("command", nargs="*", metavar="-- CMD", help="hold while CMD runs")
    hold.set_defaults(run=cmd_hold)

    release = sub.add_parser("release", help="remove a lease")
    release.add_argument("name")
    release.set_defaults(run=cmd_release)

    restart = sub.add_parser("restart", help="restart torpord")
    restart.set_defaults(run=cmd_restart)
    return top


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        paths = Paths.from_env()
        env = dict(os.environ)
        try:
            result: int = args.run(args, paths, env)
        except Usage as error:
            print(f"torporctl {args.command}: {error}", file=sys.stderr)
            return 2
        return result
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
