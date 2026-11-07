"""Games: detected through GameMode or a power-profiles-daemon performance hold, and the brightness boost.

The boost tests port the single-PC agent's, generalised from its amd-game hold to both sources and from
its fixed +30 to Home Assistant's game_boost. Each display here has the PG39WCDM's two modes.
"""

from __future__ import annotations

import itertools

import pytest

from conftest import Env
from fakes.bus import GAMEMODE, OLD_PPD, PPD
from fakes.desk import KDE_ID, SAVING, SETTLE, STANDARD, TICK, at_desk, config, desk, lux, manual, slider, writes
from torpor import modes
from torpor.bus import BusError
from torpor.curve import Curve
from torpor.modules import brightness as b

REASON = "Launched with CachyOS game-performance utility"


def gamemode_desk(env: Env, brightness: int = 7500, setting: int = SAVING) -> Env:
    """GameMode installed and running, the monitor on `setting`, at its target already."""
    env.world.gamemode()
    env.ddc.osd(setting)
    return at_desk(env, brightness)


def game(env: Env, on: bool) -> None:
    env.world.game(on)
    env.loop.run_due()


def boost(env: Env, value: float) -> None:
    message = config()
    message["brightness"]["game_boost"] = value
    env.ha.push_config(**message)
    env.loop.run_due()


def test_the_boost_is_added_on_top_of_the_ceiling_up_to_100() -> None:
    curve = Curve(anchor_lux=84, anchor_pct=40, slope=20, floor=15, ceiling=80)
    assert curve.target(84, boost=30) == 70
    assert curve.target(84, anchor_pct=75, boost=30) == 100
    assert curve.target(5000, boost=30) == 100
    assert curve.target(1, boost=30) == 45
    assert curve.target(84, anchor_pct=75) == 75
    assert curve.target(84, boost=0) == 40


# Detection.


def test_gamemode_installed_is_a_source_and_its_clients_are_games(env: Env) -> None:
    env.pair()
    env.world.gamemode(running=False)
    env.world.login("3")
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["core"]["games"] == ["gamemode"]
    assert env.last_state()["game"] is False
    assert not env.world.called("Get") or all(c.args != (GAMEMODE, "ClientCount") for c in env.world.called("Get")), (
        "asked a gamemoded that isn't running"
    )
    env.world.set_owner("session", GAMEMODE, ":1.80")
    game(env, True)
    env.advance(1)
    assert env.last_state()["game"] is True
    game(env, False)
    env.advance(1)
    assert env.last_state()["game"] is False


@pytest.mark.parametrize("name", [PPD, OLD_PPD])
def test_a_performance_hold_with_a_matching_reason_is_a_game(env: Env, name: str) -> None:
    env.pair()
    env.set("games.performance-hold", "game-performance")
    env.world.set_owner("system", name, ":1.20")
    env.world.login("3")
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["core"]["games"] == ["performance_hold"]
    env.world.hold(REASON)
    env.advance(1)
    assert env.last_state()["game"] is True
    env.world.hold("Watching a video", profile="performance")
    env.advance(1)
    assert env.last_state()["game"] is False
    env.world.hold(REASON, profile="power-saver")
    env.advance(1)
    assert env.last_state()["game"] is False


def test_a_daemon_restart_or_a_slow_bus_keeps_the_game_sources(env: Env) -> None:
    """Home Assistant removes the Game running sensor when the sources go, so neither a daemon that restarts or
    exits nor a bus too slow to answer may drop them. power-profiles-daemon is activatable, as its own service
    files make it."""
    env.pair()
    env.set("games.performance-hold", "game-performance")
    env.world.activatable["system"].add(PPD)
    env.world.set_owner("system", PPD, ":1.20")
    env.world.gamemode()
    env.world.login("3")
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["core"]["games"] == ["gamemode", "performance_hold"]
    sent = len(env.ha.received("capabilities"))
    env.world.set_owner("system", PPD, None)
    env.world.set_owner("session", GAMEMODE, None)
    env.advance(2 * TICK)
    timeout = BusError("org.freedesktop.DBus.Error.NoReply", "Timeout was reached")
    env.world.fail[("org.freedesktop.DBus", "ListActivatableNames")] = timeout
    env.advance(2 * TICK)
    del env.world.fail[("org.freedesktop.DBus", "ListActivatableNames")]
    env.world.set_owner("system", PPD, ":1.21")
    env.advance(2 * TICK)
    assert len(env.ha.received("capabilities")) == sent, env.ha.received("capabilities")[sent:]
    assert env.last_state()["game"] is False
    env.world.activatable["system"].clear()
    env.world.set_owner("system", PPD, None)
    env.advance(2 * TICK)
    assert env.capabilities()["modules"]["core"]["games"] == ["gamemode"]


def test_without_a_pattern_holds_are_never_read(env: Env) -> None:
    env.pair()
    env.world.set_owner("system", PPD, ":1.20")
    env.world.hold(REASON)
    env.world.login("3")
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["core"]["games"] == []
    assert env.last_state()["game"] is None
    assert not [c for c in env.world.calls if PPD in c.args]


def test_gamemode_can_be_turned_off_in_torpor_conf(env: Env) -> None:
    env.pair()
    env.set("games.gamemode", "no")
    env.world.gamemode()
    env.world.login("3")
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["core"]["games"] == []
    game(env, True)
    env.advance(1)
    assert env.last_state()["game"] is None


def test_a_bad_pattern_is_refused_by_config_set(env: Env) -> None:
    from torpor import config as conf

    with pytest.raises(conf.ConfigError):
        conf.validate("games.performance-hold", "game-(performance")
    parsed, issues = conf.parse("[games]\nperformance-hold = game-(\n")
    assert parsed.performance_hold == "" and issues[0].level == "error"


# The boost.


@pytest.mark.parametrize(("setting", "base", "boosted"), [(SAVING, 7500, 10000), (STANDARD, 4000, 7000)])
def test_a_game_gets_its_boost_once_it_settled_and_loses_it_when_it_ends(
    env: Env, caplog: pytest.LogCaptureFixture, setting: int, base: int, boosted: int
) -> None:
    """+30 in either mode, capped at 100 %, GAME_SETTLE_S after the edge, inside the 5 minute gap after a write."""
    caplog.set_level("INFO", logger="torpor")
    env.world.gamemode()
    desk(env)
    env.ddc.osd(setting)
    env.start()
    env.advance(SETTLE + 15)
    assert writes(env) == [base]
    env.advance(env.world.write_times[0] + b.EDGE_GAP_S - env.loop.mono)
    game(env, True)
    env.advance(b.GAME_SETTLE_S - 1)
    assert writes(env) == [base], "wrote during a launcher's modeset"
    env.advance(TICK + 2)
    assert writes(env) == [base, boosted]
    assert any("game)" in r.getMessage() for r in caplog.records if "brightness:" in r.getMessage())
    env.advance(5 * 60)
    game(env, False)
    env.advance(b.GAME_SETTLE_S - 1)
    assert writes(env) == [base, boosted]
    env.advance(TICK + 2)
    assert writes(env) == [base, boosted, base]
    game(env, True)
    env.advance(5)
    game(env, False)
    env.advance(60)
    assert writes(env) == [base, boosted, base], "a game shorter than the settle wrote"


def test_a_performance_hold_boosts_too(env: Env) -> None:
    env.set("games.performance-hold", "game-performance")
    env.world.set_owner("system", PPD, ":1.20")
    at_desk(env, 7500)
    env.advance(b.EDGE_GAP_S)
    env.world.hold(REASON)
    env.advance(b.GAME_SETTLE_S + TICK + 2)
    assert writes(env) == [10000]


def test_a_hold_that_keeps_flapping_writes_at_most_once_a_minute(env: Env) -> None:
    gamemode_desk(env)
    times: list[float] = []
    on = False
    for span in (12, 20, 15, 18, 13, 20, 16, 12, 19, 14) * 2:
        on = not on
        game(env, on)
        for _ in range(span):
            count = len(writes(env))
            env.advance(1)
            if len(writes(env)) > count:
                times.append(env.loop.mono)
    assert len(times) >= 3, writes(env)
    assert all(later - earlier >= b.EDGE_GAP_S for earlier, later in itertools.pairwise(times))


def test_the_boost_comes_from_home_assistant_and_0_turns_it_off(env: Env) -> None:
    gamemode_desk(env, 4000, STANDARD)
    boost(env, 10)
    game(env, True)
    env.advance(b.EDGE_GAP_S + b.GAME_SETTLE_S + TICK)
    assert writes(env)[-1] == 5000
    assert env.last_state()["brightness"]["displays"][KDE_ID]["target"] == 50
    boost(env, 0)
    env.advance(TICK + 1)
    assert env.last_state()["brightness"]["displays"][KDE_ID]["target"] == 40


def test_a_game_with_no_boost_is_no_edge(env: Env) -> None:
    """With the boost at 0 a game moves nothing, so it can't let a lux change out before the 5 minute gap."""
    env.world.gamemode()
    desk(env)
    env.ha.config["brightness"]["game_boost"] = 0
    env.start()
    env.advance(SETTLE + 15)
    assert writes(env) == [7500]
    first = env.world.write_times[0]
    lux(env, 30.0)
    env.advance(b.LUX_SETTLE_S + 1)
    game(env, True)
    env.advance(first + b.GAP_DDC_S - 2 - env.loop.mono)
    assert writes(env) == [7500], "a game with no boost let a lux change out early"
    env.advance(2 * TICK)
    assert writes(env) == [7500, 6600]


def test_game_edges_while_the_reading_keeps_changing(env: Env) -> None:
    """A reading that keeps changing holds lux-driven moves only: a game still gets its boost, and loses it,
    from the last settled reading."""
    gamemode_desk(env)
    game(env, True)
    for step in range(6):
        lux(env, 30.0 if step % 2 == 0 else 41.0)
        env.advance(45)
    assert writes(env) == [10000]
    game(env, False)
    for step in range(6):
        lux(env, 41.0 if step % 2 == 0 else 30.0)
        env.advance(45)
    assert writes(env) == [10000, 7500]
    lux(env, None)
    game(env, True)
    env.advance(b.EDGE_GAP_S + b.GAME_SETTLE_S + TICK)
    assert writes(env) == [10000, 7500, 10000]


def test_a_game_ends_a_hand_set_pause_when_it_moves_the_target_enough(env: Env) -> None:
    gamemode_desk(env, 4000, STANDARD)
    assert writes(env) == []
    slider(env, 2500)
    env.advance(modes.QUIET_S + TICK)
    assert manual(env) == 40
    game(env, True)
    env.advance(b.GAME_SETTLE_S - 1)
    assert manual(env) == 40, "a game not yet settled ended the pause"
    env.advance(TICK + 2)
    assert manual(env) is None and writes(env)[-1] == 7000


def test_no_mode_read_while_a_game_settles(env: Env) -> None:
    """A launcher's modeset sits right next to a game's edges, so a poll due then waits out GAME_SETTLE_S;
    the fake flags any call inside it in every test."""
    gamemode_desk(env)
    last = env.ddc.calls[-1].start
    env.advance(last + modes.POLL_S - 5 - env.loop.mono)
    game(env, True)
    edge = env.loop.mono
    env.advance(b.GAME_SETTLE_S + 2 * TICK)
    starts = [call.start for call in env.ddc.calls if call.start > last]
    assert starts and all(start >= edge + b.GAME_SETTLE_S for start in starts)
    assert writes(env) == [10000]


def test_a_resume_restarts_the_game_settle(env: Env) -> None:
    gamemode_desk(env)
    game(env, True)
    env.advance(b.EDGE_GAP_S)
    assert env.agent is not None and env.agent.brightness.gaming is True
    env.world.sleep(True)
    env.loop.run_due()
    env.world.sleep(False)
    env.loop.run_due()
    assert env.loop.mono - env.agent.games.since < 1
