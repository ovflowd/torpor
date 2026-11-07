"""Where a running game shows: Feral GameMode's clients, and power-profiles-daemon's performance holds.

Both are read with NO_AUTO_START, so looking never starts gamemoded or power-profiles-daemon. Each
counts as installed while it runs or while its bus can start it, so a restart doesn't make it go away.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from torpor.bus import PROPERTIES, Bus

GAMEMODE = ("com.feralinteractive.GameMode", "/com/feralinteractive/GameMode", "com.feralinteractive.GameMode")
PROFILES = (
    (
        "org.freedesktop.UPower.PowerProfiles",
        "/org/freedesktop/UPower/PowerProfiles",
        "org.freedesktop.UPower.PowerProfiles",
    ),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles"),
)
DBUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")


def activatable(bus: Bus) -> list[str]:
    """The names this bus can start. Raises BusError when it can't say."""
    (names,) = bus.call(DBUS, "ListActivatableNames", reply="as", timeout_ms=1500)
    return list(names)


class GameMode:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def installed(self) -> bool:
        """Running, or activatable on this bus: gamemoded starts with the first game."""
        return self.bus.has_owner(GAMEMODE[0]) or GAMEMODE[0] in activatable(self.bus)

    def clients(self) -> int:
        """Games registered with gamemoded; 0 while it isn't running."""
        if not self.bus.has_owner(GAMEMODE[0]):
            return 0
        return int(self.bus.get(GAMEMODE[0], GAMEMODE[1], GAMEMODE[2], "ClientCount"))

    def watch(self, callback: Callable[[], None]) -> None:
        self.bus.watch_name(GAMEMODE[0], lambda _owner: callback())
        for member in ("GameRegistered", "GameUnregistered"):
            self.bus.subscribe(GAMEMODE[0], GAMEMODE[2], member, GAMEMODE[1], lambda _path, _args: callback())
        self.bus.subscribe(GAMEMODE[0], PROPERTIES, "PropertiesChanged", GAMEMODE[1], lambda _path, _args: callback())


class PowerProfiles:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def target(self) -> tuple[str, str, str] | None:
        """power-profiles-daemon 0.20 and later own both names; older ones only net.hadess."""
        return next((target for target in PROFILES if self.bus.has_owner(target[0])), None)

    def installed(self) -> bool:
        """Running, or activatable on the system bus, as power-profiles-daemon's own service files make it."""
        return self.target() is not None or any(target[0] in activatable(self.bus) for target in PROFILES)

    def performance_held(self, pattern: str) -> bool:
        target = self.target()
        if target is None:
            return False
        holds = self.bus.get(target[0], target[1], target[2], "ActiveProfileHolds")
        return any(
            hold.get("Profile") == "performance" and re.search(pattern, str(hold.get("Reason", "")))
            for hold in holds
            if isinstance(hold, dict)
        )

    def watch(self, callback: Callable[[], None]) -> None:
        for name, path, _iface in PROFILES:
            self.bus.watch_name(name, lambda _owner: callback())
            self.bus.subscribe(name, PROPERTIES, "PropertiesChanged", path, lambda _path, _args: callback())
