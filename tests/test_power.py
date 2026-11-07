"""power: the [allow] ceiling, blockers and force, one command at a time, logind errors, hibernate (A5)."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import Env
from fakes.bus import MANAGER, SESSION
from torpor.bus import BusError

PLAYER = ("sleep", "Example Player", "Playing video", "block", 1000, 900)
UPDATER = ("shutdown", "Example Updater", "Installing updates", "block", 0, 901)


def connected(env: Env, **config: str) -> Env:
    env.pair()
    for key, value in config.items():
        env.set(key, value)
    env.desk()
    env.start()
    env.advance(2)
    return env


def error_of(env: Env, command_id: str) -> dict[str, Any]:
    result = env.result(command_id)
    assert result["ok"] is False, result
    return result["error"]


def test_suspend_calls_logind_and_answers_ok(env: Env) -> None:
    connected(env)
    result = env.result(env.ha.command("suspend", force=False))
    assert result == {"type": "result", "id": "c1", "ok": True, "data": {}}
    assert [(c.method, c.args) for c in env.world.writes()] == [("Suspend", (False,))]


@pytest.mark.parametrize("action", ["hibernate", "poweroff", "reboot"])
def test_actions_off_by_default_are_not_allowed(env: Env, action: str) -> None:
    connected(env)
    assert error_of(env, env.ha.command(action, force=False))["code"] == "not_allowed"
    assert env.world.writes() == []


def test_capabilities_carry_the_allow_ceiling(env: Env) -> None:
    connected(env, **{"allow.poweroff": "yes", "allow.lock": "no"})
    actions = env.capabilities()["modules"]["power"]["actions"]
    assert actions == {"lock": False, "suspend": True, "hibernate": False, "poweroff": True, "reboot": False}


def test_other_apps_blocking_sleep_refuse_it_and_force_skips_only_that_check(env: Env) -> None:
    env.world.inhibitors = [PLAYER, ("sleep:shutdown", "Torpor", "x", "delay", 1000, 4242)]
    connected(env)
    error = error_of(env, env.ha.command("suspend", force=False))
    assert error["code"] == "refused"
    assert error["message"] == "1 app blocks sleep"
    assert error["blockers"] == [{"who": "Example Player", "why": "Playing video", "what": "sleep"}]
    assert env.world.writes() == []
    assert env.result(env.ha.command("suspend", force=True))["ok"] is True
    assert [(c.method, c.args) for c in env.world.writes()] == [("Suspend", (False,))]
    assert not [c for c in env.world.calls if "Ignore" in c.method]


def test_torpors_own_inhibitors_are_never_blockers(env: Env) -> None:
    env.world.inhibitors = [("sleep", "Torpor", "Kept awake", "block", 1000, 4242)]
    connected(env)
    assert env.last_state()["power"]["blockers"] == []
    assert env.result(env.ha.command("suspend", force=False))["ok"] is True


def test_blockers_are_reported_in_state(env: Env) -> None:
    env.world.inhibitors = [PLAYER, UPDATER, ("idle", "Example Editor", "x", "block", 1000, 5)]
    connected(env)
    assert env.last_state()["power"]["blockers"] == [
        {"who": "Example Player", "why": "Playing video", "what": "sleep"},
        {"who": "Example Updater", "why": "Installing updates", "what": "shutdown"},
    ]


def test_shutdown_blockers_refuse_power_off(env: Env) -> None:
    env.world.inhibitors = [UPDATER]
    connected(env, **{"allow.poweroff": "yes"})
    error = error_of(env, env.ha.command("poweroff", force=False))
    assert error["code"] == "refused" and error["blockers"][0]["who"] == "Example Updater"
    assert env.result(env.ha.command("poweroff", force=True))["ok"] is True
    assert [c.method for c in env.world.writes()] == ["PowerOff"]


def test_one_power_command_at_a_time(env: Env) -> None:
    connected(env)
    env.world.hold_async = True
    first = env.ha.command("suspend", force=False)
    second = env.ha.command("suspend", force=False)
    env.loop.run_due()
    assert error_of(env, second)["code"] == "busy"
    env.world.hold_async = False
    for answer in env.world.held:
        answer()
    assert env.result(first)["ok"] is True


def test_polkit_denial_is_refused_with_a_hint(env: Env) -> None:
    connected(env)
    env.world.fail[(MANAGER, "Suspend")] = BusError(
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired", "Interactive authentication required."
    )
    error = error_of(env, env.ha.command("suspend", force=False))
    assert error["code"] == "refused"
    assert "torporctl doctor" in error["hint"]


def test_other_logind_errors_fail_with_the_message(env: Env) -> None:
    connected(env)
    env.world.fail[(MANAGER, "Suspend")] = BusError(
        "org.freedesktop.login1.SleepVerbNotSupported", "Sleep verb not supported"
    )
    error = error_of(env, env.ha.command("suspend", force=False))
    assert error["code"] == "failed" and "Sleep verb not supported" in error["message"]


def test_hibernate_needs_logind_to_say_yes_right_now(env: Env) -> None:
    env.world.can["hibernate"] = "no"
    connected(env, **{"allow.hibernate": "yes"})
    assert env.capabilities()["modules"]["power"]["actions"]["hibernate"] is True
    assert env.last_state()["power"]["can_hibernate"] == "no"
    assert error_of(env, env.ha.command("hibernate", force=False))["code"] == "unavailable"
    env.world.can["hibernate"] = "yes"
    env.advance(10)
    assert env.last_state()["power"]["can_hibernate"] == "yes"
    assert env.result(env.ha.command("hibernate", force=False))["ok"] is True
    assert [c.method for c in env.world.writes()] == ["Hibernate"]


def test_suspend_is_unavailable_where_logind_says_na(env: Env) -> None:
    env.world.can["suspend"] = "na"
    connected(env)
    assert error_of(env, env.ha.command("suspend", force=False))["code"] == "unavailable"


def test_lock_locks_each_of_the_owners_graphical_sessions(env: Env) -> None:
    env.world.login("4", seat="", active=False, type="x11", service="xrdp")
    env.world.ssh()
    connected(env)
    assert env.result(env.ha.command("lock"))["ok"] is True
    locked = sorted(c.target[1] for c in env.world.writes("Lock"))
    assert locked == ["/org/freedesktop/login1/session/_3", "/org/freedesktop/login1/session/_4"]


def test_lock_without_a_graphical_session_is_unavailable(env: Env) -> None:
    env.pair()
    env.world.greeter()
    env.start()
    env.advance(2)
    assert error_of(env, env.ha.command("lock"))["code"] == "unavailable"


def test_another_user_at_the_desk_refuses_lock_and_sleep(env: Env) -> None:
    env.pair()
    env.world.login("3", active=False)
    env.world.login("5", uid=1001)
    env.start()
    env.advance(2)
    assert error_of(env, env.ha.command("lock"))["message"] == "another user is at the desk"
    assert error_of(env, env.ha.command("suspend", force=False))["code"] == "refused"
    assert env.world.writes() == []
    assert env.result(env.ha.command("suspend", force=True))["ok"] is True


def test_a_failed_lock_reports_failed(env: Env) -> None:
    connected(env)
    env.world.fail[(SESSION, "Lock")] = BusError("org.freedesktop.DBus.Error.Failed", "no lock screen")
    error = error_of(env, env.ha.command("lock"))
    assert error["code"] == "failed" and "no lock screen" in error["message"]


def test_bad_arguments_and_unknown_commands(env: Env) -> None:
    connected(env)
    assert error_of(env, env.ha.command("suspend", force="yes"))["code"] == "bad_request"
    assert error_of(env, env.ha.command("self_destruct"))["code"] == "bad_request"


def test_a_module_off_on_the_pc_or_in_home_assistant_is_disabled(env: Env) -> None:
    connected(env, **{"modules.power": "no"})
    assert error_of(env, env.ha.command("suspend", force=False))["code"] == "disabled"
    caps = env.capabilities()["modules"]["power"]
    assert caps == {"enabled": False, "available": False, "backend": None, "problems": []}
    assert "power" not in env.last_state()


def test_home_assistant_turning_power_off_disables_its_commands(env: Env) -> None:
    connected(env)
    env.ha.push_config(modules={"power": False, "keep_awake": True, "wake": True, "brightness": True})
    env.loop.run_due()
    assert error_of(env, env.ha.command("suspend", force=False))["code"] == "disabled"
