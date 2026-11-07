"""What every module shares: the context it runs in, HA's settings, command errors and problems."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from torpor.bootstate import BootState
from torpor.config import Config
from torpor.curve import GAME_BOOST, Curve
from torpor.known import Known
from torpor.logs import Failures
from torpor.model import Facts
from torpor.modes import ModeSetting, settings_from_config
from torpor.paths import Paths
from torpor.runtime import Runtime

Reply = Callable[[dict[str, Any] | None, "CommandError | None"], None]


class CommandError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def wire(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def problem(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "hint": hint}


@dataclass
class HaConfig:
    """The last `config` from Home Assistant. Until one arrives, every module counts as wanted."""

    modules: dict[str, bool] = field(default_factory=dict)
    lux: bool = False
    curve: Curve = field(default_factory=Curve)
    auto: dict[str, bool] = field(default_factory=dict)
    game_boost: float = GAME_BOOST
    modes: dict[str, ModeSetting] = field(default_factory=dict)
    received: bool = False

    def wants(self, module: str) -> bool:
        return self.modules.get(module, True)

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> HaConfig:
        modules = message.get("modules")
        brightness = message.get("brightness")
        brightness = brightness if isinstance(brightness, dict) else {}
        auto = brightness.get("auto")
        boost = brightness.get("game_boost", GAME_BOOST)
        if isinstance(boost, bool) or not isinstance(boost, int | float) or not 0 <= boost <= 100:
            boost = GAME_BOOST
        return cls(
            modules={str(k): v for k, v in modules.items() if isinstance(v, bool)} if isinstance(modules, dict) else {},
            lux=brightness.get("lux") is True,
            curve=Curve.from_config(brightness.get("curve")),
            auto={str(k): v for k, v in auto.items() if isinstance(v, bool)} if isinstance(auto, dict) else {},
            game_boost=float(boost),
            modes=settings_from_config(brightness.get("modes")),
            received=True,
        )


@dataclass
class Context:
    rt: Runtime
    config: Config
    paths: Paths
    known: Known
    boot: BootState
    failures: Failures
    observe_only: bool
    changed: Callable[[], None]
    facts: Callable[[], Facts | None]
    refresh: Callable[[], Facts | None]
    down: Callable[[], bool]
    ha: HaConfig = field(default_factory=HaConfig)
    ha_address: str | None = None

    @property
    def uid(self) -> int:
        return self.rt.uid


class Module:
    id = ""
    commands: tuple[str, ...] = ()

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    @property
    def enabled(self) -> bool:
        return self.ctx.config.modules.get(self.id, True)

    @property
    def active(self) -> bool:
        """Enabled on the PC, wanted by Home Assistant, and allowed to act (not observe-only)."""
        return self.enabled and self.ctx.ha.wants(self.id) and not self.ctx.observe_only

    def start(self) -> None:
        return None

    def tick(self, facts: Facts) -> None:
        return None

    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def state(self) -> dict[str, Any] | None:
        return None

    def command(self, name: str, args: dict[str, Any], reply: Reply) -> None:
        raise CommandError("bad_request", f"unknown command {name}")

    def configure(self) -> None:
        return None

    def resumed(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def disabled_capabilities(self) -> dict[str, Any]:
        return {"enabled": False, "available": False, "backend": None, "problems": []}
