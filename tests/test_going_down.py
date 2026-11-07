"""Going down: the delay inhibitor, going_down kinds, the 2 s ack, 1001, and coming back (A4)."""

from __future__ import annotations

import pytest

from conftest import Env


def connected(env: Env, *, observe_only: bool = False) -> Env:
    env.pair()
    env.desk()
    env.start(observe_only=observe_only)
    env.advance(2)
    assert env.ha.current is not None
    return env


@pytest.mark.parametrize("observe_only", [False, True])
def test_the_agent_holds_a_delay_inhibitor_on_sleep_and_shutdown(env: Env, observe_only: bool) -> None:
    connected(env, observe_only=observe_only)
    inhibit = env.world.called("Inhibit")
    assert [c.args for c in inhibit] == [
        ("sleep:shutdown", "Torpor", "Tell Home Assistant the PC is going down", "delay")
    ]
    assert env.world.delay_held()


def test_sleep_sends_going_down_and_releases_on_the_ack(env: Env) -> None:
    connected(env)
    env.world.sleep(True)
    env.loop.run_due()
    down = env.ha.received("going_down")
    assert down == [{"type": "going_down", "id": "g1", "kind": "sleep"}]
    assert env.ha.closes[-1] == ("agent", 1001, "going down")
    assert not env.world.delay_held()


def test_without_an_ack_the_agent_lets_go_after_2_s(env: Env) -> None:
    connected(env)
    env.ha.auto_ack = False
    env.world.sleep(True)
    env.loop.run_due()
    assert env.world.delay_held()
    env.advance(1.5, step=0.5)
    assert env.world.delay_held()
    env.advance(1, step=0.5)
    assert not env.world.delay_held()
    assert env.ha.closes[-1] == ("agent", 1001, "going down")


def test_a_sleep_home_assistant_asked_for_carries_its_detail(env: Env) -> None:
    connected(env)
    env.world.can["hibernate"] = "yes"
    env.set("allow.hibernate", "yes")
    assert env.result(env.ha.command("suspend", force=False))["ok"] is True
    env.world.sleep(True)
    env.loop.run_due()
    assert env.ha.received("going_down")[-1]["detail"] == "suspend"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("reboot", "reboot"),
        ("kexec", "reboot"),
        ("soft-reboot", "reboot"),
        ("poweroff", "poweroff"),
        ("halt", "poweroff"),
    ],
)
def test_shutdown_metadata_maps_to_a_kind(env: Env, kind: str, expected: str) -> None:
    connected(env)
    env.world.shutdown(True, kind)
    env.advance(1, step=0.1)
    assert [m["kind"] for m in env.ha.received("going_down")] == [expected]


def test_a_shutdown_without_metadata_is_unknown_after_a_short_grace(env: Env) -> None:
    connected(env)
    env.world.shutdown(True, None)
    env.loop.run_due()
    assert env.ha.received("going_down") == []
    env.advance(0.5, step=0.1)
    assert [m["kind"] for m in env.ha.received("going_down")] == ["unknown"]


def test_going_down_while_disconnected_releases_at_once(env: Env) -> None:
    connected(env)
    env.ha.reachable = False
    env.ha.drop()
    env.loop.run_due()
    env.world.sleep(True)
    env.loop.run_due()
    assert not env.world.delay_held()


def test_resume_retakes_the_inhibitor_dials_at_once_and_sends_resumed(env: Env) -> None:
    connected(env)
    env.world.sleep(True)
    env.loop.run_due()
    connections = len(env.ha.connections)
    env.advance(30)
    assert len(env.ha.connections) == connections, "dialed while asleep"
    env.world.sleep(False)
    env.loop.run_due()
    env.advance(0.5, step=0.1)
    assert len(env.ha.connections) == connections + 1
    assert env.world.delay_held()
    assert len(env.world.called("Inhibit")) == 2
    types = [m["type"] for m in env.ha.connections[-1].received]
    assert types[:3] == ["capabilities", "state", "event"]
    assert env.ha.connections[-1].received[2] == {"type": "event", "name": "resumed"}


def test_a_cancelled_shutdown_comes_back_without_resumed(env: Env) -> None:
    connected(env)
    env.world.shutdown(True, "poweroff")
    env.loop.run_due()
    env.world.shutdown(False, "poweroff")
    env.advance(1, step=0.1)
    assert env.world.delay_held()
    assert env.ha.current is not None
    assert env.ha.received("event") == [env.ha.received("event")[0]]


def test_started_is_sent_once_after_the_first_connection(env: Env) -> None:
    connected(env)
    events = env.ha.received("event")
    assert events == [{"type": "event", "name": "started", "boot_time": 1799990000, "uptime": 10000.5}]
    env.ha.drop()
    env.advance(5)
    assert len(env.ha.received("event")) == 1


def test_sigterm_closes_with_1001_and_releases_everything(env: Env) -> None:
    connected(env)
    assert env.result(env.ha.command("keep_awake", on=True, **{"for": None}))["ok"]
    assert env.world.inhibits["kde"]
    assert env.agent is not None
    env.agent.stop()
    env.loop.run_due()
    assert env.ha.closes[-1][:2] == ("agent", 1001)
    assert env.world.inhibits["kde"] == {}
    assert not env.world.delay_held()
