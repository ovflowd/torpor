"""Observe-only: every command answered observe_only, nothing written anywhere, the target still computed (A14)."""

from __future__ import annotations

import pytest

from conftest import Env
from torpor import leases
from torpor.modules import brightness as b

KDE_ID = b.display_id("kde", "Example 27Q")
DDC_ID = b.display_id("ddcutil", "EXA:Example 24:SYN0001")


def observing(env: Env, *, flag: bool) -> Env:
    env.pair()
    env.set("allow.poweroff", "yes")
    env.set("allow.reboot", "yes")
    env.set("allow.hibernate", "yes")
    if not flag:
        env.set("agent.observe-only", "yes")
    env.world.can["hibernate"] = "yes"
    env.world.displays["display0"] = {
        "Label": "Example 27Q",
        "Brightness": 5000,
        "MaxBrightness": 10000,
        "IsInternal": False,
    }
    env.desk()
    env.ha.config = {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "brightness": {"lux": True, "curve": {}, "auto": {KDE_ID: True}},
    }
    env.ha.lux = (100.0, 3600.0)
    env.start(observe_only=flag)
    env.advance(2)
    return env


@pytest.mark.parametrize("flag", [True, False], ids=["--observe-only", "observe-only = yes"])
def test_every_command_is_answered_observe_only(env: Env, flag: bool) -> None:
    observing(env, flag=flag)
    assert env.capabilities()["observe_only"] is True
    commands = [
        ("lock", {}),
        ("suspend", {"force": False}),
        ("hibernate", {"force": True}),
        ("poweroff", {"force": False}),
        ("reboot", {"force": False}),
        ("keep_awake", {"on": True, "for": None}),
        ("brightness_set", {"display": KDE_ID, "percent": 10}),
    ]
    for name, args in commands:
        result = env.result(env.ha.command(name, **args))
        assert result["ok"] is False and result["error"]["code"] == "observe_only", (name, result)
    assert env.world.writes() == []


def test_nothing_is_written_but_the_target_is_still_reported(env: Env) -> None:
    observing(env, flag=True)
    leases.write(env.paths.leases_dir, "build", until=env.loop.time() + 3600)
    env.advance(b.DESK_SETTLE_S + b.GAP_DDC_S)
    assert env.world.writes() == []
    assert env.world.inhibits == {"kde": {}, "gnome": {}, "portal": {}}
    state = env.last_state()
    assert state["brightness"]["displays"][KDE_ID]["target"] == 40
    assert state["brightness"]["displays"][KDE_ID]["percent"] == 50
    assert state["busy"] == ["lease:build"]


def test_the_delay_inhibitor_and_going_down_still_work(env: Env) -> None:
    observing(env, flag=True)
    assert env.world.delay_held()
    env.world.sleep(True)
    env.loop.run_due()
    assert [m["kind"] for m in env.ha.received("going_down")] == ["sleep"]
    assert not env.world.delay_held()


def test_ddcutil_never_gets_a_setvcp(env: Env) -> None:
    env.tools.set_ddc(monitors=[{"bus": 4, "mfg": "EXA", "model": "Example 24", "serial": "SYN0001", "brightness": 80}])
    env.pair()
    env.world.login("3")
    env.tree.connector("card1-DP-2")
    env.ha.config = {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "brightness": {"lux": True, "curve": {}, "auto": {DDC_ID: True}},
    }
    env.ha.lux = (100.0, 3600.0)
    env.start(observe_only=True)
    env.advance(b.DESK_SETTLE_S + b.GAP_DDC_S)
    result = env.result(env.ha.command("brightness_set", display=DDC_ID, percent=10))
    assert result["error"]["code"] == "observe_only"
    calls = env.tools.ddc()["calls"]
    assert calls and not [c for c in calls if "setvcp" in c]
    assert env.last_state()["brightness"]["displays"][DDC_ID]["target"] == 40
