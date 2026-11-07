"""The fake machine behind the system and session buses: logind, PowerDevil, GNOME, the portal, GameMode and
power-profiles-daemon.

Every call is checked against the real interface's signatures, so a wrong signature fails here
instead of on a real desktop. Calls that change something are listed in `World.writes`. `changes`
and `game_edges` keep when brightness changed and when a game started or ended, on the fake clock,
for the checks on mode reads.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fakes.loop import FakeLoop
from torpor.bus import BusError, Done, SignalCallback, Target, Variant

LOGIN = "org.freedesktop.login1"
MANAGER = "org.freedesktop.login1.Manager"
SESSION = "org.freedesktop.login1.Session"
USER = "org.freedesktop.login1.User"
PROPS = "org.freedesktop.DBus.Properties"
SB = "org.kde.ScreenBrightness"
SB_PATH = "/org/kde/ScreenBrightness"
SB_DISPLAY = "org.kde.ScreenBrightness.Display"
PD = "org.freedesktop.PowerManagement"
PD_IFACE = "org.freedesktop.PowerManagement.Inhibit"
GNOME = "org.gnome.SessionManager"
PORTAL = "org.freedesktop.portal.Desktop"
SESSION_PATH = "/org/freedesktop/login1/session/_3"
GAMEMODE = "com.feralinteractive.GameMode"
GAMEMODE_PATH = "/com/feralinteractive/GameMode"
PPD = "org.freedesktop.UPower.PowerProfiles"
PPD_PATH = "/org/freedesktop/UPower/PowerProfiles"
OLD_PPD = "net.hadess.PowerProfiles"
OLD_PPD_PATH = "/net/hadess/PowerProfiles"

SIGNATURES: dict[tuple[str, str], tuple[str, str]] = {
    (MANAGER, "ListSessions"): ("", "a(susso)"),
    (MANAGER, "CanSuspend"): ("", "s"),
    (MANAGER, "CanHibernate"): ("", "s"),
    (MANAGER, "CanPowerOff"): ("", "s"),
    (MANAGER, "CanReboot"): ("", "s"),
    (MANAGER, "ListInhibitors"): ("", "a(ssssuu)"),
    (MANAGER, "Inhibit"): ("ssss", "h"),
    (MANAGER, "Suspend"): ("b", ""),
    (MANAGER, "Hibernate"): ("b", ""),
    (MANAGER, "PowerOff"): ("b", ""),
    (MANAGER, "Reboot"): ("b", ""),
    (MANAGER, "GetUser"): ("u", "o"),
    (SESSION, "Lock"): ("", ""),
    (SESSION, "SetBrightness"): ("ssu", ""),
    (PROPS, "Get"): ("ss", "v"),
    (PROPS, "GetAll"): ("s", "a{sv}"),
    ("org.freedesktop.ScreenSaver", "GetActive"): ("", "b"),
    (PD_IFACE, "Inhibit"): ("ss", "u"),
    (PD_IFACE, "UnInhibit"): ("u", ""),
    (GNOME, "Inhibit"): ("susu", "u"),
    (GNOME, "Uninhibit"): ("u", ""),
    ("org.freedesktop.portal.Inhibit", "Inhibit"): ("sua{sv}", "o"),
    ("org.freedesktop.portal.Request", "Close"): ("", ""),
    (SB_DISPLAY, "SetBrightnessWithContext"): ("ius", ""),
    ("org.freedesktop.DBus", "NameHasOwner"): ("s", "b"),
    ("org.freedesktop.DBus", "ListActivatableNames"): ("", "as"),
}
WRITES = {
    (MANAGER, "Suspend"),
    (MANAGER, "Hibernate"),
    (MANAGER, "PowerOff"),
    (MANAGER, "Reboot"),
    (SESSION, "Lock"),
    (SESSION, "SetBrightness"),
    (PD_IFACE, "Inhibit"),
    (GNOME, "Inhibit"),
    ("org.freedesktop.portal.Inhibit", "Inhibit"),
    (SB_DISPLAY, "SetBrightnessWithContext"),
}


def session_path(sid: str) -> str:
    return f"/org/freedesktop/login1/session/_{sid}"


@dataclass
class FakeSession:
    id: str
    uid: int
    user: str
    seat: str = "seat0"
    cls: str = "user"
    type: str = "wayland"
    state: str = "active"
    active: bool = True
    remote: bool = False
    service: str = "sddm"
    locked: bool = False
    desktop: str = "KDE"


@dataclass
class Call:
    bus: str
    target: Target
    method: str
    args: tuple[Any, ...]


@dataclass
class World:
    loop: FakeLoop
    uid: int = 1000
    sessions: dict[str, FakeSession] = field(default_factory=dict)
    can: dict[str, str] = field(
        default_factory=lambda: {"suspend": "yes", "hibernate": "no", "poweroff": "yes", "reboot": "yes"}
    )
    inhibitors: list[tuple[str, str, str, str, int, int]] = field(default_factory=list)
    linger: bool = True
    owners: dict[tuple[str, str], str | None] = field(default_factory=dict)
    displays: dict[str, dict[str, Any]] = field(default_factory=dict)
    screensaver: bool | None = None
    calls: list[Call] = field(default_factory=list)
    fail: dict[tuple[str, str], BusError] = field(default_factory=dict)
    subs: list[tuple[str, str, str, str | None, SignalCallback]] = field(default_factory=list)
    watchers: list[tuple[str, str, Callable[[str | None], None]]] = field(default_factory=list)
    inhibits: dict[str, dict[object, str]] = field(default_factory=lambda: {"kde": {}, "gnome": {}, "portal": {}})
    delay_pipes: list[tuple[int, int]] = field(default_factory=list)
    next_cookie: int = 1
    hold_async: bool = False
    held: list[Callable[[], None]] = field(default_factory=list)
    backlight: Callable[[str, int], None] | None = None
    activatable: dict[str, set[str]] = field(default_factory=lambda: {"system": set(), "session": set()})
    games: int = 0
    holds: list[dict[str, str]] = field(default_factory=list)
    changes: list[float] = field(default_factory=list)
    write_times: list[float] = field(default_factory=list)
    game_edges: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.owners.setdefault(("system", LOGIN), ":1.1")

    # The machine.

    def login(self, sid: str = "3", **kwargs: Any) -> FakeSession:
        kwargs.setdefault("uid", self.uid)
        kwargs.setdefault("user", "alex" if kwargs["uid"] == self.uid else "sam")
        session = FakeSession(sid, **kwargs)
        if session.seat == "seat0" and session.active:
            for other in self.sessions.values():
                if other.seat == "seat0":
                    other.active = False
        self.sessions[sid] = session
        return session

    def greeter(self, sid: str = "c1") -> FakeSession:
        return self.login(sid, uid=967, user="sddm", cls="greeter", type="wayland", service="sddm-greeter")

    def ssh(self, sid: str = "9") -> FakeSession:
        return self.login(sid, seat="", type="tty", service="sshd", remote=True, active=False, desktop="")

    def set_owner(self, bus: str, name: str, owner: str | None) -> None:
        if name == SB:
            self.changes.append(self.loop.mono)
        self.owners[(bus, name)] = owner
        for kind, watched, callback in list(self.watchers):
            if kind == bus and watched == name:
                callback(owner)

    def plasma(self, owner: str | None = ":1.50") -> None:
        for name in (PD, SB):
            self.set_owner("session", name, owner)

    def emit(self, bus: str, iface: str, member: str, path: str, args: tuple[Any, ...]) -> None:
        if iface == SB:
            self.changes.append(self.loop.mono)
        for kind, sub_iface, sub_member, sub_path, callback in list(self.subs):
            if kind == bus and sub_iface == iface and sub_member == member and sub_path in (None, path):
                callback(path, args)

    def sleep(self, starting: bool) -> None:
        self.emit("system", MANAGER, "PrepareForSleep", "/org/freedesktop/login1", (starting,))

    def shutdown(self, starting: bool, kind: str | None) -> None:
        path = "/org/freedesktop/login1"
        if kind is not None:
            self.emit("system", MANAGER, "PrepareForShutdownWithMetadata", path, (starting, {"type": kind}))
        self.emit("system", MANAGER, "PrepareForShutdown", path, (starting,))

    def slider(self, name: str, value: int, context: str = "") -> None:
        self.displays[name]["Brightness"] = value
        self.emit("session", SB, "BrightnessChanged", SB_PATH, (name, value, ":1.9", context))

    def gamemode(self, running: bool = True) -> None:
        """gamemoded installed, and started by D-Bus activation when `running`."""
        self.activatable["session"].add(GAMEMODE)
        self.set_owner("session", GAMEMODE, ":1.80" if running else None)

    def game(self, on: bool = True) -> None:
        """A game registering with GameMode, or leaving it, as gamemoded 1.8 signals it."""
        self.game_edges.append(self.loop.mono)
        self.games += 1 if on else -1
        member = "GameRegistered" if on else "GameUnregistered"
        self.emit("session", GAMEMODE, member, GAMEMODE_PATH, (4321, f"{GAMEMODE_PATH}/Games/4321"))
        self.emit("session", PROPS, "PropertiesChanged", GAMEMODE_PATH, (GAMEMODE, {"ClientCount": self.games}, []))

    def hold(self, reason: str | None, profile: str = "performance") -> None:
        """A power-profiles-daemon hold, as a launcher takes it, or none."""
        self.game_edges.append(self.loop.mono)
        self.holds = [{"Profile": profile, "Reason": reason, "ApplicationId": "launcher"}] if reason else []
        for path in (PPD_PATH, OLD_PPD_PATH):
            self.emit("system", PROPS, "PropertiesChanged", path, (PPD, {}, ["ActiveProfileHolds"]))

    def delay_held(self) -> bool:
        held = False
        for _read, write in self.delay_pipes:
            try:
                os.write(write, b"x")
            except OSError:
                continue
            held = True
        return held

    def close(self) -> None:
        for read, write in self.delay_pipes:
            for fd in (read, write):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def writes(self, method: str | None = None) -> list[Call]:
        return [c for c in self.calls if (c.target[2], c.method) in WRITES and (method is None or c.method == method)]

    def called(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    # The buses.

    def handle(self, bus: str, target: Target, method: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
        _name, path, iface = target
        self.calls.append(Call(bus, target, method, args))
        failure = self.fail.get((iface, method))
        if failure is not None:
            raise failure
        if iface == "org.freedesktop.DBus":
            if method == "ListActivatableNames":
                return (sorted(self.activatable[bus]),)
            return (self.owners.get((bus, args[0])) is not None,)
        if iface == PROPS:
            return self.properties(bus, path, method, args)
        if iface == MANAGER:
            return self.manager(method, args)
        if iface == SESSION:
            sid = path.rsplit("_", 1)[-1]
            session = self.sessions.get(sid)
            if session is None:
                raise BusError("org.freedesktop.login1.NoSuchSession", "No such session")
            if method == "Lock":
                session.locked = True
            if method == "SetBrightness" and self.backlight is not None:
                self.changes.append(self.loop.mono)
                self.write_times.append(self.loop.mono)
                self.backlight(args[1], args[2])
            return ()
        if iface == "org.freedesktop.ScreenSaver":
            if self.screensaver is None:
                raise BusError("org.freedesktop.DBus.Error.ServiceUnknown", "The name is not activatable")
            return (self.screensaver,)
        if iface == PD_IFACE:
            return self.inhibit("kde", PD, method, args, args[1] if method == "Inhibit" else "")
        if iface == GNOME:
            return self.inhibit("gnome", GNOME, method, args, args[2] if method == "Inhibit" else "")
        if iface == "org.freedesktop.portal.Inhibit":
            reason = args[2]["reason"]
            assert isinstance(reason, Variant) and reason.signature == "s", reason
            return self.inhibit("portal", PORTAL, method, args, reason.value)
        if iface == "org.freedesktop.portal.Request":
            self.inhibits["portal"].pop(path, None)
            return ()
        if iface == SB_DISPLAY:
            display = self.display(path)
            self.changes.append(self.loop.mono)
            self.write_times.append(self.loop.mono)
            value, _flags, context = args
            display["Brightness"] = value
            dname = path.rsplit("/", 1)[-1]
            self.loop.call_later(0, lambda: self.slider(dname, value, context))
            return ()
        raise AssertionError(f"unexpected call {bus} {iface}.{method} {path} {args}")

    def inhibit(self, kind: str, owner: str, method: str, args: tuple[Any, ...], reason: str) -> tuple[Any, ...]:
        if self.owners.get(("session", owner)) is None:
            raise BusError("org.freedesktop.DBus.Error.ServiceUnknown", f"{owner} is not running")
        if method in ("Inhibit",):
            cookie: object = (
                f"/org/freedesktop/portal/desktop/request/1_1/t{self.next_cookie}"
                if kind == "portal"
                else self.next_cookie
            )
            self.next_cookie += 1
            self.inhibits[kind][cookie] = reason
            return (cookie,)
        self.inhibits[kind].pop(args[0], None)
        return ()

    def manager(self, method: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
        if method == "ListSessions":
            return ([(s.id, s.uid, s.user, s.seat, session_path(s.id)) for s in self.sessions.values()],)
        if method.startswith("Can"):
            key = {
                "CanSuspend": "suspend",
                "CanHibernate": "hibernate",
                "CanPowerOff": "poweroff",
                "CanReboot": "reboot",
            }
            return (self.can[key[method]],)
        if method == "ListInhibitors":
            return (list(self.inhibitors),)
        if method == "GetUser":
            return (f"/org/freedesktop/login1/user/_{args[0]}",)
        if method in ("Suspend", "Hibernate", "PowerOff", "Reboot"):
            return ()
        if method == "Inhibit":
            return (0,)
        raise AssertionError(method)

    def properties(self, bus: str, path: str, method: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
        if method == "GetAll":
            (iface,) = args
            if iface == SESSION:
                session = self.sessions.get(path.rsplit("_", 1)[-1])
                if session is None:
                    raise BusError("org.freedesktop.DBus.Error.UnknownObject", "No such session")
                return (
                    {
                        "Class": session.cls,
                        "Type": session.type,
                        "State": session.state,
                        "Active": session.active,
                        "Remote": session.remote,
                        "Service": session.service,
                        "LockedHint": session.locked,
                        "Desktop": session.desktop,
                        "Seat": (session.seat, "/"),
                    },
                )
            if iface == SB_DISPLAY:
                return (dict(self.display(path)),)
            raise AssertionError(f"GetAll {path} {iface}")
        iface, prop = args
        if iface == USER and prop == "Linger":
            return (self.linger,)
        if iface == GAMEMODE and prop == "ClientCount":
            if self.owners.get((bus, GAMEMODE)) is None:
                raise BusError("org.freedesktop.DBus.Error.ServiceUnknown", "gamemoded is not running")
            return (self.games,)
        if iface in (PPD, OLD_PPD) and prop == "ActiveProfileHolds":
            if self.owners.get((bus, iface)) is None:
                raise BusError("org.freedesktop.DBus.Error.ServiceUnknown", f"{iface} is not running")
            return ([dict(hold) for hold in self.holds],)
        if path == SB_PATH and prop == "DisplaysDBusNames":
            if self.owners.get(("session", SB)) is None:
                raise BusError("org.freedesktop.DBus.Error.ServiceUnknown", "no PowerDevil")
            return (list(self.displays),)
        raise AssertionError(f"Get {path} {iface}.{prop}")

    def display(self, path: str) -> dict[str, Any]:
        if self.owners.get(("session", SB)) is None:
            raise BusError("org.freedesktop.DBus.Error.ServiceUnknown", "no PowerDevil")
        display = self.displays.get(path.rsplit("/", 1)[-1])
        if display is None:
            raise BusError("org.freedesktop.DBus.Error.UnknownObject", path)
        return display


class FakeBus:
    def __init__(self, world: World, kind: str) -> None:
        self.world = world
        self.kind = kind

    def check(self, target: Target, method: str, signature: str, reply: str | None) -> None:
        expected = SIGNATURES.get((target[2], method))
        assert expected is not None, f"no signature known for {target[2]}.{method}"
        assert signature == expected[0], f"{target[2]}.{method}: agent sends ({signature}), bus wants ({expected[0]})"
        if reply is not None:
            assert reply == expected[1], f"{target[2]}.{method}: agent expects ({reply}), bus replies ({expected[1]})"

    def call(
        self,
        target: Target,
        method: str,
        args: tuple[Any, ...] = (),
        *,
        signature: str = "",
        reply: str | None = None,
        timeout_ms: int = 5000,
    ) -> tuple[Any, ...]:
        self.check(target, method, signature, reply)
        return self.world.handle(self.kind, target, method, args)

    def call_async(
        self,
        target: Target,
        method: str,
        args: tuple[Any, ...] = (),
        *,
        signature: str = "",
        done: Done,
        timeout_ms: int = 25000,
    ) -> None:
        self.check(target, method, signature, None)

        def answer() -> None:
            try:
                result = self.world.handle(self.kind, target, method, args)
            except BusError as error:
                done(None, error)
                return
            done(result, None)

        if self.world.hold_async:
            self.world.held.append(answer)
        else:
            self.world.loop.call_later(0, answer)

    def call_with_fd(
        self, target: Target, method: str, args: tuple[Any, ...], *, signature: str, timeout_ms: int = 5000
    ) -> int:
        self.check(target, method, signature, "h")
        self.world.handle(self.kind, target, method, args)
        read, write = os.pipe()
        self.world.delay_pipes.append((read, write))
        return read

    def get(self, name: str, path: str, iface: str, prop: str, *, timeout_ms: int = 1500) -> Any:
        assert 0 < timeout_ms <= 1500, timeout_ms
        (value,) = self.call((name, path, PROPS), "Get", (iface, prop), signature="ss", reply="v")
        return value

    def get_all(self, name: str, path: str, iface: str, *, timeout_ms: int = 1500) -> dict[str, Any]:
        (value,) = self.call((name, path, PROPS), "GetAll", (iface,), signature="s", reply="a{sv}")
        return dict(value)

    def subscribe(self, sender: str | None, iface: str, member: str, path: str | None, callback: SignalCallback) -> int:
        self.world.subs.append((self.kind, iface, member, path, callback))
        return len(self.world.subs)

    def watch_name(self, name: str, callback: Callable[[str | None], None]) -> int:
        self.world.watchers.append((self.kind, name, callback))
        self.world.loop.call_later(0, lambda: callback(self.world.owners.get((self.kind, name))))
        return len(self.world.watchers)

    def has_owner(self, name: str) -> bool:
        (owned,) = self.call(
            ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus"),
            "NameHasOwner",
            (name,),
            signature="s",
            reply="b",
        )
        return bool(owned)
