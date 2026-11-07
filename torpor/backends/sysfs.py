"""Read-only facts from /sys and /proc. Roots come from TORPOR_SYSFS and TORPOR_PROC in tests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

LIT = "On"
ESTABLISHED = "01"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def display_lit(sysfs: Path) -> bool | None:
    """Any connected connector lit → True; all off → False; nothing readable → None."""
    states: list[bool] = []
    for connector in sorted((sysfs / "class" / "drm").glob("card*-*")):
        if read_text(connector / "status") != "connected":
            continue
        dpms = read_text(connector / "dpms")
        if dpms is None:
            continue
        states.append(dpms == LIT)
    if any(states):
        return True
    if states:
        return False
    return None


def connected_connectors(sysfs: Path) -> frozenset[str]:
    return frozenset(
        connector.name
        for connector in (sysfs / "class" / "drm").glob("card*-*")
        if read_text(connector / "status") == "connected"
    )


@dataclass(frozen=True)
class Backlight:
    name: str
    maximum: int
    value: int
    kind: str

    @property
    def internal(self) -> bool:
        return not self.name.startswith("ddcci")


def backlights(sysfs: Path) -> list[Backlight]:
    found: list[Backlight] = []
    for device in sorted((sysfs / "class" / "backlight").glob("*")):
        maximum = read_text(device / "max_brightness")
        value = read_text(device / "brightness")
        if maximum is None or value is None or not maximum.isdigit() or not value.isdigit() or int(maximum) <= 0:
            continue
        found.append(Backlight(device.name, int(maximum), int(value), read_text(device / "type") or "raw"))
    return found


def backlight_value(sysfs: Path, name: str) -> int | None:
    value = read_text(sysfs / "class" / "backlight" / name / "brightness")
    return int(value) if value is not None and value.isdigit() else None


def inbound_ports(proc: Path, ports: Iterable[int]) -> list[str]:
    """`port:<n>` for each port in `ports` with an established connection on it, from /proc/net/tcp{,6}."""
    wanted = set(ports)
    if not wanted:
        return []
    seen: Counter[int] = Counter()
    for table in ("tcp", "tcp6"):
        try:
            rows = (proc / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or fields[3] != ESTABLISHED:
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if port in wanted:
                seen[port] += 1
    return [f"port:{port}" for port in sorted(seen)]


def boot_time(proc: Path) -> int | None:
    try:
        lines = (proc / "stat").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("btime "):
            value = line.split()[1]
            return int(value) if value.isdigit() else None
    return None


def uptime(proc: Path) -> float | None:
    text = read_text(proc / "uptime")
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def dmi(sysfs: Path) -> tuple[str | None, str | None]:
    root = sysfs / "class" / "dmi" / "id"
    junk = {"", "To Be Filled By O.E.M.", "System manufacturer", "System Product Name", "Default string"}
    vendor = read_text(root / "sys_vendor")
    model = read_text(root / "product_name")
    return (vendor if vendor not in junk else None, model if model not in junk else None)


def os_release(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key] = value.strip().strip("\"'")
    return values


def i2c_dev_loaded(sysfs: Path) -> bool:
    return (sysfs / "module" / "i2c_dev").is_dir()
