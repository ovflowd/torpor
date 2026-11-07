"""The ASUS PG39WCDM at the desk through Plasma, the mode tests' set-up, shared with the game tests.

Its OSD Power Setting is VCP 0xE1: 0x00 Standard, 0x01 Power Saving, anchors 40 and 75 % at 84 lx.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from torpor.modules import brightness as b

if TYPE_CHECKING:
    from conftest import Env

LABEL = "ASUSTek COMPUTER INC PG39WCDM"
KDE_ID = b.display_id("kde", LABEL)
CURVE = {"anchor_lux": 84, "anchor_pct": 40, "slope": 20, "floor": 15, "ceiling": 100}
STANDARD, SAVING = 0x00, 0x01
VALUES = [
    {"value": STANDARD, "anchor_pct": 40, "label": "Standard"},
    {"value": SAVING, "anchor_pct": 75, "label": "Power Saving"},
]
SETTLE = b.DESK_SETTLE_S
TICK = 10


def config(did: str = KDE_ID, *, auto: bool = True, values: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "brightness": {
            "lux": True,
            "curve": CURVE,
            "auto": {did: auto},
            "game_boost": 30,
            "modes": {did: {"code": 0xE1, "values": VALUES if values is None else values}},
        },
    }


def desk(env: Env, brightness: int = 5000, **kwargs: Any) -> Env:
    """The PG39WCDM at the desk through Plasma, the room at 84 lx for an hour; not started yet."""
    env.pair()
    env.tree.i2c()
    env.world.displays["display0"] = {"Label": LABEL, "Brightness": brightness, "MaxBrightness": 10000}
    env.desk()
    env.ha.config = config(**kwargs)
    env.ha.lux = (84.0, 3600.0)
    return env


def at_desk(env: Env, brightness: int = 5000, **kwargs: Any) -> Env:
    """Started, the desk settled, the mode read and the first write done."""
    desk(env, brightness, **kwargs)
    env.start()
    env.advance(SETTLE + 15)
    return env


def writes(env: Env) -> list[int]:
    return [c.args[0] for c in env.world.writes("SetBrightnessWithContext")]


def lux(env: Env, value: float | None) -> None:
    env.ha.input(value, 0.0)
    env.loop.run_due()


def slider(env: Env, value: int, name: str = "display0", context: str = "") -> None:
    env.world.slider(name, value, context)
    env.loop.run_due()


def manual(env: Env, did: str = KDE_ID) -> Any:
    assert env.agent is not None
    return env.agent.brightness.displays[did].manual
