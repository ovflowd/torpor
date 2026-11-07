"""keep_awake: backend order and fallthrough, none available, expiry, restarts and reboots, late backends (A6)."""

from __future__ import annotations

from conftest import Env
from fakes.bus import GNOME, PD, PD_IFACE, PORTAL
from torpor import leases
from torpor.bus import BusError


def connected(env: Env, *, desktop: str | None = "kde", **config: str) -> Env:
    env.pair()
    for key, value in config.items():
        env.set(key, value)
    env.world.login("3")
    if desktop == "kde":
        env.world.plasma()
    elif desktop == "gnome":
        env.world.set_owner("session", GNOME, ":1.60")
    elif desktop == "portal":
        env.world.set_owner("session", PORTAL, ":1.70")
    env.start()
    env.advance(2)
    return env


def keep_awake(env: Env, *, on: bool = True, length: float | None = None) -> dict:  # type: ignore[type-arg]
    return env.result(env.ha.command("keep_awake", on=on, **{"for": length}))


def test_kde_comes_first_and_holds_one_inhibit(env: Env) -> None:
    connected(env)
    env.world.set_owner("session", PORTAL, ":1.70")
    assert keep_awake(env)["data"] == {"remaining": None}
    env.loop.run_due()
    assert list(env.world.inhibits["kde"].values()) == ["Kept awake from Home Assistant"]
    assert env.world.inhibits["portal"] == {}
    state = env.last_state()
    assert state["keep_awake"] == {"on": True, "remaining": None, "backend": "kde", "applied": True}
    assert state["busy"] == ["keep_awake"] and state["state"] == "in_use"


def test_gnome_and_the_portal_when_plasma_is_not_there(env: Env) -> None:
    connected(env, desktop="gnome")
    keep_awake(env)
    assert list(env.world.inhibits["gnome"].values()) == ["Kept awake from Home Assistant"]
    gnome_call = next(c for c in env.world.calls if c.method == "Inhibit" and c.target[2] == GNOME)
    assert gnome_call.args == ("torpor", 0, "Kept awake from Home Assistant", 12)


def test_the_portal_is_last(env: Env) -> None:
    connected(env, desktop="portal")
    keep_awake(env)
    assert list(env.world.inhibits["portal"].values()) == ["Kept awake from Home Assistant"]
    keep_awake(env, on=False)
    assert env.world.inhibits["portal"] == {}
    assert env.world.called("Close")


def test_a_backend_that_fails_its_first_call_is_reported_and_the_next_is_tried(env: Env) -> None:
    env.world.set_owner("session", GNOME, ":1.60")
    env.world.fail[(PD_IFACE, "Inhibit")] = BusError("org.freedesktop.DBus.Error.Failed", "broken PowerDevil")
    connected(env)
    keep_awake(env)
    env.advance(1)
    assert list(env.world.inhibits["gnome"].values()) == ["Kept awake from Home Assistant"]
    caps = env.capabilities()["modules"]["keep_awake"]
    assert caps["available"] is True and caps["backend"] == "gnome"
    assert caps["problems"][0]["code"] == "kde_failed"


def test_no_backend_means_unavailable_with_a_problem(env: Env) -> None:
    connected(env, desktop=None)
    caps = env.capabilities()["modules"]["keep_awake"]
    assert caps["available"] is False and caps["backend"] is None
    assert caps["problems"][-1]["code"] == "no_backend"


def test_a_request_with_no_desktop_waits_for_one(env: Env) -> None:
    connected(env, desktop=None)
    keep_awake(env)
    assert env.last_state()["keep_awake"]["applied"] is False
    env.world.plasma()
    env.advance(1)
    assert list(env.world.inhibits["kde"].values()) == ["Kept awake from Home Assistant"]
    assert env.last_state()["keep_awake"]["applied"] is True


def test_a_backend_that_worked_once_stays_available(env: Env) -> None:
    connected(env)
    keep_awake(env)
    env.world.plasma(None)
    env.advance(11)
    caps = env.capabilities()["modules"]["keep_awake"]
    assert caps["available"] is True
    assert env.last_state()["keep_awake"]["applied"] is False


def test_powerdevil_restarting_gets_a_new_inhibit(env: Env) -> None:
    connected(env)
    keep_awake(env)
    env.world.inhibits["kde"].clear()
    env.world.plasma(":1.99")
    env.advance(1)
    assert list(env.world.inhibits["kde"].values()) == ["Kept awake from Home Assistant"]


def test_the_switch_ends_on_time(env: Env) -> None:
    connected(env)
    assert keep_awake(env, length=600)["data"] == {"remaining": 600}
    env.advance(300)
    assert env.agent is not None
    assert env.agent.state()["keep_awake"]["remaining"] == 300  # type: ignore[index]
    assert env.last_state()["keep_awake"]["remaining"] == 600
    env.advance(301)
    state = env.last_state()["keep_awake"]
    assert state == {"on": False, "remaining": None, "backend": None, "applied": False}
    assert env.world.inhibits["kde"] == {}


def test_it_survives_an_agent_restart_but_not_a_reboot(env: Env) -> None:
    connected(env)
    keep_awake(env, length=3600)
    env.advance(100)
    env.restart()
    env.advance(2)
    assert env.last_state()["keep_awake"]["on"] is True
    assert env.last_state()["keep_awake"]["remaining"] == 3500
    env.tree.boot("boot-b")
    env.restart()
    env.advance(2)
    assert env.last_state()["keep_awake"]["on"] is False


def test_turning_it_off_releases_the_inhibit(env: Env) -> None:
    connected(env)
    keep_awake(env)
    keep_awake(env, on=False)
    assert env.world.inhibits["kde"] == {}


def test_bad_arguments(env: Env) -> None:
    connected(env)
    assert keep_awake(env, length=-5)["error"]["code"] == "bad_request"
    result = env.result(env.ha.command("keep_awake", on="yes"))
    assert result["error"]["code"] == "bad_request"


def test_busy_sources_take_the_same_inhibit_with_their_own_reason(env: Env) -> None:
    connected(env)
    leases.write(env.paths.leases_dir, "render", until=env.loop.time() + 120)
    env.advance(11)
    assert list(env.world.inhibits["kde"].values()) == ["Busy: lease:render"]
    keep_awake(env)
    assert list(env.world.inhibits["kde"].values()) == ["Kept awake from Home Assistant"]
    keep_awake(env, on=False)
    env.advance(11)
    assert list(env.world.inhibits["kde"].values()) == ["Busy: lease:render"]
    env.advance(120)
    assert env.world.inhibits["kde"] == {}


def test_the_backend_setting_picks_one(env: Env) -> None:
    env.world.set_owner("session", PORTAL, ":1.70")
    connected(env, **{"keep-awake.backend": "portal"})
    keep_awake(env)
    assert env.world.inhibits["kde"] == {} and env.world.inhibits["portal"]


def test_home_assistant_turning_the_module_off_drops_the_switch(env: Env) -> None:
    connected(env)
    keep_awake(env)
    env.ha.push_config(modules={"power": True, "keep_awake": False, "wake": True, "brightness": True})
    env.loop.run_due()
    assert env.world.inhibits["kde"] == {}
    assert keep_awake(env)["error"]["code"] == "disabled"


def test_not_allowed_by_the_ceiling(env: Env) -> None:
    connected(env, **{"allow.keep-awake": "no"})
    assert env.capabilities()["modules"]["keep_awake"]["actions"] == {"keep_awake": False}
    assert keep_awake(env)["error"]["code"] == "not_allowed"
    assert env.world.writes() == []


def test_powerdevil_inhibits_use_the_freedesktop_interface(env: Env) -> None:
    connected(env)
    keep_awake(env)
    call = next(c for c in env.world.calls if c.method == "Inhibit" and c.target[0] == PD)
    assert call.target == (PD, "/org/freedesktop/PowerManagement/Inhibit", PD_IFACE)
    assert call.args == ("Torpor", "Kept awake from Home Assistant")


def test_a_ceiling_lowered_across_a_restart_drops_the_switch(env: Env) -> None:
    connected(env)
    keep_awake(env)
    env.set("allow.keep-awake", "no")
    env.restart()
    env.advance(2)
    assert env.world.inhibits["kde"] == {}
    assert env.last_state()["keep_awake"]["on"] is False
    assert "keep_awake" not in env.last_state()["busy"]
    assert env.agent is not None and env.agent.ctx.boot.get("keep_awake") is None


def test_turning_it_off_is_accepted_whatever_the_ceiling(env: Env) -> None:
    connected(env, **{"allow.keep-awake": "no"})
    assert keep_awake(env, on=False)["ok"] is True
    assert keep_awake(env)["error"]["code"] == "not_allowed"


def test_a_restart_while_it_is_on_holds_one_inhibit_and_gives_it_back(env: Env) -> None:
    connected(env)
    keep_awake(env)
    env.restart()
    env.advance(2)
    assert list(env.world.inhibits["kde"].values()) == ["Kept awake from Home Assistant"]
    keep_awake(env, on=False)
    assert env.world.inhibits["kde"] == {}


def test_a_lease_at_start_holds_one_inhibit_and_gives_it_back(env: Env) -> None:
    env.pair()
    env.world.login("3")
    env.world.plasma()
    leases.write(env.paths.leases_dir, "backup", until=env.loop.time() + 60)
    env.start()
    env.advance(2)
    assert list(env.world.inhibits["kde"].values()) == ["Busy: lease:backup"]
    env.advance(120)
    assert env.world.inhibits["kde"] == {}


def test_time_asleep_counts_toward_the_end(env: Env) -> None:
    connected(env)
    keep_awake(env, length=3600)
    env.advance(1800)
    env.world.sleep(True)
    env.loop.run_due()
    env.loop.wall += 10 * 3600
    env.world.sleep(False)
    env.loop.run_due()
    assert env.agent is not None
    assert env.agent.state()["keep_awake"]["on"] is False  # type: ignore[index]
    assert env.world.inhibits["kde"] == {}


def test_a_wall_clock_step_back_does_not_stretch_it(env: Env) -> None:
    connected(env)
    keep_awake(env, length=600)
    env.loop.wall -= 3600
    env.advance(601)
    assert env.world.inhibits["kde"] == {}
