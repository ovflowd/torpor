"""A fake ddcutil for Torpor's tests, chosen through TORPOR_DDCUTIL.

It replays monitors from the JSON state file named by FAKE_DDCUTIL_STATE: their bus, EDID ids,
brightness and other VCP values ("vcp": {"e1": 1}), the ddcutil version whose output format to print
(1.4.1 has no DRM connector line, 2.x and 3.x do, and refuses the options mode reads use, as the real
1.4.1 does), and scenario counters for failures and hangs. Every call is appended to the state's
"calls" list, with its wall-clock time in "times"; every setvcp to "writes". It never touches
/dev/i2c-*.
"""

import fcntl
import json
import os
import sys
import time
from typing import Any

OPTIONS = (
    "--noconfig",
    "--disable-udf",
    "--enable-cross-instance-locks",
    "--disable-dynamic-sleep",
    "--skip-ddc-checks",
)
NEW_OPTIONS = frozenset({"--noconfig", "--enable-cross-instance-locks", "--disable-dynamic-sleep", "--skip-ddc-checks"})


def countdown(state: dict[str, Any], key: str) -> bool:
    left = state.setdefault("fail", {}).get(key, 0)
    if left == 0:
        return False
    if left > 0:
        state["fail"][key] = left - 1
    return True


def find(state: dict[str, Any], bus: str | None) -> dict[str, Any] | None:
    return next((m for m in state["monitors"] if str(m["bus"]) == str(bus)), None)


def raw_value(code: int, value: int, maximum: int) -> list[str]:
    """getvcp --verbose as ddcutil 3.0.2 prints it for a manufacturer code."""
    return [
        "VCP (aka MCCS) version for display is undetected or less than 2.0. Interpretation may not be accurate.",
        "",
        f"Getting data for non-table VCP code 0x{code:02x} - Manufacturer Specific:",
        f"Raw value: opcode=0x{code:02x}, mh=0x{maximum >> 8:02x}, ml=0x{maximum & 0xFF:02x}, sh=0x{value >> 8:02x}, "
        f"sl=0x{value & 0xFF:02x}, max_val={maximum} (0x{maximum:04x}), cur_val={value} (0x{value:04x})",
    ]


def run(state: dict[str, Any], argv: list[str], out: list[str]) -> int:
    version = state.get("version", "3.0.2")
    if argv == ["--version"]:
        out.append(f"ddcutil {version}")
        return 0
    while argv and argv[0] in OPTIONS:
        if version.startswith("1.") and argv[0] in NEW_OPTIONS:
            out.append(f"ddcutil option parsing failed: Unknown option {argv[0]}")
            return 1
        argv = argv[1:]
    bus = None
    if "--bus" in argv:
        bus = argv[argv.index("--bus") + 1]
        argv = [a for i, a in enumerate(argv) if a != "--bus" and argv[i - 1] != "--bus"]
    command = argv[0] if argv else ""
    if state.get("hang", {}).get(command):
        time.sleep(state["hang"][command])
    if countdown(state, command):
        out.append(f"fake ddcutil: {command} failed (I/O error)")
        return 1
    if command == "detect":
        if "--terse" not in argv:
            return 2
        for number, monitor in enumerate(state["monitors"], 1):
            out.append(f"Display {number}")
            out.append(f"   I2C bus:             /dev/i2c-{monitor['bus']}")
            if not version.startswith("1.") and monitor.get("connector"):
                out.append(f"   DRM connector:       {monitor['connector']}")
            out.append(f"   Monitor:             {monitor['mfg']}:{monitor['model']}:{monitor.get('serial', '')}")
            out.append("")
        for invalid in state.get("invalid", []):
            out.append("Invalid display")
            out.append(f"   I2C bus:             /dev/i2c-{invalid}")
            out.append("   DDC communication failed")
            out.append("")
        return 0
    monitor = find(state, bus)
    if monitor is None:
        out.append(f"No monitor detected on bus {bus}")
        return 1
    if command == "getvcp" and argv[1:3] == ["10", "--terse"]:
        out.append(f"VCP 10 C {monitor['brightness']} {monitor.get('max', 100)}")
        return 0
    if command == "getvcp" and len(argv) == 3 and argv[1] == "--verbose":
        code = int(argv[2], 16)
        value = monitor.get("vcp", {}).get(f"{code:02x}")
        if value is None:
            out.append(f"VCP code 0x{code:02x} (Manufacturer Specific ): Maximum retries exceeded")
            return 1
        out.extend(raw_value(code, value, monitor.get("vcp_max", {}).get(f"{code:02x}", 1)))
        return 0
    if command == "setvcp" and len(argv) == 3 and argv[1] == "10":
        value = int(argv[2])
        state["writes"].append([monitor["bus"], value])
        monitor["brightness"] = value
        return 0
    out.append(f"fake ddcutil: unexpected arguments {argv}")
    state.setdefault("violations", []).append(argv)
    return 2


def main(argv: list[str]) -> int:
    path = os.environ["FAKE_DDCUTIL_STATE"]
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path) as handle:
            state = json.load(handle)
        state.setdefault("calls", []).append(argv)
        state.setdefault("times", []).append(time.time())
        state.setdefault("writes", [])
        out: list[str] = []
        code = run(state, argv, out)
        with open(path + ".tmp", "w") as handle:
            json.dump(state, handle)
        os.replace(path + ".tmp", path)
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
