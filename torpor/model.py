"""From facts about the PC to the state Home Assistant sees. Pure functions, no I/O."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

GRAPHICAL = ("wayland", "x11", "mir")
IN_USE, REMOTE, BUSY, IDLE = "in_use", "remote", "busy", "idle"


@dataclass(frozen=True)
class SessionInfo:
    id: str
    uid: int
    user: str
    seat: str
    path: str
    cls: str = "user"
    type: str = "tty"
    state: str = "online"
    active: bool = False
    remote: bool = False
    service: str = ""
    locked: bool | None = False
    desktop: str = ""

    @property
    def graphical(self) -> bool:
        return self.cls == "user" and self.type in GRAPHICAL and self.state != "closing"


@dataclass(frozen=True)
class Facts:
    sessions: tuple[SessionInfo, ...] = ()
    display_on: bool | None = None
    remote: tuple[str, ...] = ()
    busy: tuple[str, ...] = ()
    boot_time: int | None = None
    desk: SessionInfo | None = field(default=None)
    login_screen: bool = False

    @property
    def session(self) -> str:
        if self.desk is not None:
            if self.desk.locked is None:
                return "unknown"
            return "locked" if self.desk.locked else "unlocked"
        return "login_screen" if self.login_screen else "none"

    @property
    def state(self) -> str:
        return compute_state(self)


def desk_session(sessions: Iterable[SessionInfo]) -> SessionInfo | None:
    """The active graphical user session on seat0, whoever owns it."""
    for session in sessions:
        if session.seat == "seat0" and session.active and session.graphical:
            return session
    return None


def login_screen(sessions: Sequence[SessionInfo]) -> bool:
    greeter = any(s.seat == "seat0" and s.active and s.cls == "greeter" for s in sessions)
    return greeter and desk_session(sessions) is None


def owner_graphical(sessions: Iterable[SessionInfo], uid: int) -> list[SessionInfo]:
    return [s for s in sessions if s.uid == uid and s.graphical]


def another_user_at_desk(sessions: Sequence[SessionInfo], uid: int) -> bool:
    desk = desk_session(sessions)
    return desk is not None and desk.uid != uid and desk.locked is not True


def remote_sessions(sessions: Iterable[SessionInfo], *, ssh: bool) -> list[str]:
    reasons: list[str] = []
    for session in sessions:
        if session.state == "closing":
            continue
        if session.service == "sshd":
            if ssh and "ssh" not in reasons:
                reasons.append("ssh")
        elif session.remote and "session" not in reasons:
            reasons.append("session")
    return reasons


def compute_state(facts: Facts) -> str:
    """First match wins. Unknown facts lean to in_use, the state in which automations leave the PC alone."""
    desk = facts.desk
    if desk is not None and desk.locked is not True and facts.display_on is not False:
        return IN_USE
    if facts.remote:
        return REMOTE
    if facts.busy:
        return BUSY
    return IDLE


def observe(
    sessions: Sequence[SessionInfo],
    *,
    display_on: bool | None,
    remote: Sequence[str],
    busy: Sequence[str],
    boot_time: int | None,
) -> Facts:
    return Facts(
        sessions=tuple(sessions),
        display_on=display_on,
        remote=tuple(remote),
        busy=tuple(busy),
        boot_time=boot_time,
        desk=desk_session(sessions),
        login_screen=login_screen(sessions),
    )
