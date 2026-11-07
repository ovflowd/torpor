"""From facts to state: the table, unknowns leaning to in_use, other users (A3)."""

from __future__ import annotations

import pytest

from conftest import Env
from torpor.model import SessionInfo, compute_state, observe, remote_sessions

ME = 1000


def session(sid: str = "3", **kwargs: object) -> SessionInfo:
    values: dict[str, object] = {
        "id": sid,
        "uid": ME,
        "user": "alex",
        "seat": "seat0",
        "path": f"/s/{sid}",
        "cls": "user",
        "type": "wayland",
        "state": "active",
        "active": True,
    }
    values.update(kwargs)
    return SessionInfo(**values)  # type: ignore[arg-type]


def state(sessions: list[SessionInfo], *, display: bool | None = True, remote=(), busy=()) -> str:  # type: ignore[no-untyped-def]
    return compute_state(observe(sessions, display_on=display, remote=remote, busy=busy, boot_time=None))


@pytest.mark.parametrize(
    ("locked", "display", "expected"),
    [
        (False, True, "in_use"),
        (None, True, "in_use"),
        (False, None, "in_use"),
        (None, None, "in_use"),
        (True, True, "idle"),
        (False, False, "idle"),
    ],
)
def test_the_desk_is_in_use_unless_it_is_known_locked_or_dark(
    locked: bool | None, display: bool | None, expected: str
) -> None:
    assert state([session(locked=locked)], display=display) == expected


def test_first_match_wins_in_use_then_remote_then_busy() -> None:
    assert state([session()], remote=["ssh"], busy=["keep_awake"]) == "in_use"
    assert state([session(locked=True)], remote=["ssh"], busy=["keep_awake"]) == "remote"
    assert state([session(locked=True)], busy=["lease:build"]) == "busy"
    assert state([]) == "idle"


def test_another_users_desk_session_counts() -> None:
    assert state([session(uid=1001, user="sam")]) == "in_use"


@pytest.mark.parametrize(
    "other",
    [
        {"cls": "greeter", "type": "wayland"},
        {"type": "tty"},
        {"seat": "", "active": False},
        {"active": False},
        {"state": "closing"},
    ],
)
def test_only_an_active_graphical_user_session_on_seat0_is_the_desk(other: dict[str, object]) -> None:
    assert state([session(**other)]) == "idle"


def test_the_session_field() -> None:
    facts = observe([session(locked=True)], display_on=True, remote=[], busy=[], boot_time=None)
    assert facts.session == "locked"
    greeter = session("c1", uid=967, cls="greeter")
    facts = observe([greeter], display_on=True, remote=[], busy=[], boot_time=None)
    assert facts.session == "login_screen"
    assert observe([], display_on=None, remote=[], busy=[], boot_time=None).session == "none"
    unknown = observe([session(locked=None)], display_on=True, remote=[], busy=[], boot_time=None)
    assert unknown.session == "unknown"


def test_remote_sessions_name_ssh_and_other_remote_logins() -> None:
    ssh = session("9", seat="", type="tty", service="sshd", remote=True, active=False)
    xrdp = session("10", seat="", type="x11", service="xrdp-sesman", remote=True, active=False)
    closing = session("11", seat="", type="tty", service="sshd", state="closing")
    assert remote_sessions([ssh, xrdp, closing], ssh=True) == ["ssh", "session"]
    assert remote_sessions([ssh], ssh=False) == []


def test_the_agent_reports_ssh_and_inbound_ports_as_remote(env: Env) -> None:
    env.pair()
    env.set("core.remote-ports", "3389 5900")
    env.world.greeter()
    env.world.ssh()
    env.tree.tcp([3389, 3389], listening=[3389, 22])
    env.start()
    env.advance(2)
    last = env.last_state()
    assert last["state"] == "remote"
    assert last["remote"] == ["ssh", "port:3389"]
    assert last["session"] == "login_screen"


def test_the_lock_screen_counts_as_locked_without_lockedhint(env: Env) -> None:
    env.pair()
    env.desk()
    env.world.screensaver = True
    env.start()
    env.advance(2)
    assert env.last_state()["state"] == "idle"
    assert env.last_state()["session"] == "locked"
    env.world.screensaver = False
    env.advance(11)
    assert env.last_state()["state"] == "in_use"


def test_a_screensaver_that_does_not_answer_leaves_lockedhint_alone(env: Env) -> None:
    env.pair()
    env.desk()
    env.start()
    env.advance(2)
    assert env.last_state()["state"] == "in_use"


def test_unreadable_logind_keeps_the_last_state(env: Env) -> None:
    from torpor.bus import BusError

    env.pair()
    env.desk()
    env.start()
    env.advance(2)
    env.world.fail[("org.freedesktop.login1.Manager", "ListSessions")] = BusError("x.Failed", "timeout")
    env.world.sessions.clear()
    env.advance(30)
    assert env.last_state()["state"] == "in_use"
