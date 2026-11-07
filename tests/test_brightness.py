"""brightness: the curve, every gate, hand changes per backend, failures, ddcutil, backlights (A9).

The KDE tests port the single-PC agent's brightness tests with the curve it had then (84 lx -> 31 %, slope
20, 15 to 60 %), so their numbers carry over: 30 lx is 22 %, 41 lx 25 %, 60 lx 28 %, 72 lx 30 %.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import Env
from fakes.bus import SB, SB_DISPLAY, SB_PATH
from torpor.backends import ddcutil as ddc
from torpor.bus import BusError
from torpor.curve import Curve
from torpor.known import Known
from torpor.modules import brightness as b

CURVE = {"anchor_lux": 84, "anchor_pct": 31, "slope": 20, "floor": 15, "ceiling": 60}
LABEL = "Example 27Q"
KDE_ID = b.display_id("kde", LABEL)
SETTLE = b.DESK_SETTLE_S
LUX_SETTLE = b.LUX_SETTLE_S
GAP = b.GAP_DDC_S


def config(*ids: str, auto: bool = True) -> dict[str, Any]:
    return {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "brightness": {"lux": True, "curve": CURVE, "auto": {i: auto for i in ids}},
    }


def kde_display(
    env: Env, name: str = "display0", label: str = LABEL, value: int = 3100, internal: bool = False
) -> None:
    env.world.displays[name] = {"Label": label, "Brightness": value, "MaxBrightness": 10000, "IsInternal": internal}


def at_desk(env: Env, brightness: int = 3100, *, settle: bool = True) -> Env:
    env.pair()
    kde_display(env, value=brightness)
    env.desk()
    env.ha.config = config(KDE_ID)
    env.ha.lux = (84.0, 3600.0)
    env.start()
    if settle:
        env.advance(SETTLE + 15)
    return env


def writes(env: Env) -> list[int]:
    return [c.args[0] for c in env.world.writes("SetBrightnessWithContext")]


def lux(env: Env, value: float | None, age: float = 0.0) -> None:
    env.ha.input(value, age)
    env.loop.run_due()


def kde_calls(env: Env) -> list[Any]:
    return [c for c in env.world.calls if c.target[1].startswith(SB_PATH)]


def manual(env: Env, did: str = KDE_ID) -> Any:
    assert env.agent is not None
    return env.agent.brightness.displays[did].manual


def lock(env: Env, locked: bool = True) -> None:
    env.world.sessions["3"].locked = locked


def test_the_default_curve() -> None:
    curve = Curve()
    assert [curve.target(lux) for lux in (100, 1000, 10, 1, 0, 100000)] == [40, 65, 15, 10, 10, 80]


def test_the_ported_curve_around_its_anchor() -> None:
    curve = Curve.from_config(CURVE)
    expected = {84: 31, 30: 22, 41: 25, 1: 15, 0: 15, 294: 42, 5000: 60, 60: 28, 72: 30}
    assert {lux: curve.target(lux) for lux in expected} == expected


def test_a_curve_from_home_assistant_keeps_defaults_for_bad_values() -> None:
    curve = Curve.from_config({"anchor_lux": 0, "slope": True, "floor": 50, "ceiling": 20, "anchor_pct": 35})
    assert curve == Curve(anchor_pct=35)
    assert Curve.from_config(None) == Curve()


def test_display_ids_are_stable_and_per_backend() -> None:
    assert b.display_id("kde", LABEL) == b.display_id("kde", LABEL)
    assert len(KDE_ID) == 12 and int(KDE_ID, 16) >= 0
    assert b.display_id("ddcutil", LABEL) != KDE_ID


def test_it_writes_once_at_the_desk_after_the_settle_and_stays(env: Env) -> None:
    at_desk(env, 5000, settle=False)
    env.advance(SETTLE - 1)
    assert writes(env) == []
    env.advance(11)
    call = env.world.writes("SetBrightnessWithContext")[0]
    assert (call.target, call.args) == ((SB, f"{SB_PATH}/display0", SB_DISPLAY), (3100, 1, "torpor"))
    env.advance(30 * 60)
    assert writes(env) == [3100]
    state = env.last_state()["brightness"]
    assert state == {
        "lux": 84.0,
        "displays": {KDE_ID: {"percent": 31, "auto": True, "overridden": False, "target": 31, "error": None}},
    }


def test_it_waits_for_the_lux_reading_to_settle(env: Env) -> None:
    at_desk(env)
    lux(env, 26.0)
    env.advance(15)
    lux(env, 1.0)
    env.advance(15)
    lux(env, 84.0)
    env.advance(2 * 60)
    assert writes(env) == [], "wrote for a lamp switched off and on"
    lux(env, 26.0)
    env.advance(99)
    lux(env, 84.0)
    env.advance(LUX_SETTLE + GAP)
    assert writes(env) == [], "wrote for a level that held 99 s"
    lux(env, 30.0)
    env.advance(LUX_SETTLE - 5)
    assert writes(env) == []
    env.advance(15)
    assert writes(env) == [2200]


def test_small_moves_are_left_alone_and_writes_are_5_minutes_apart(env: Env) -> None:
    at_desk(env)
    lux(env, 72.0)
    env.advance(2 * 60 + 15)
    assert writes(env) == [], "wrote a 1-point move"
    lux(env, 41.0)
    env.advance(LUX_SETTLE + 15)
    assert writes(env) == [2500]
    written_at = env.loop.monotonic()
    lux(env, 84.0)
    env.advance(GAP - 30)
    assert writes(env) == [2500], "wrote inside the gap"
    env.advance(40)
    assert writes(env) == [2500, 3100]
    assert env.loop.monotonic() - written_at >= GAP


@pytest.mark.parametrize("away", ["locked", "lock screen", "display off", "logged out"])
def test_away_from_the_desk_nothing_is_written(env: Env, away: str) -> None:
    at_desk(env, settle=False)
    env.advance(1)
    if away == "locked":
        lock(env)
    elif away == "lock screen":
        env.world.screensaver = True
    elif away == "display off":
        env.tree.connector("card0-DP-2", dpms="Off")
    else:
        del env.world.sessions["3"]
        env.world.greeter()
    env.advance(11)
    lux(env, 30.0)
    env.advance(3 * 60)
    assert writes(env) == []
    if away == "logged out":
        return
    lock(env, False)
    env.world.screensaver = False
    env.tree.connector("card0-DP-2", dpms="On")
    env.advance(SETTLE - 5)
    assert writes(env) == [], "wrote inside the desk settle"
    env.advance(20)
    assert writes(env) == [2200]


def test_without_powerdevil_nothing_is_called_and_once_it_is_up_one_write(env: Env) -> None:
    env.pair()
    kde_display(env, value=5000)
    env.world.login("3")
    env.tree.connector()
    env.ha.config = config(KDE_ID)
    env.ha.lux = (84.0, 3600.0)
    env.start()
    env.advance(2 * 60)
    assert kde_calls(env) == []
    env.world.plasma()
    env.advance(SETTLE - 5)
    assert writes(env) == []
    env.advance(15)
    assert writes(env) == [3100]


def test_a_hand_change_pauses_it_until_the_room_changes_enough(env: Env) -> None:
    at_desk(env)
    env.world.slider("display0", 5000)
    env.loop.run_due()
    assert manual(env) == b.PENDING
    lux(env, 60.0)
    env.advance(LUX_SETTLE + GAP + 15)
    assert writes(env) == [], "overrode a hand-set brightness"
    assert manual(env) == 28
    assert env.last_state()["brightness"]["displays"][KDE_ID]["overridden"] is True
    lux(env, 30.0)
    env.advance(LUX_SETTLE + 15)
    assert writes(env) == [2200]
    assert manual(env) is None


def test_the_baseline_is_the_situation_once_the_reading_settled(env: Env) -> None:
    at_desk(env)
    lux(env, 41.0)
    env.advance(10)
    env.world.slider("display0", 2000)
    env.advance(LUX_SETTLE + GAP)
    assert writes(env) == [] and manual(env) == 25


def test_a_slider_move_right_after_an_unlock_is_a_hand_change(env: Env) -> None:
    at_desk(env)
    lock(env)
    env.advance(15)
    lux(env, 30.0)
    env.advance(3600)
    lock(env, False)
    assert env.agent is not None and env.agent.facts is not None
    assert env.agent.facts.state == "idle"
    looks = len(env.world.called("ListSessions"))
    for value in (3600, 3800, 4000):
        env.world.slider("display0", value)
    assert len(env.world.called("ListSessions")) - looks == 1, "more than one look for one drag"
    env.advance(SETTLE + GAP)
    assert writes(env) == [] and manual(env) == 22


def test_changes_while_locked_or_at_the_login_screen_are_not_hand_changes(env: Env) -> None:
    at_desk(env, 5000)
    assert writes(env) == [3100]
    env.world.slider("display0", 3100)
    env.world.slider("display0", 3150)
    assert manual(env) is None, "an echo of the agent's own write paused it"
    lock(env)
    env.advance(11)
    env.world.slider("display0", 6000)
    assert manual(env) is None
    assert env.agent is not None
    assert env.agent.brightness.displays[KDE_ID].raw == 6000


def test_an_override_survives_an_agent_restart_but_not_a_reboot(env: Env) -> None:
    at_desk(env)
    env.world.slider("display0", 5000)
    env.advance(20)
    assert manual(env) == 31
    env.restart()
    assert manual(env) == 31
    env.advance(SETTLE + GAP)
    assert writes(env) == []
    env.world.slider("display0", 6000)
    env.loop.run_due()
    env.restart()
    assert manual(env) == b.PENDING
    env.advance(SETTLE + GAP)
    assert writes(env) == [] and manual(env) == 31
    env.tree.boot("boot-b")
    env.restart()
    env.advance(SETTLE + 15)
    assert manual(env) is None and writes(env) == [3100]


def test_no_reading_or_no_home_assistant_keeps_what_the_monitor_has(env: Env) -> None:
    at_desk(env)
    lux(env, None)
    env.advance(2 * 60)
    assert writes(env) == []
    lux(env, 30.0)
    env.advance(20)
    env.ha.reachable = False
    env.ha.drop()
    env.advance(LUX_SETTLE + 30)
    assert writes(env) == [], "wrote while Home Assistant was disconnected"
    env.ha.reachable = True
    env.ha.lux = (30.0, 200.0)
    env.advance(70)
    assert writes(env) == [2200]
    env.advance(GAP)
    env.ha.lux = (84.0, 0.0)
    env.ha.drop()
    env.advance(LUX_SETTLE - 10)
    assert writes(env) == [2200], "wrote before the reconnect's reading settled"
    env.advance(30)
    assert writes(env) == [2200, 3100]


def test_a_failed_write_stops_it_until_powerdevil_restarts_or_the_pc_resumes(env: Env) -> None:
    def attempts() -> int:
        return len(env.world.called("SetBrightnessWithContext"))

    failure = (SB_DISPLAY, "SetBrightnessWithContext")
    at_desk(env)
    env.world.fail[failure] = BusError("org.freedesktop.DBus.Error.Failed", "I/O error")
    lux(env, 30.0)
    env.advance(LUX_SETTLE + 15)
    assert attempts() == 1
    assert env.last_state()["brightness"]["displays"][KDE_ID]["error"] == "I/O error"
    del env.world.fail[failure]
    env.advance(3 * GAP)
    assert attempts() == 1, "retried after a failed write"
    env.world.plasma(":1.300")
    env.advance(SETTLE + 15)
    assert attempts() == 2 and writes(env)[-1] == 2200
    env.world.fail[failure] = BusError("org.freedesktop.DBus.Error.Failed", "I/O error")
    lux(env, 84.0)
    env.advance(LUX_SETTLE + GAP)
    assert attempts() == 3
    del env.world.fail[failure]
    env.advance(3 * GAP)
    assert attempts() == 3
    env.world.sleep(True)
    env.loop.run_due()
    env.world.sleep(False)
    env.advance(SETTLE + 15)
    assert attempts() == 4 and writes(env)[-1] == 3100


def test_the_display_is_found_by_label_whatever_plasma_calls_it(env: Env) -> None:
    at_desk(env)
    kde_display(env, "display2", label="Virtual output", value=10000)
    env.world.emit("session", SB, "DisplayAdded", SB_PATH, ("display2",))
    env.world.displays["display1"] = env.world.displays.pop("display0")
    env.world.emit("session", SB, "DisplayRemoved", SB_PATH, ("display0",))
    env.world.emit("session", SB, "DisplayAdded", SB_PATH, ("display1",))
    lux(env, 30.0)
    env.advance(LUX_SETTLE + SETTLE)
    targets = [c.target[1].rsplit("/", 1)[-1] for c in env.world.writes("SetBrightnessWithContext")]
    assert targets == ["display1"]
    assert env.world.displays["display2"]["Brightness"] == 10000
    displays = {d["label"] for d in env.capabilities()["modules"]["brightness"]["displays"]}
    assert displays == {LABEL, "Virtual output"}


def test_a_renumbered_display_keeps_its_id_and_a_slider_move_on_it_counts(env: Env) -> None:
    at_desk(env)
    env.world.displays["display1"] = env.world.displays.pop("display0")
    env.world.slider("display1", 5000)
    env.advance(60)
    assert manual(env) == 31
    lux(env, 60.0)
    env.advance(LUX_SETTLE + 2 * GAP)
    assert writes(env) == []
    lux(env, 30.0)
    env.advance(LUX_SETTLE + 15)
    targets = [(c.target[1].rsplit("/", 1)[-1], c.args[0]) for c in env.world.writes("SetBrightnessWithContext")]
    assert targets == [("display1", 2200)]


def test_resume_ends_an_override_and_restarts_the_settle(env: Env) -> None:
    at_desk(env)
    env.world.slider("display0", 5000)
    env.loop.run_due()
    env.world.sleep(True)
    env.loop.run_due()
    env.world.sleep(False)
    env.loop.run_due()
    assert manual(env) is None
    assert json.loads(env.paths.boot_state_file.read_text()).get("brightness") is None
    env.advance(SETTLE - 5)
    assert writes(env) == []
    env.advance(15)
    assert writes(env) == [3100]


def test_auto_is_off_until_home_assistant_turns_it_on(env: Env) -> None:
    env.pair()
    kde_display(env, value=5000)
    env.desk()
    env.ha.config = config()
    env.ha.lux = (84.0, 3600.0)
    env.start()
    env.advance(SETTLE + 60)
    assert writes(env) == []
    assert env.last_state()["brightness"]["displays"][KDE_ID]["auto"] is False
    assert env.last_state()["brightness"]["displays"][KDE_ID]["target"] == 31


def test_turning_auto_on_clears_a_hand_override(env: Env) -> None:
    at_desk(env)
    env.world.slider("display0", 5000)
    env.advance(20)
    assert manual(env) == 31
    env.ha.push_config(**config(KDE_ID, auto=False))
    env.loop.run_due()
    env.ha.push_config(**config(KDE_ID, auto=True))
    env.advance(SETTLE + 15)
    assert manual(env) is None
    assert writes(env) == [3100]


def test_brightness_set_writes_and_counts_as_a_hand_change(env: Env) -> None:
    at_desk(env)
    result = env.result(env.ha.command("brightness_set", display=KDE_ID, percent=55))
    assert result["ok"] is True and result["data"] == {"percent": 55}
    assert writes(env) == [5500]
    assert manual(env) == b.PENDING
    env.advance(20)
    assert manual(env) == 31


def test_brightness_set_refusals(env: Env) -> None:
    at_desk(env)
    unknown = env.result(env.ha.command("brightness_set", display="000000000000", percent=50))
    assert unknown["error"]["code"] == "unavailable"
    bad = env.result(env.ha.command("brightness_set", display=KDE_ID, percent=150))
    assert bad["error"]["code"] == "bad_request"
    env.set("allow.brightness", "no")
    env.restart()
    env.advance(2)
    assert env.result(env.ha.command("brightness_set", display=KDE_ID, percent=50))["error"]["code"] == "not_allowed"


def test_the_ceiling_stops_automatic_brightness_too(env: Env) -> None:
    env.pair()
    env.set("allow.brightness", "no")
    kde_display(env, value=5000)
    env.desk()
    env.ha.config = config(KDE_ID)
    env.ha.lux = (84.0, 3600.0)
    env.start()
    env.advance(SETTLE + GAP)
    assert env.capabilities()["modules"]["brightness"]["actions"] == {"brightness": False}
    assert writes(env) == []


def test_equal_labels_get_a_number(env: Env) -> None:
    env.pair()
    kde_display(env, "display0")
    kde_display(env, "display1")
    env.desk()
    env.start()
    env.advance(2)
    displays = env.capabilities()["modules"]["brightness"]["displays"]
    assert sorted(d["label"] for d in displays) == [f"{LABEL} #1", f"{LABEL} #2"]
    assert {d["id"] for d in displays} == {b.display_id("kde", f"{LABEL}#1"), b.display_id("kde", f"{LABEL}#2")}


def test_a_display_can_be_left_out(env: Env) -> None:
    env.pair()
    env.set(f"display:{KDE_ID}.backend", "none")
    kde_display(env)
    env.desk()
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["brightness"]["displays"] == []


def test_known_displays_stay_listed_for_30_days(env: Env) -> None:
    at_desk(env)
    env.world.displays.clear()
    env.world.emit("session", SB, "DisplayRemoved", SB_PATH, ("display0",))
    env.advance(11)
    assert [d["id"] for d in env.capabilities()["modules"]["brightness"]["displays"]] == [KDE_ID]
    assert KDE_ID not in env.last_state()["brightness"]["displays"]
    env.loop.wall += 31 * 86400
    env.advance(11)
    env.agent.changed()  # type: ignore[union-attr]
    env.loop.run_due()
    assert env.capabilities()["modules"]["brightness"]["displays"] == []


# Backlights through logind.

BACKLIGHT_ID = b.display_id("logind", "intel_backlight")


def laptop(env: Env, value: int = 600) -> Env:
    env.pair()
    env.tree.backlight("intel_backlight", value=value, maximum=1200)
    env.world.login("3")
    env.tree.connector("card0-eDP-1")
    env.ha.config = config(BACKLIGHT_ID)
    env.ha.lux = (84.0, 3600.0)
    env.start()
    env.advance(SETTLE + 15)
    return env


def backlight_writes(env: Env) -> list[tuple[str, tuple[Any, ...]]]:
    return [(c.target[1], c.args) for c in env.world.writes("SetBrightness")]


def test_a_backlight_is_set_through_the_owners_logind_session(env: Env) -> None:
    laptop(env, 900)
    assert backlight_writes(env) == [("/org/freedesktop/login1/session/_3", ("backlight", "intel_backlight", 372))]
    caps = env.capabilities()["modules"]["brightness"]["displays"]
    assert caps == [
        {"id": BACKLIGHT_ID, "label": "intel_backlight", "backend": "logind", "internal": True, "modes": False}
    ]


def test_backlights_may_be_written_every_minute(env: Env) -> None:
    laptop(env, 372)
    env.tree.set_backlight("intel_backlight", 372)
    lux(env, 30.0)
    env.advance(LUX_SETTLE + 15)
    lux(env, 84.0)
    env.advance(LUX_SETTLE + 5)
    assert [args[2] for _, args in backlight_writes(env)] == [264, 372]


def test_a_backlight_change_read_from_sysfs_is_a_hand_change(env: Env) -> None:
    laptop(env, 372)
    env.tree.set_backlight("intel_backlight", 1000)
    env.advance(11)
    assert manual(env, BACKLIGHT_ID) is not None
    lux(env, 60.0)
    env.advance(LUX_SETTLE + 60)
    assert backlight_writes(env) == []


# ddcutil.

DDC_ID = b.display_id("ddcutil", "EXA:Example 24:SYN0001")
DDC14 = """Display 1
   I2C bus:             /dev/i2c-4
   Monitor:             EXA:Example 24:SYN0001

Invalid display
   I2C bus:             /dev/i2c-6
   DDC communication failed

"""
DDC30 = """Display 1
   I2C bus:          /dev/i2c-4
   DRM connector:    card1-DP-2
   drm_connector_id: 458
   Monitor:          EXA:Example 24:SYN0001

Display 2
   I2C bus:          /dev/i2c-7
   DRM connector:    card1-HDMI-A-1
   Monitor:          EXB:Office Screen:
"""


def test_detect_output_from_ddcutil_1_4_and_3_0() -> None:
    old = ddc.parse_detect(DDC14)
    assert old == [ddc.DdcDisplay(4, "EXA", "Example 24", "SYN0001")]
    new = ddc.parse_detect(DDC30)
    assert [(d.bus, d.input, d.label, d.connector) for d in new] == [
        (4, "EXA:Example 24:SYN0001", "Example 24", "card1-DP-2"),
        (7, "i2c-7", "Office Screen", "card1-HDMI-A-1"),
    ]


def test_getvcp_output_terse_and_verbose() -> None:
    assert ddc.parse_brightness("VCP 10 C 50 100\n") == (50, 100)
    verbose = "VCP code 0x10 (Brightness                    ): current value =    35, max value =   100\n"
    assert ddc.parse_brightness(verbose) == (35, 100)
    assert ddc.parse_brightness("VCP 10 ERR\n") is None


def monitor_env(env: Env, *, version: str = "3.0.2", brightness: int = 50, **ddc_state: Any) -> Env:
    env.tools.set_ddc(
        version=version,
        monitors=[
            {
                "bus": 4,
                "mfg": "EXA",
                "model": "Example 24",
                "serial": "SYN0001",
                "connector": "card1-DP-2",
                "brightness": brightness,
            }
        ],
        **ddc_state,
    )
    env.pair()
    env.world.login("3")
    env.tree.connector("card1-DP-2")
    env.ha.config = config(DDC_ID)
    env.ha.lux = (84.0, 3600.0)
    return env


def ddc_writes(env: Env) -> list[list[int]]:
    return env.tools.ddc()["writes"]


@pytest.mark.parametrize("version", ["1.4.1", "3.0.2"])
def test_ddcutil_displays_are_found_and_set(env: Env, version: str) -> None:
    monitor_env(env, version=version)
    env.start()
    env.advance(SETTLE + 15)
    assert ddc_writes(env) == [[4, 31]]
    calls = env.tools.ddc()["calls"]
    assert ["detect", "--terse"] in calls
    assert ["--bus", "4", "getvcp", "10", "--terse"] in calls
    assert calls[-1] == ["--bus", "4", "setvcp", "10", "31"]
    caps = env.capabilities()["modules"]["brightness"]["displays"]
    assert caps == [{"id": DDC_ID, "label": "Example 24", "backend": "ddcutil", "internal": False, "modes": True}]


def test_a_monitor_changed_by_hand_is_seen_before_the_next_write(env: Env) -> None:
    monitor_env(env, brightness=31)
    env.start()
    env.advance(SETTLE + 15)
    assert ddc_writes(env) == []
    state = env.tools.ddc()
    state["monitors"][0]["brightness"] = 70
    env.tools.set_ddc(**state)
    lux(env, 30.0)
    env.advance(LUX_SETTLE + 15)
    assert ddc_writes(env) == []
    assert manual(env, DDC_ID) is not None


def test_a_failing_monitor_is_retried_once_then_stops_until_a_redetect(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ddc, "RETRY_AFTER_S", 0)
    monitor_env(env, fail={"setvcp": 1})
    env.start()
    env.advance(SETTLE + 15)
    assert ddc_writes(env) == [[4, 31]], "no retry after one failure"
    env.tools.set_ddc(fail={"setvcp": -1})
    lux(env, 30.0)
    env.advance(LUX_SETTLE + 2 * GAP)
    setvcp = [c for c in env.tools.ddc()["calls"] if "setvcp" in c]
    assert len(setvcp) == 4
    assert env.last_state()["brightness"]["displays"][DDC_ID]["error"].startswith("ddcutil setvcp failed")
    env.tools.set_ddc(fail={})
    env.advance(3 * GAP)
    assert len([c for c in env.tools.ddc()["calls"] if "setvcp" in c]) == 4
    env.tree.connector("card1-HDMI-A-1")
    env.advance(SETTLE + 15)
    assert ddc_writes(env)[-1] == [4, 22]


def test_ddcutil_is_never_used_when_plasma_drives_the_displays(env: Env) -> None:
    monitor_env(env)
    kde_display(env)
    env.world.plasma()
    env.start()
    env.advance(SETTLE + 60)
    assert env.tools.ddc().get("calls", []) == []
    assert [d["backend"] for d in env.capabilities()["modules"]["brightness"]["displays"]] == ["kde"]


def test_once_plasma_has_driven_the_displays_ddcutil_stays_out_after_logout(env: Env) -> None:
    monitor_env(env)
    kde_display(env)
    env.world.plasma()
    env.start()
    env.advance(5)
    env.world.plasma(None)
    del env.world.sessions["3"]
    env.world.greeter()
    env.advance(60)
    assert env.tools.ddc().get("calls", []) == []


def test_without_ddcutil_the_problem_says_so(env: Env) -> None:
    env.env["TORPOR_DDCUTIL"] = str(env.tmp / "no-such-ddcutil")
    env.pair()
    env.world.login("3")
    env.start()
    env.advance(2)
    problems = env.capabilities()["modules"]["brightness"]["problems"]
    assert problems[0]["code"] in ("no_ddcutil", "ddcutil_failed")


def test_a_ddcutil_that_hangs_is_cut_off(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddc, "TIMEOUT_S", 0.5)
    monkeypatch.setattr(ddc, "RETRY_AFTER_S", 0)
    monitor_env(env, hang={"detect": 2})
    env.start()
    env.advance(5)
    problems = env.capabilities()["modules"]["brightness"]["problems"]
    assert problems and "timed out" in problems[0]["message"]


def test_plasma_once_then_another_desktop_uses_logind_and_ddcutil(env: Env) -> None:
    Known(env.paths.known_file).remember("brightness", "kde")
    env.pair()
    env.tree.backlight("intel_backlight", value=600, maximum=1200)
    session = env.world.login("3", desktop="GNOME")
    env.start()
    env.advance(11)
    displays = env.capabilities()["modules"]["brightness"]["displays"]
    assert [d["id"] for d in displays] == [BACKLIGHT_ID]
    env.world.sessions.pop(session.id)
    env.world.login("4", desktop="KDE")
    env.world.plasma()
    kde_display(env)
    env.advance(11)
    assert set(env.last_state()["brightness"]["displays"]) == {KDE_ID}


def test_plasma_once_stays_the_only_writer_while_logged_out(env: Env) -> None:
    Known(env.paths.known_file).remember("brightness", "kde")
    env.pair()
    env.tree.backlight("intel_backlight", value=600, maximum=1200)
    env.start()
    env.advance(11)
    assert env.last_state()["brightness"]["displays"] == {}
