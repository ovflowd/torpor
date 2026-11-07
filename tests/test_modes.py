"""Monitor modes: one read-only VCP code per display picks its anchor, read next to PowerDevil without fighting it.

Ported from the single-PC agent's Power Setting tests, with its numbers: an ASUS PG39WCDM whose OSD Power
Setting (VCP 0xE1) is 0x00 Standard or 0x01 Power Saving, anchors 40 and 75 % at 84 lx, slope 20, floor 15,
ceiling 100. The fake ddcutil checks every call in every test (fakes/mode_ddcutil.py).
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
from typing import Any

import pytest

from conftest import Env, gi_only
from fakes.bus import SB, SB_PATH
from fakes.desk import (
    KDE_ID,
    LABEL,
    SAVING,
    SETTLE,
    STANDARD,
    TICK,
    VALUES,
    at_desk,
    config,
    desk,
    lux,
    manual,
    slider,
    writes,
)
from fakes.mode_ddcutil import Monitor, getvcp_output, retries_output
from torpor import modes
from torpor.backends import ddcutil as ddc
from torpor.modes import Mode, ModeSetting, label_matches, settings_from_config
from torpor.modules import brightness as b
from torpor.runtime import Result, run


def logged(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def renumber(env: Env, old: str, new: str, *, signal: bool = True) -> None:
    """PowerDevil finding the monitor again (a DP blackout, an output toggle): it never reuses a name."""
    env.world.displays[new] = env.world.displays.pop(old)
    if signal:
        env.world.emit("session", SB, "DisplayRemoved", SB_PATH, (old,))
        env.world.emit("session", SB, "DisplayAdded", SB_PATH, (new,))
    env.loop.run_due()


def resume(env: Env) -> None:
    env.world.sleep(True)
    env.loop.run_due()
    env.advance(5)
    env.world.sleep(False)
    env.loop.run_due()


def after_next_poll(env: Env) -> None:
    count = len(env.ddc.calls)
    for _ in range(modes.POLL_S + 2 * TICK):
        env.advance(1)
        if len(env.ddc.calls) > count:
            return
    raise AssertionError(f"no poll: {env.ddc.summary()}")


def fail_reads(env: Env, count: int) -> None:
    """Every read failing until `count` have failed in a row, at the back-off's own pace."""
    env.ddc.broken = "retries"
    state = env.agent.brightness.modes.states[KDE_ID]  # type: ignore[union-attr]
    for _ in range(modes.RETRY_MAX_S * count):
        if state.failures >= count:
            return
        env.advance(1)
    raise AssertionError(f"{state.failures} failures: {env.ddc.summary()}")


def mode_state(env: Env, did: str = KDE_ID) -> Any:
    return env.last_state()["brightness"]["displays"][did]["mode"]


# The parsers.


def test_getvcp_verbose_and_replies_that_do_not_count() -> None:
    assert ddc.parse_raw_value(getvcp_output(1), 0xE1) == (1, 1)
    assert ddc.parse_raw_value(getvcp_output(0x1234, 0xFFFF, 0x14), 0x14) == (0x1234, 0xFFFF)
    assert ddc.parse_raw_value(getvcp_output(1, 1, 0xDD), 0xE1) == "a reply for 0xdd, not 0xe1"
    assert ddc.parse_raw_value(retries_output(), 0xE1) == "no reply"
    assert ddc.parse_raw_value(getvcp_output(1) + getvcp_output(0), 0xE1) == "more than one reply"


def test_the_mode_options_and_arguments() -> None:
    options = ["--noconfig", "--disable-udf", "--enable-cross-instance-locks", "--disable-dynamic-sleep"]
    assert ddc.mode_detect_args() == [*options, "detect", "--terse"]
    assert ddc.mode_read_args(9, 0xE1) == [*options, "--skip-ddc-checks", "getvcp", "--bus", "9", "--verbose", "0xE1"]
    assert modes.TIMEOUT_S < 3, "a read must not hold the bus lock as long as libddcutil waits for it"
    assert modes.QUIET_S >= 4 * 3 + 1 + 2 + 3, "shorter than a PowerDevil write and its three retries"


def test_a_plasma_label_matches_the_model_ddcutil_reads_from_the_edid() -> None:
    found = ddc.DdcDisplay(9, "AUS", "PG39WCDM", "SYN0001")
    assert label_matches(LABEL, found)
    assert label_matches("PG39WCDM", found)
    assert not label_matches("ASUSTek COMPUTER INC XG27AQ", found)
    assert not label_matches("XPG39WCDM", found)
    assert not label_matches(LABEL, ddc.DdcDisplay(9, "AUS", "", ""))


def test_mode_settings_from_home_assistant_skip_what_is_malformed() -> None:
    data = {
        KDE_ID: {"code": 0xE1, "values": [*VALUES, {"value": -1, "anchor_pct": 50}, {"value": 2, "anchor_pct": 101}]},
        "not-an-id": {"code": 0xE1},
        "a1b2c3d4e5f6": {"code": 256, "values": []},
        "0123456789ab": {"code": True},
    }
    assert settings_from_config(data) == {
        KDE_ID: ModeSetting(0xE1, {STANDARD: Mode(40.0, "Standard"), SAVING: Mode(75.0, "Power Saving")})
    }
    assert settings_from_config(None) == {}
    setting = settings_from_config(data)[KDE_ID]
    assert setting.lowest == 40.0
    assert setting.name(SAVING) == "0x01 (Power Saving)"
    assert setting.name(7) == "0x07"


# The curve each mode picks, and when the reads run.


@pytest.mark.parametrize(("value", "written", "name"), [(SAVING, 7500, "Power Saving"), (STANDARD, 4000, "Standard")])
def test_the_mode_picks_the_anchor(
    env: Env, caplog: pytest.LogCaptureFixture, value: int, written: int, name: str
) -> None:
    caplog.set_level("INFO", logger="torpor")
    desk(env)
    env.ddc.osd(value)
    env.start()
    env.advance(SETTLE - 1)
    assert env.ddc.kinds() == [], "ran ddcutil inside the desk settle"
    env.advance(16)
    assert writes(env) == [written]
    assert env.ddc.kinds() == ["detect", "getvcp"]
    read = env.ddc.reads()[0]
    options = list(ddc.MODE_OPTIONS)
    assert read.args == [*options, "--skip-ddc-checks", "getvcp", "--bus", "9", "--verbose", "0xE1"]
    assert read.end <= env.world.write_times[0], "wrote before the read ended"
    assert logged(caplog, f"{LABEL} mode: 0x{value:02x} ({name})")
    assert logged(caplog, f"{LABEL} brightness: 50 -> {written // 100} % (84 lx, mode 0x{value:02x} ({name}))")
    assert mode_state(env) == {"value": value, "error": None}
    assert env.last_state()["brightness"]["displays"][KDE_ID]["target"] == written // 100
    saved = json.loads(env.paths.boot_state_file.read_text())["modes"]
    assert saved == {KDE_ID: {"code": 0xE1, "value": value}}


def test_every_write_follows_a_read_that_just_ended(env: Env) -> None:
    """Reads take 0.5 s here and detect 1.5 s; the fake flags any write while one runs. The bus is found once."""
    desk(env)
    env.ddc.delay, env.ddc.detect_delay = 0.5, 1.5
    env.start()
    env.advance(SETTLE + 15, step=0.01)
    for value in (30.0, 84.0, 41.0, 150.0, 30.0):
        lux(env, value)
        env.advance(b.LUX_SETTLE_S + b.GAP_DDC_S)
    assert writes(env) == [7500, 6600, 7500, 6900, 8000, 6600]
    reads = env.ddc.reads()
    for at in env.world.write_times:
        before = [call for call in reads if call.end <= at]
        assert before and at - before[-1].end < modes.FRESH_S, f"a write at {at} without a fresh read"
    assert env.ddc.kinds().count("detect") == 1
    detect, first = env.ddc.calls[0], env.ddc.calls[1]
    assert (detect.kind, first.kind) == ("detect", "getvcp")
    assert first.start - detect.end >= modes.SPACING_S, "no room for a PowerDevil write between detect and getvcp"


def test_a_mode_changed_in_the_osd_is_an_edge(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """It shows at the next poll and goes out after EDGE_GAP_S, not the 5 minute gap."""
    caplog.set_level("INFO", logger="torpor")
    at_desk(env)
    assert writes(env) == [7500]
    first = env.world.write_times[0]
    env.advance(first + b.EDGE_GAP_S - env.loop.mono)
    env.ddc.osd(STANDARD)
    env.advance(modes.POLL_S + TICK + 1)
    assert writes(env) == [7500, 4000]
    assert env.world.write_times[1] - first < b.GAP_DDC_S
    assert logged(caplog, f"{LABEL} mode: 0x00 (Standard)")
    assert mode_state(env)["value"] == STANDARD
    env.ddc.osd(SAVING)
    env.advance(modes.POLL_S + TICK + 1)
    assert writes(env) == [7500, 4000, 7500]


def test_a_mode_change_ends_a_hand_set_pause(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="torpor")
    at_desk(env)
    slider(env, 5000)
    env.advance(modes.QUIET_S + 15)
    assert manual(env) == 75
    env.advance(b.GAP_DDC_S)
    assert writes(env) == [7500]
    env.ddc.osd(STANDARD)
    env.advance(modes.POLL_S + TICK + 1)
    assert writes(env) == [7500, 4000]
    assert manual(env) is None
    assert logged(caplog, "automatic brightness resumed")


def test_a_mode_change_with_close_anchors_still_ends_the_pause(env: Env) -> None:
    values = [{"value": STANDARD, "anchor_pct": 40}, {"value": SAVING, "anchor_pct": 43}]
    at_desk(env, values=values)
    slider(env, 6000)
    env.advance(modes.QUIET_S + 15)
    assert manual(env) == 43
    env.ddc.osd(STANDARD)
    env.advance(modes.POLL_S + TICK + 1)
    assert manual(env) is None and writes(env) == [4300, 4000]


def test_an_osd_change_then_a_slider_move_keeps_the_pause(env: Env) -> None:
    """The baseline waits for a read begun after the slider move, so the OSD change he made first is part of it."""
    at_desk(env)
    after_next_poll(env)
    env.ddc.osd(STANDARD)
    env.advance(5)
    slider(env, 3000)
    env.advance(modes.QUIET_S - 5)
    assert manual(env) == b.PENDING, "baseline taken before a read"
    env.advance(15)
    assert manual(env) == 40
    env.advance(b.GAP_DDC_S + 2 * modes.POLL_S)
    assert writes(env) == [7500]


def test_a_monitor_that_never_answers(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """Each failed read drops the bus and backs off 60, 120, 240 s; the lowest anchor stays; the first write waits
    for TRIES failures. A read that works again resets it all, and the new anchor goes out as an edge."""
    caplog.set_level("INFO", logger="torpor")
    desk(env)
    env.ddc.broken = "retries"
    env.start()
    env.advance(SETTLE + 4 * modes.RETRY_S + 2 * TICK)
    reads = env.ddc.reads()
    assert len(reads) == modes.TRIES
    gaps = [later.start - earlier.end for earlier, later in itertools.pairwise(reads)]
    assert modes.RETRY_S <= gaps[0] < modes.RETRY_S + TICK + 2 and gaps[1] >= 2 * modes.RETRY_S
    assert env.ddc.kinds() == ["detect", "getvcp"] * modes.TRIES
    assert writes(env) == [4000] and env.world.write_times[0] >= reads[-1].end
    said = "VCP code 0xe1 (Manufacturer Specific ): Maximum retries exceeded"
    for delay in (60, 120, 240):
        line = (
            f"{LABEL}: reading its mode failed: ddcutil getvcp failed (exit 1: {said}); brightness keeps the lowest "
            f"anchor, next try in {delay} s"
        )
        assert len(logged(caplog, line)) == 1, line
    assert mode_state(env) == {"value": None, "error": f"ddcutil getvcp failed (exit 1: {said})"}
    env.ddc.broken = None
    env.advance(4 * modes.RETRY_S + TICK)
    assert writes(env) == [4000, 7500]
    state = env.agent.brightness.modes.states[KDE_ID]  # type: ignore[union-attr]
    assert (state.failures, state.next_at, state.error) == (0, None, None)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("timeout", "ddcutil getvcp timed out"),
        ("other-code", "implausible reply: a reply for 0xdd, not 0xe1"),
    ],
)
def test_a_failed_read_keeps_the_last_mode_and_holds_the_write(
    env: Env, caplog: pytest.LogCaptureFixture, failure: str, reason: str
) -> None:
    """A bus or a monitor that isn't answering would fail PowerDevil's write too, so the write waits for the retry."""
    caplog.set_level("INFO", logger="torpor")
    at_desk(env, brightness=7500)
    env.ddc.fail = [failure]
    lux(env, 30.0)
    env.advance(b.LUX_SETTLE_S + modes.RETRY_S + 3 * TICK)
    line = f"{LABEL}: reading its mode failed: {reason}; brightness keeps 0x01 (Power Saving), next try in 60 s"
    assert len(logged(caplog, line)) == 1
    failed = next(call for call in env.ddc.calls if call.failure == failure)
    later = [call for call in env.ddc.calls if call.start > failed.start]
    assert later and later[0].start - failed.end >= modes.RETRY_S, "retried too soon"
    assert later[0].kind == "detect", "the bus was not found again"
    working = next(call for call in later if call.kind == "getvcp" and call.status == 0)
    assert writes(env) == [6600] and env.world.write_times[0] >= working.end
    if failure == "timeout":
        assert failed.end - failed.start == modes.TIMEOUT_S


@pytest.mark.parametrize("answer", ["mode 2", "greyed"])
def test_a_value_none_of_the_rows_has_keeps_the_mode_and_the_bus(
    env: Env, caplog: pytest.LogCaptureFixture, answer: str
) -> None:
    """A mode Home Assistant has no row for yet, or the PG39WCDM's 0xFE for a setting its OSD greys out, is a
    clean reply: no back-off and no new detect. The last known mode stays, writes don't wait, the error names
    the value, and a listed value is back at the next poll."""
    caplog.set_level("INFO", logger="torpor")
    at_desk(env, brightness=7500)
    if answer == "greyed":
        env.ddc.broken = "greyed"
    else:
        env.ddc.osd(0x02)
    after_next_poll(env)
    value = 0xFE if answer == "greyed" else 0x02
    error = f"the monitor answers 0x{value:02x}, none of the values set in Home Assistant"
    state = env.agent.brightness.modes.states[KDE_ID]  # type: ignore[union-attr]
    assert (state.value, state.failures, state.next_at, state.bus) == (SAVING, 0, None, 9)
    env.advance(TICK)
    assert mode_state(env) == {"value": SAVING, "error": error}
    lux(env, 30.0)
    changed = env.loop.mono
    env.advance(b.LUX_SETTLE_S + b.GAP_DDC_S)
    assert writes(env) == [6600] and env.world.write_times[0] - changed < b.LUX_SETTLE_S + 2 * TICK
    assert len(logged(caplog, f"{LABEL}: {error}; brightness keeps 0x01 (Power Saving)")) == 1
    assert env.ddc.kinds().count("detect") == 1
    env.ddc.broken = None
    env.ddc.osd(STANDARD)
    polled = env.loop.mono
    env.advance(modes.POLL_S + TICK + 1)
    assert mode_state(env) == {"value": STANDARD, "error": None}
    assert writes(env) == [6600, 3100] and env.world.write_times[-1] - polled <= modes.POLL_S + TICK + 1


def test_reads_keep_clear_of_brightness_changes(env: Env) -> None:
    """No call within QUIET_S of a BrightnessChanged (echoes here), a DisplayAdded or a new PowerDevil; a due poll
    waits and then runs. The fake checks every call in every test; this one drives the cases."""
    at_desk(env)
    last = env.ddc.calls[-1].start
    env.advance(last + modes.POLL_S - 5 - env.loop.mono)
    for _ in range(4):
        slider(env, 7500)
        env.advance(20)
    assert env.ddc.calls[-1].start == last and manual(env) is None, "polled within 30 s"
    env.advance(modes.QUIET_S)
    assert env.ddc.calls[-1].start > last, "the postponed poll never ran"
    calls = len(env.ddc.calls)
    renumber(env, "display0", "display1")
    env.advance(modes.QUIET_S - 1)
    assert len(env.ddc.calls) == calls, "read right after a DisplayAdded"
    env.advance(15)
    assert env.ddc.kinds()[calls:] == ["detect", "getvcp"]
    after_next_poll(env)
    detects = env.ddc.kinds().count("detect")
    env.world.plasma(":1.400")
    calls = len(env.ddc.calls)
    env.advance(modes.POLL_S + TICK)
    assert len(env.ddc.calls) == calls + 1
    assert env.ddc.calls[-1].start - env.world.changes[-1] >= modes.QUIET_S
    assert env.ddc.kinds().count("detect") == detects, "detected again for a new PowerDevil"


def test_the_bus_is_found_again_after_a_resume_a_renumber_or_a_failure(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """krdp's output coming and going is no reason; a detect that finds no working monitor is a failed read."""
    caplog.set_level("INFO", logger="torpor")
    at_desk(env)
    env.world.displays["display7"] = {"Label": "krdp", "Brightness": 10000, "MaxBrightness": 10000}
    env.world.emit("session", SB, "DisplayAdded", SB_PATH, ("display7",))
    env.advance(5 * 60)
    env.world.displays.pop("display7")
    env.world.emit("session", SB, "DisplayRemoved", SB_PATH, ("display7",))
    env.advance(25 * 60)
    assert env.ddc.kinds().count("detect") == 1 and env.ddc.kinds().count("getvcp") >= 14
    resume(env)
    env.advance(SETTLE + 15)
    assert env.ddc.kinds().count("detect") == 2 and env.ddc.kinds()[-2:] == ["detect", "getvcp"]
    env.ddc.monitors[0].bus = 10
    renumber(env, "display0", "display1")
    env.advance(SETTLE + 15)
    assert env.ddc.kinds().count("detect") == 3 and env.ddc.calls[-1].args[-3] == "10"
    env.ddc.fail = ["retries"]
    after_next_poll(env)
    assert env.ddc.calls[-1].failure == "retries"
    env.advance(modes.RETRY_S + TICK + 3)
    assert env.ddc.kinds().count("detect") == 4 and env.ddc.kinds()[-2:] == ["detect", "getvcp"]
    renumber(env, "display1", "display2", signal=False)
    slider(env, 5000, name="display2")
    env.advance(modes.QUIET_S + TICK)
    assert env.ddc.kinds().count("detect") == 5 and env.ddc.kinds()[-2:] == ["detect", "getvcp"]
    env.ddc.fail = ["retries", "no-monitor"]
    after_next_poll(env)
    env.advance(modes.RETRY_S + TICK + 1)
    assert env.ddc.calls[-1].failure == "no-monitor"
    assert logged(caplog, f"ddcutil detect: no working DDC/CI display matches {LABEL}; brightness keeps 0x01")
    env.advance(2 * modes.RETRY_S + TICK + 1)
    assert env.ddc.kinds()[-2:] == ["detect", "getvcp"] and env.ddc.calls[-1].status == 0


def test_the_back_off_caps_at_30_minutes_and_a_resume_or_renumber_starts_afresh(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="torpor")
    at_desk(env)
    fail_reads(env, 7)
    for delay in (60, 120, 240, 480, 960, 1800):
        assert logged(caplog, f"next try in {delay} s"), delay
    state = env.agent.brightness.modes.states[KDE_ID]  # type: ignore[union-attr]
    assert state.next_at is not None
    assert 0 <= state.next_at - env.ddc.calls[-1].end - modes.RETRY_MAX_S < 1
    env.ddc.broken = None
    env.ddc.osd(STANDARD)
    resume(env)
    env.advance(SETTLE + 15)
    assert env.ddc.kinds()[-2:] == ["detect", "getvcp"] and state.failures == 0
    assert writes(env)[-1] == 4000

    fail_reads(env, 7)
    env.ddc.broken = None
    env.ddc.osd(SAVING)
    renumber(env, "display0", "display1")
    env.advance(SETTLE + 15)
    assert env.ddc.kinds()[-2:] == ["detect", "getvcp"] and state.failures == 0
    call = env.world.writes("SetBrightnessWithContext")[-1]
    assert (call.target[1].rsplit("/", 1)[-1], call.args[0]) == ("display1", 7500)


def test_a_pause_with_an_unreadable_monitor_takes_the_last_known_mode(env: Env) -> None:
    at_desk(env)
    slider(env, 5000)
    fail_reads(env, modes.TRIES)
    env.advance(TICK)
    assert manual(env) == 75 and writes(env) == [7500]


def test_the_first_read_that_works_keeps_a_pause(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """A baseline taken on the lowest anchor, every read failing since the start, is taken again when the first
    read that works finds Power Saving: the mode was learned, not changed."""
    caplog.set_level("INFO", logger="torpor")
    desk(env)
    env.ddc.broken = "retries"
    env.start()
    env.advance(SETTLE + 4 * modes.RETRY_S + 2 * TICK)
    assert writes(env) == [4000]
    slider(env, 5000)
    env.advance(modes.QUIET_S + TICK)
    assert manual(env) == 40
    env.ddc.broken = None
    env.advance(modes.RETRY_MAX_S)
    assert logged(caplog, f"{LABEL} mode: 0x01 (Power Saving)") and manual(env) == 75
    assert writes(env) == [4000] and not logged(caplog, "resumed")


def test_a_read_begun_before_a_change_does_not_count(env: Env) -> None:
    """A read still running (2 s here, as when it waits for the bus lock) when he moves the slider, or when the
    monitor is renumbered, may have caught the monitor before he changed its OSD: it is dropped."""
    at_desk(env)
    env.ddc.delay = 2
    after_next_poll(env)
    env.advance(0.5)
    env.ddc.osd(STANDARD)
    slider(env, 3000)
    env.advance(2)
    state = env.agent.brightness.modes.states[KDE_ID]  # type: ignore[union-attr]
    assert state.stale and manual(env) == b.PENDING and state.value == SAVING
    env.advance(modes.QUIET_S + 2 * TICK)
    assert manual(env) == 40
    env.advance(b.GAP_DDC_S + 2 * modes.POLL_S)
    assert writes(env) == [7500]

    read_at = state.read_at
    after_next_poll(env)
    read_at = state.read_at
    renumber(env, "display0", "display1")
    env.advance(3)
    assert state.stale and state.read_at == read_at


def test_each_call_starts_a_spacing_after_the_last_one_ended(env: Env) -> None:
    """A brightness tick that lands right after detect ends must not start getvcp before a PowerDevil write
    waiting for the lock had its chance."""
    at_desk(env)
    agent = env.agent
    assert agent is not None and agent.facts is not None
    agent.brightness.modes.reprobe(KDE_ID)
    count = len(env.ddc.calls)
    for _ in range(int((modes.POLL_S + 2 * TICK) / 0.01)):
        env.advance(0.01, step=0.01)
        if len(env.ddc.calls) > count:
            break
    detect = env.ddc.calls[-1]
    assert detect.kind == "detect"
    env.advance(detect.end - env.loop.mono, step=0.01)
    agent.brightness.tick(agent.facts)
    env.loop.run_due()
    assert env.ddc.calls[-1] is detect, "getvcp started the instant detect ended"
    env.advance(modes.SPACING_S + 0.05, step=0.01)
    read = env.ddc.calls[-1]
    assert read.kind == "getvcp" and read.start - detect.end >= modes.SPACING_S - 1e-6


def test_one_call_at_a_time_and_a_set_from_home_assistant_waits_for_it(env: Env) -> None:
    at_desk(env)
    env.ddc.delay = 2
    after_next_poll(env)
    calls = len(env.ddc.calls)
    env.agent.tick()  # type: ignore[union-attr]
    env.loop.run_due()
    assert len(env.ddc.calls) == calls, "a second call while one ran"
    command = env.ha.command("brightness_set", display=KDE_ID, percent=55)
    env.loop.run_due()
    assert writes(env) == [7500], "wrote while a read held the bus"
    env.advance(2)
    assert writes(env) == [7500, 5500]
    assert env.result(command)["ok"] is True


def test_without_a_usable_ddcutil_the_lowest_anchor_and_no_reads(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="torpor")
    desk(env)
    env.env["TORPOR_DDCUTIL"] = str(env.tmp / "no-such-ddcutil")
    env.start()
    env.advance(SETTLE + 15)
    assert writes(env) == [4000] and env.ddc.calls == []
    assert len(logged(caplog, "monitor modes can't be read: ddcutil is not installed")) == 1
    caps = env.capabilities()["modules"]["brightness"]["modes"]
    assert caps == {
        "available": False,
        "problems": [{"code": "no_ddcutil", "message": "ddcutil is not installed", "hint": "torporctl doctor"}],
    }
    slider(env, 2000)
    env.advance(15)
    assert manual(env) == 40
    assert mode_state(env) == {"value": None, "error": None}


def test_a_failed_version_check_is_asked_again(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """One failed `ddcutil --version` (a library mid-update) must not turn mode reads off until a restart: it is
    asked again on the read back-off, and after a resume."""
    caplog.set_level("INFO", logger="torpor")
    desk(env)
    env.ddc.version = None
    env.start()
    env.advance(SETTLE + 15)
    assert env.ddc.versions == 1 and env.ddc.kinds() == [] and writes(env) == [4000]
    assert [p["code"] for p in env.capabilities()["modules"]["brightness"]["modes"]["problems"]] == ["ddcutil_failed"]
    env.advance(modes.RETRY_S)
    assert env.ddc.versions == 2
    env.advance(modes.RETRY_S)
    assert env.ddc.versions == 2, "no back-off"
    env.ddc.version = "3.0.2"
    env.advance(modes.RETRY_S + TICK)
    assert env.ddc.versions == 3 and env.ddc.kinds() == ["detect", "getvcp"]
    assert writes(env) == [4000, 7500]
    assert env.capabilities()["modules"]["brightness"]["modes"] == {"available": True, "problems": []}
    resume(env)
    assert env.ddc.versions == 4


@pytest.mark.parametrize(
    ("version", "devices", "code"),
    [("1.4.1", True, "old_ddcutil"), ("2.1.4", True, "old_ddcutil"), ("3.0.2", False, "no_i2c")],
)
def test_an_old_ddcutil_or_no_i2c_device_reads_nothing(env: Env, version: str, devices: bool, code: str) -> None:
    """ddcutil 1.4 has none of the options, and 2.1 had the cross-instance lock off by default."""
    desk(env)
    if not devices:
        for device in env.tree.dev.glob("i2c-*"):
            device.unlink()
    env.ddc.version = version
    env.start()
    env.advance(SETTLE + 15)
    assert writes(env) == [4000]
    assert env.ddc.kinds() == []
    problems = env.capabilities()["modules"]["brightness"]["modes"]["problems"]
    assert [p["code"] for p in problems] == [code]


def test_a_ready_pc_reports_mode_reads_available(env: Env) -> None:
    at_desk(env)
    assert env.capabilities()["modules"]["brightness"]["modes"] == {"available": True, "problems": []}
    displays = env.capabilities()["modules"]["brightness"]["displays"]
    assert [(d["id"], d["modes"]) for d in displays] == [(KDE_ID, True)]


def test_the_mode_survives_an_agent_restart_but_not_a_reboot(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """A restarted agent starts from the last mode read, so the lowest anchor can't end a pause while the
    monitor can't be read; a change made while it was down shows at its first read and ends the pause."""
    caplog.set_level("INFO", logger="torpor")
    at_desk(env)
    slider(env, 5000)
    env.advance(modes.QUIET_S + 15)
    assert manual(env) == 75
    env.ddc.broken = "timeout"
    env.restart()
    env.advance(1)
    assert env.agent is not None and env.agent.brightness.modes.states[KDE_ID].value == SAVING
    env.advance(SETTLE + 10 * 60)
    assert env.ddc.calls[-1].failure == "timeout"
    assert manual(env) == 75 and writes(env) == [7500]
    env.ddc.broken = None
    env.ddc.osd(STANDARD)
    env.restart()
    env.advance(SETTLE + 15)
    assert manual(env) is None and writes(env) == [7500, 4000]
    env.tree.boot("boot-b")
    env.restart()
    env.advance(1)
    assert env.agent.brightness.modes.states[KDE_ID].value is None


def test_it_polls_every_two_minutes_at_the_desk_and_never_away_from_it(env: Env) -> None:
    at_desk(env)
    env.advance(3600)
    starts = [call.start for call in env.ddc.reads()]
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert gaps and all(modes.POLL_S <= gap <= modes.POLL_S + TICK for gap in gaps), gaps
    env.world.sessions["3"].locked = True
    env.advance(15)
    calls = len(env.ddc.calls)
    env.advance(3600)
    assert len(env.ddc.calls) == calls, "read the monitor while locked"


def test_modes_are_read_with_auto_brightness_off_too(env: Env) -> None:
    at_desk(env, auto=False)
    assert writes(env) == [] and env.ddc.kinds() == ["detect", "getvcp"]
    assert mode_state(env) == {"value": SAVING, "error": None}
    assert env.last_state()["brightness"]["displays"][KDE_ID]["target"] == 75


def test_without_values_the_raw_mode_is_reported_and_the_curve_stays(env: Env) -> None:
    """A code with no values yet: how to learn what the monitor answers before giving each value an anchor.
    Its values move no curve, so a new one keeps a hand-set pause, and even 0xFE is a value to learn."""
    at_desk(env, values=[])
    assert writes(env) == [4000]
    assert mode_state(env) == {"value": SAVING, "error": None}
    env.ddc.osd(0x05)
    env.advance(modes.POLL_S + TICK)
    assert mode_state(env)["value"] == 0x05 and writes(env) == [4000]
    slider(env, 2500)
    env.advance(modes.QUIET_S + TICK)
    assert manual(env) == 40
    env.ddc.osd(0x06)
    env.advance(modes.POLL_S + TICK)
    assert mode_state(env)["value"] == 0x06 and manual(env) == 40
    env.ddc.broken = "greyed"
    env.advance(modes.POLL_S + TICK)
    assert mode_state(env) == {"value": 0xFE, "error": None} and manual(env) == 40


def test_two_monitors_with_the_same_model_cannot_tell_their_buses_apart(env: Env) -> None:
    desk(env)
    env.ddc.monitors.append(Monitor(10, serial="SYN0002", connector="card1-DP-2"))
    env.start()
    env.advance(SETTLE + 15)
    assert mode_state(env)["error"] == f"ddcutil detect: 2 DDC/CI displays match {LABEL}"


def test_a_ddcutil_backend_display_is_found_by_its_edid(env: Env) -> None:
    """Without Plasma, Torpor drives the monitor with ddcutil itself; the mode read finds it by EDID, not label."""
    monitor = {"bus": 9, "mfg": "AUS", "model": "PG39WCDM", "serial": "SYN0001", "connector": "card1-DP-1"}
    env.tools.set_ddc(monitors=[{**monitor, "brightness": 50}])
    did = b.display_id("ddcutil", "AUS:PG39WCDM:SYN0001")
    env.pair()
    env.tree.i2c()
    env.world.login("3")
    env.tree.connector("card1-DP-1")
    env.ha.config = config(did)
    env.ha.lux = (84.0, 3600.0)
    env.start()
    env.advance(SETTLE + 15)
    assert env.tools.ddc()["writes"] == [[9, 75]]
    assert env.ddc.kinds() == ["detect", "getvcp"]
    assert mode_state(env, did)["value"] == SAVING


def test_a_display_without_a_mode_code_is_never_read(env: Env) -> None:
    desk(env)
    env.ha.config = {**config(), "brightness": {**config()["brightness"], "modes": {}}}
    env.start()
    env.advance(SETTLE + 3600)
    assert env.ddc.kinds() == [] and writes(env) == [4000]
    assert "mode" not in env.last_state()["brightness"]["displays"][KDE_ID]


# The real runner.


@gi_only
def test_the_spawn_runner_pins_the_options_and_kills_a_hang(tmp_path: Any) -> None:
    """GLibLoop.spawn against a fake ddcutil binary: output and exit status come back, a ddcutil that hangs is
    killed after the timeout (so it can't hold the bus lock past libddcutil's 3 s wait), a missing one fails."""
    from torpor.glib import GLibLoop

    script = tmp_path / "ddcutil"
    calls = tmp_path / "calls"
    script.write_text(
        f"#!{sys.executable} -I\n"
        "import os, sys, time\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + ' ' + str(os.getpid()) + '\\n')\n"
        "if 'hang' in os.environ.get('MODE', ''): time.sleep(60)\n"
        f"sys.stdout.write({getvcp_output(1)!r})\n"
        "sys.exit(0 if os.environ.get('MODE') == 'ok' else 1)\n"
    )
    script.chmod(0o755)
    loop = GLibLoop()

    def spawn(argv: list[str], mode: str, timeout: float = modes.TIMEOUT_S) -> Result:
        results: list[Result] = []
        loop.spawn(argv, timeout=timeout, env={**os.environ, "MODE": mode}, done=results.append)
        assert loop.run_until(lambda: bool(results), 10)
        return results[0]

    args = [str(script), *ddc.mode_read_args(9, 0xE1)]
    ok = spawn(args, "ok")
    assert ok.code == 0 and ddc.parse_raw_value(ok.output, 0xE1) == (1, 1)
    assert calls.read_text().split()[:-1] == ddc.mode_read_args(9, 0xE1)
    assert spawn(args, "fail").code == 1
    began = time.monotonic()
    hung = spawn(args, "hang", timeout=0.3)
    assert hung.code is None and "timed out after 0.3 s" in hung.output
    assert time.monotonic() - began < 3
    pid = int(calls.read_text().splitlines()[-1].split()[-1])
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    missing = spawn([str(tmp_path / "missing"), "--version"], "ok")
    assert missing.code is None and "cannot run" in missing.output


def test_the_fake_ddcutil_binary_answers_mode_reads_like_the_real_ones(env: Env) -> None:
    """The end-to-end test runs this fake as a real process: it must parse like 3.0.2 and refuse like 1.4.1."""
    monitor = {"bus": 9, "mfg": "AUS", "model": "PG39WCDM", "serial": "SYN0001", "brightness": 50, "vcp": {"e1": 1}}
    env.tools.set_ddc(monitors=[monitor])
    binary = str(env.tools.folder / "ddcutil")
    detect = run([binary, *ddc.mode_detect_args()], env=env.env)
    assert detect.ok and [found.bus for found in ddc.parse_detect(detect.output)] == [9]
    read = run([binary, *ddc.mode_read_args(9, 0xE1)], env=env.env)
    assert read.ok and ddc.parse_raw_value(read.output, 0xE1) == (1, 1)
    missing = run([binary, *ddc.mode_read_args(9, 0x14)], env=env.env)
    assert missing.code == 1 and ddc.parse_raw_value(missing.output, 0x14) == "no reply"
    env.tools.set_ddc(version="1.4.1")
    old = run([binary, *ddc.mode_read_args(9, 0xE1)], env=env.env)
    assert old.code == 1 and "Unknown option --noconfig" in old.output


@pytest.mark.skipif(os.getuid() == 0, reason="root opens any device")
def test_i2c_devices_without_access_read_nothing(env: Env) -> None:
    desk(env)
    env.tree.i2c(access=False)
    env.start()
    env.advance(SETTLE + 15)
    assert env.ddc.kinds() == [] and writes(env) == [4000]
    assert env.capabilities()["modules"]["brightness"]["modes"]["problems"][0]["code"] == "no_i2c"


def going_down(env: Env) -> float:
    env.world.sleep(True)
    env.loop.run_due()
    return env.loop.mono


def first_call(env: Env, kind: str, limit: float = modes.POLL_S + 2 * TICK) -> Any:
    count = len(env.ddc.calls)
    for _ in range(int(limit / 0.01)):
        env.advance(0.01, step=0.01)
        if len(env.ddc.calls) > count and env.ddc.calls[-1].kind == kind:
            return env.ddc.calls[-1]
    raise AssertionError(f"no {kind}: {env.ddc.summary()}")


def test_nothing_is_written_or_read_once_the_pc_is_going_down(env: Env) -> None:
    """A game edge waits for a read (2 s here); PrepareForSleep comes 1 s into it. When the read ends, the write
    it held must not go out at suspend entry, nor a getvcp after a detect, nor a set from Home Assistant."""
    env.world.gamemode()
    at_desk(env, 7500)
    env.ddc.delay = 2
    env.advance(modes.POLL_S)
    env.world.game(True)
    read = first_call(env, "getvcp")
    assert writes(env) == [], "the boost went out without a read"
    env.advance(1, step=0.01)
    going_down(env)
    env.advance(30, step=0.01)
    assert read.end < env.loop.mono and writes(env) == [] and env.ddc.calls[-1] is read

    env.world.sleep(False)
    env.loop.run_due()
    env.ddc.delay, env.ddc.detect_delay = 0.12, 1.0
    detect = first_call(env, "detect", SETTLE + 2 * TICK)
    env.advance(0.5, step=0.01)
    down = going_down(env)
    env.advance(5, step=0.01)
    assert detect.end < env.loop.mono and env.ddc.calls[-1] is detect
    assert all(at < down for at in env.world.write_times)


def test_a_set_from_home_assistant_waiting_for_a_read_is_refused_at_suspend(env: Env) -> None:
    at_desk(env)
    env.ddc.delay = 2
    first_call(env, "getvcp")
    env.ha.command("brightness_set", display=KDE_ID, percent=55)
    env.loop.run_due()
    going_down(env)
    env.advance(5, step=0.01)
    assert writes(env) == [7500]


def test_a_read_that_ends_after_the_agent_stopped_starts_nothing(env: Env) -> None:
    at_desk(env)
    env.ddc.detect_delay = 1.0
    env.agent.brightness.modes.reprobe(KDE_ID)  # type: ignore[union-attr]
    detect = first_call(env, "detect")
    env.advance(0.95, step=0.01)
    env.agent.stop()  # type: ignore[union-attr]
    env.advance(2, step=0.01)
    assert env.ddc.calls[-1] is detect


def test_a_read_that_ends_after_a_lock_writes_nothing(env: Env) -> None:
    """The write a read held waits for facts read when it ends, not the ones from before the read."""
    env.world.gamemode()
    at_desk(env, 7500)
    env.ddc.delay = 2
    env.advance(modes.POLL_S)
    env.world.game(True)
    first_call(env, "getvcp")
    env.world.sessions["3"].locked = True
    env.advance(3, step=0.01)
    assert writes(env) == []


def test_a_display_plugged_in_or_out_starts_the_quiet_window_too(env: Env) -> None:
    """Without Plasma's signals, a new set of connected connectors is the sign that something probes the bus."""
    at_desk(env)
    after_next_poll(env)
    env.advance(modes.QUIET_S)
    calls = len(env.ddc.calls)
    env.tree.connector("card1-HDMI-A-1")
    env.advance(TICK)
    plugged = env.agent.brightness.modes.quiet_at  # type: ignore[union-attr]
    assert plugged is not None and env.loop.mono - plugged <= TICK
    env.advance(modes.POLL_S)
    assert all(call.start - plugged >= modes.QUIET_S for call in env.ddc.calls[calls:])
