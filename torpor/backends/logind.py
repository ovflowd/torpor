"""systemd-logind on the system bus: sessions, power actions, inhibitors, backlight writes, linger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from torpor.bus import Bus, BusError, Done
from torpor.model import SessionInfo

NAME = "org.freedesktop.login1"
MANAGER = (NAME, "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
SESSION = "org.freedesktop.login1.Session"
USER = "org.freedesktop.login1.User"
WHO = "Torpor"
CALLS = {"suspend": "Suspend", "hibernate": "Hibernate", "poweroff": "PowerOff", "reboot": "Reboot"}
CAN = {"suspend": "CanSuspend", "hibernate": "CanHibernate", "poweroff": "CanPowerOff", "reboot": "CanReboot"}


@dataclass(frozen=True)
class Inhibitor:
    what: str
    who: str
    why: str
    mode: str
    uid: int
    pid: int

    def wire(self) -> dict[str, str]:
        return {"who": self.who, "why": self.why, "what": self.what}


def _session(sid: str, uid: int, user: str, seat: str, path: str, props: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        id=sid,
        uid=uid,
        user=user,
        seat=seat,
        path=path,
        cls=str(props.get("Class", "")),
        type=str(props.get("Type", "")),
        state=str(props.get("State", "")),
        active=bool(props.get("Active", False)),
        remote=bool(props.get("Remote", False)),
        service=str(props.get("Service", "")),
        locked=bool(props.get("LockedHint", False)),
        desktop=str(props.get("Desktop", "")),
    )


class Logind:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def present(self) -> bool:
        return self.bus.has_owner(NAME)

    def sessions(self) -> list[SessionInfo]:
        (rows,) = self.bus.call(MANAGER, "ListSessions", reply="a(susso)")
        found: list[SessionInfo] = []
        for sid, uid, user, seat, path in rows:
            try:
                props = self.bus.get_all(NAME, path, SESSION)
            except BusError:
                continue
            found.append(_session(sid, uid, user, seat, path, props))
        return found

    def can(self, action: str) -> str:
        (answer,) = self.bus.call(MANAGER, CAN[action], reply="s")
        return str(answer)

    def inhibitors(self) -> list[Inhibitor]:
        (rows,) = self.bus.call(MANAGER, "ListInhibitors", reply="a(ssssuu)")
        return [Inhibitor(*row) for row in rows]

    def take_delay(self, why: str) -> int:
        return self.bus.call_with_fd(MANAGER, "Inhibit", ("sleep:shutdown", WHO, why, "delay"), signature="ssss")

    def act(self, action: str, done: Done) -> None:
        self.bus.call_async(MANAGER, CALLS[action], (False,), signature="b", done=done)

    def lock(self, path: str, done: Done) -> None:
        self.bus.call_async((NAME, path, SESSION), "Lock", done=done)

    def set_brightness(self, path: str, device: str, value: int, done: Done) -> None:
        self.bus.call_async(
            (NAME, path, SESSION), "SetBrightness", ("backlight", device, value), signature="ssu", done=done
        )

    def linger(self, uid: int) -> bool | None:
        try:
            (path,) = self.bus.call(MANAGER, "GetUser", (uid,), signature="u", reply="o")
            return bool(self.bus.get(NAME, path, USER, "Linger"))
        except BusError:
            return None

    def on_sleep(self, callback: Callable[[bool], None]) -> None:
        self.bus.subscribe(NAME, MANAGER[2], "PrepareForSleep", MANAGER[1], lambda _path, args: callback(args[0]))

    def on_shutdown(self, callback: Callable[[bool, dict[str, Any] | None], None]) -> None:
        self.bus.subscribe(
            NAME, MANAGER[2], "PrepareForShutdown", MANAGER[1], lambda _path, args: callback(args[0], None)
        )
        self.bus.subscribe(
            NAME,
            MANAGER[2],
            "PrepareForShutdownWithMetadata",
            MANAGER[1],
            lambda _path, args: callback(args[0], dict(args[1])),
        )
