"""Synthetic /sys, /proc, /dev, /run and /etc trees. Every name, MAC and serial here is made up."""

from __future__ import annotations

import os
from pathlib import Path

MACHINE_ID = "0123456789abcdef0123456789abcdef"
TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


class Tree:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.sysfs = root / "sys"
        self.proc = root / "proc"
        self.dev = root / "dev"
        self.run = root / "run"
        self.etc = root / "etc"
        for folder in (
            self.sysfs / "class" / "drm",
            self.sysfs / "class" / "net",
            self.proc / "net",
            self.dev,
            self.etc,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        (self.run / "systemd" / "system").mkdir(parents=True, exist_ok=True)
        (self.proc / "sys" / "kernel" / "random").mkdir(parents=True, exist_ok=True)
        self.boot("boot-a")
        (self.proc / "stat").write_text("cpu  1 2 3 4\nbtime 1799990000\nprocesses 5\n")
        (self.proc / "uptime").write_text("10000.50 20000.00\n")
        (self.etc / "machine-id").write_text(MACHINE_ID + "\n")
        self.os_release("arch", "Arch Linux")
        self.tcp([])
        (self.proc / "net" / "route").write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        )

    def env(self) -> dict[str, str]:
        return {
            "TORPOR_SYSFS": str(self.sysfs),
            "TORPOR_PROC": str(self.proc),
            "TORPOR_DEV": str(self.dev),
            "TORPOR_RUN": str(self.run),
            "TORPOR_OS_RELEASE": str(self.etc / "os-release"),
            "TORPOR_MACHINE_ID": str(self.etc / "machine-id"),
        }

    def boot(self, boot_id: str) -> None:
        (self.proc / "sys" / "kernel" / "random" / "boot_id").write_text(boot_id + "\n")

    def os_release(self, distro: str, pretty: str, *, like: str = "", version: str = "") -> None:
        lines = [f"ID={distro}", f'PRETTY_NAME="{pretty}"']
        if like:
            lines.append(f'ID_LIKE="{like}"')
        if version:
            lines.append(f'VERSION_ID="{version}"')
        (self.etc / "os-release").write_text("\n".join(lines) + "\n")

    def connector(self, name: str = "card0-DP-2", *, status: str = "connected", dpms: str = "On") -> None:
        folder = self.sysfs / "class" / "drm" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "status").write_text(status + "\n")
        (folder / "dpms").write_text(dpms + "\n")

    def backlight(self, name: str = "intel_backlight", *, value: int = 600, maximum: int = 1200) -> None:
        folder = self.sysfs / "class" / "backlight" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "max_brightness").write_text(f"{maximum}\n")
        (folder / "brightness").write_text(f"{value}\n")
        (folder / "type").write_text("raw\n")

    def set_backlight(self, name: str, value: int) -> None:
        (self.sysfs / "class" / "backlight" / name / "brightness").write_text(f"{value}\n")

    def nic(
        self, name: str, mac: str, *, kind: str = "ethernet", wakeup: str | None = "enabled", physical: bool = True
    ) -> None:
        folder = self.sysfs / "class" / "net" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "address").write_text(mac + "\n")
        (folder / "type").write_text("1\n" if kind in ("ethernet", "wifi") else "772\n")
        if kind == "wifi":
            (folder / "wireless").mkdir(exist_ok=True)
        if physical:
            device = folder / "device"
            device.mkdir(exist_ok=True)
            if wakeup is not None:
                (device / "power").mkdir(exist_ok=True)
                (device / "power" / "wakeup").write_text(wakeup + "\n")

    def route(self, interface: str, destination: str, mask: str) -> None:
        def le(address: str) -> str:
            return bytes(int(part) for part in reversed(address.split("."))).hex().upper()

        path = self.proc / "net" / "route"
        path.write_text(
            path.read_text() + f"{interface}\t{le(destination)}\t00000000\t0001\t0\t0\t100\t{le(mask)}\t0\t0\t0\n"
        )

    def tcp(self, established_ports: list[int], *, listening: list[int] | None = None) -> None:
        rows = []
        for index, port in enumerate(listening or []):
            rows.append(
                f"  {index}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 1\n"
            )
        for index, port in enumerate(established_ports):
            peer = f"0D01010A:C{index:03X}"
            rows.append(
                f"  {index + 10}: 2C01010A:{port:04X} {peer} 01 00000000:00000000 00:00000000 00000000 1000 0 2\n"
            )
        (self.proc / "net" / "tcp").write_text(TCP_HEADER + "".join(rows))
        (self.proc / "net" / "tcp6").write_text(TCP_HEADER)

    def process(self, pid: int, *argv: str) -> None:
        folder = self.proc / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")

    def kill(self, pid: int) -> None:
        folder = self.proc / str(pid)
        for child in folder.iterdir():
            child.unlink()
        folder.rmdir()

    def i2c(self, count: int = 2, *, module: bool = True, access: bool = True) -> None:
        if module:
            (self.sysfs / "module" / "i2c_dev").mkdir(parents=True, exist_ok=True)
        for index in range(count):
            path = self.dev / f"i2c-{index}"
            if path.exists():
                os.chmod(path, 0o600)
            path.write_text("")
            os.chmod(path, 0o600 if access else 0o000)
