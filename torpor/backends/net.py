"""Network interfaces for Wake-on-LAN: MACs, kinds, whether WoL is armed, and which one reaches HA."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from torpor.backends.sysfs import read_text
from torpor.runtime import Result

ETHER = "1"


@dataclass(frozen=True)
class Nic:
    name: str
    mac: str
    kind: str
    wol: str
    primary: bool = False

    def wire(self) -> dict[str, object]:
        return {"name": self.name, "mac": self.mac, "kind": self.kind, "wol": self.wol, "primary": self.primary}


def physical(sysfs: Path) -> list[tuple[str, str, str]]:
    """(name, mac, kind) of every interface with a device behind it and a real MAC."""
    found: list[tuple[str, str, str]] = []
    for interface in sorted((sysfs / "class" / "net").glob("*")):
        if interface.name == "lo" or not (interface / "device").exists():
            continue
        mac = (read_text(interface / "address") or "").lower()
        if len(mac) != 17 or mac == "00:00:00:00:00:00":
            continue
        if (interface / "wireless").exists() or (interface / "phy80211").exists():
            kind = "wifi"
        elif read_text(interface / "type") == ETHER:
            kind = "ethernet"
        else:
            kind = "other"
        found.append((interface.name, mac, kind))
    return found


def wakeup(sysfs: Path, name: str) -> str | None:
    return read_text(sysfs / "class" / "net" / name / "device" / "power" / "wakeup")


def nm_wol(nmcli: str, name: str, run: Callable[[list[str]], Result]) -> str | None:
    """NetworkManager's 802-3-ethernet.wake-on-lan for the device's active connection, or None."""
    device = run([nmcli, "-g", "GENERAL.CONNECTION", "device", "show", name])
    connection = device.output.strip() if device.ok else ""
    if not connection or connection == "--":
        return None
    setting = run([nmcli, "-g", "802-3-ethernet.wake-on-lan", "connection", "show", "id", connection])
    return setting.output.strip() if setting.ok else None


def wol_status(wakeup_value: str | None, nm_value: str | None) -> str:
    """`off` only on a positive read; `armed` needs NetworkManager's magic and a wakeup that isn't disabled."""
    if wakeup_value == "disabled":
        return "off"
    if nm_value is not None and "magic" in re.split(r"[^a-z]+", nm_value.lower()):
        return "armed"
    return "unknown"


def routes(proc: Path) -> list[tuple[str, int, int]]:
    """(interface, destination, mask) from /proc/net/route, as host-order integers."""
    rows: list[tuple[str, int, int]] = []
    try:
        lines = (proc / "net" / "route").read_text().splitlines()[1:]
    except OSError:
        return rows
    for line in lines:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            destination = int.from_bytes(bytes.fromhex(fields[1]), "little")
            mask = int.from_bytes(bytes.fromhex(fields[7]), "little")
        except ValueError:
            continue
        rows.append((fields[0], destination, mask))
    return rows


def route_interface(proc: Path, address: str | None) -> str | None:
    """The interface whose route covers `address` (longest prefix), else the default route's."""
    table = routes(proc)
    target = None
    if address:
        try:
            ip = ipaddress.ip_address(address)
            target = int(ip) if ip.version == 4 else None
        except ValueError:
            target = None
    best: tuple[int, str] | None = None
    for interface, destination, mask in table:
        if target is not None and target & mask != destination:
            continue
        if target is None and mask != 0:
            continue
        prefix = bin(mask).count("1")
        if best is None or prefix > best[0]:
            best = (prefix, interface)
    return best[1] if best else None


def resolve_ha(url: str) -> str | None:
    """HA's IPv4 address, for the route lookup. Runs in the worker thread: it may hit DNS."""
    host = urlsplit(url).hostname
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    return str(infos[0][4][0]) if infos else None


def scan(
    sysfs: Path, proc: Path, *, nmcli: str, run: Callable[[list[str]], Result], ha_address: str | None
) -> list[Nic]:
    primary = route_interface(proc, ha_address)
    nics: list[Nic] = []
    for name, mac, kind in physical(sysfs):
        status = wol_status(wakeup(sysfs, name), nm_wol(nmcli, name, run))
        nics.append(Nic(name, mac, kind, status, name == primary))
    return nics
