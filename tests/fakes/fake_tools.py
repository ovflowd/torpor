"""Fake systemctl, systemd-run, loginctl, journalctl, nmcli and python for torporctl's tests.

The tests put small wrappers named after each tool in a folder and point TORPOR_SYSTEMCTL and
friends at them; each wrapper runs this script with the tool's name first. Behaviour comes from
the JSON state named by FAKE_TOOLS_STATE, and every call is appended to its "calls" list. Nothing
here touches the real systemd, logind or NetworkManager.
"""

import fcntl
import json
import os
import sys
from typing import Any


def systemctl(state: dict[str, Any], args: list[str], out: list[str]) -> int:
    if args[:1] != ["--user"]:
        out.append("fake systemctl: only --user is allowed")
        return 2
    args = args[1:]
    if not state.get("user_manager", True):
        out.append("Failed to connect to bus: No medium found")
        return 1
    verb = args[0] if args else ""
    if verb in state.get("fail", []):
        out.append(f"fake systemctl: {verb} failed")
        return 1
    if verb == "show":
        out.append("262")
        return 0
    if verb == "is-active":
        out.append(state.get("active", "inactive"))
        return 0 if state.get("active") == "active" else 3
    if verb == "is-enabled":
        out.append(state.get("enabled", "disabled"))
        return 0 if state.get("enabled") == "enabled" else 1
    if verb in ("enable", "restart", "start"):
        state["enabled" if verb == "enable" else "active"] = "enabled" if verb == "enable" else "active"
        if verb == "restart" and state.get("on_restart"):
            path, content = state["on_restart"]
            with open(path, "w") as handle:
                handle.write(content)
        return 0
    if verb == "disable":
        state["enabled"] = "disabled"
        if "--now" in args:
            state["active"] = "inactive"
        return 0
    if verb == "daemon-reload":
        return 0
    out.append(f"fake systemctl: unexpected {args}")
    return 1


def loginctl(state: dict[str, Any], args: list[str], out: list[str]) -> int:
    if args[:1] == ["show-user"]:
        out.append("yes" if state.get("linger") else "no")
        return 0
    if args[:2] == ["--no-legend", "list-sessions"]:
        return 0 if state.get("logind", True) else 1
    if args[:1] == ["enable-linger"]:
        state["linger"] = True
        return 0
    return 1


def nmcli(state: dict[str, Any], args: list[str], out: list[str]) -> int:
    nm = state.get("nm", {})
    if args[:2] == ["-g", "GENERAL.CONNECTION"] and args[2:4] == ["device", "show"]:
        connection = nm.get("devices", {}).get(args[4])
        if connection is None:
            out.append(f"Error: Device '{args[4]}' not found.")
            return 10
        out.append(connection)
        return 0
    if args[:2] == ["-g", "802-3-ethernet.wake-on-lan"] and args[2:5] == ["connection", "show", "id"]:
        value = nm.get("wol", {}).get(args[5])
        if value is None:
            out.append("Error: no such connection profile.")
            return 10
        out.append(value)
        return 0
    return 2


def python(state: dict[str, Any], args: list[str], out: list[str]) -> int:
    if "-c" in args:
        probe = state.get("probe")
        if probe is None:
            out.append("Traceback: fake python has no probe answer")
            return 1
        out.append(json.dumps(probe))
        return 0
    os.execv(sys.executable, [sys.executable, *args])
    return 1


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    path = os.environ["FAKE_TOOLS_STATE"]
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path) as handle:
            state = json.load(handle)
        state.setdefault("calls", []).append([tool, *args])
        out: list[str] = []
        with open(path + ".tmp", "w") as handle:
            json.dump(state, handle)
        os.replace(path + ".tmp", path)
        if tool == "python":
            fcntl.flock(lock, fcntl.LOCK_UN)
            code = python(state, args, out)
        elif tool == "systemd-run":
            code = 0 if state.get("probe_ok", True) else 1
            out.append("ok" if code == 0 else "self-test failed: MemoryDenyWriteExecute")
        elif tool == "journalctl":
            out.append("Nov 07 12:00:00 desk torpord[4242]: state: idle")
            code = 0
        else:
            code = {"systemctl": systemctl, "loginctl": loginctl, "nmcli": nmcli}[tool](state, args, out)
        with open(path + ".tmp", "w") as handle:
            json.dump(state, handle)
        os.replace(path + ".tmp", path)
    if out:
        print("\n".join(out))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
