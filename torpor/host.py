"""Who this PC is: its agent_id and the `host` part of capabilities."""

from __future__ import annotations

import os
import platform
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torpor import __version__
from torpor.backends import sysfs
from torpor.codes import MAX_HOSTNAME, MAX_OS

MACHINE_ID = "/etc/machine-id"
OS_RELEASE = ("/etc/os-release", "/usr/lib/os-release")


def agent_id(env: Mapping[str, str]) -> str:
    from torpor import protocol

    path = Path(env.get("TORPOR_MACHINE_ID") or MACHINE_ID)
    return protocol.agent_id_from_machine_id(path.read_text())


def os_release(env: Mapping[str, str]) -> dict[str, str]:
    override = env.get("TORPOR_OS_RELEASE")
    for candidate in (override,) if override else OS_RELEASE:
        values = sysfs.os_release(Path(candidate))
        if values:
            return values
    return {}


def hostname() -> str:
    name = socket.gethostname().strip() or "linux-pc"
    return name[:MAX_HOSTNAME]


def os_name(env: Mapping[str, str]) -> str:
    release = os_release(env)
    name = release.get("PRETTY_NAME") or release.get("NAME") or "Linux"
    return "".join(c for c in name if c.isprintable())[:MAX_OS]


def host(env: Mapping[str, str], sysfs_root: Path, *, desktop: str | None, linger: bool | None) -> dict[str, Any]:
    vendor, model = sysfs.dmi(sysfs_root)
    return {
        "hostname": hostname(),
        "os": os_name(env),
        "kernel": os.uname().release,
        "agent": __version__,
        "python": platform.python_version(),
        "vendor": vendor,
        "model": model,
        "desktop": desktop or None,
        "linger": linger,
    }
