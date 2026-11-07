"""Whether a game runs: a GameMode client, or a performance hold whose reason matches [games] performance-hold.

Signals make an edge show at once; every tick reads again, in case a signal was missed. `since` is
when the answer last changed, so brightness can wait out a game launcher's modeset next to an edge.
The sources are what is installed, not what runs, and a bus that can't say keeps the last ones: Home
Assistant removes the Game running sensor when they go, so a daemon restart must not drop them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from torpor.backends.games import GameMode, PowerProfiles
from torpor.bus import BusError
from torpor.config import Config
from torpor.logs import Failures
from torpor.runtime import Runtime

log = logging.getLogger("torpor")

GAMEMODE = "gamemode"
PERFORMANCE_HOLD = "performance_hold"


class Games:
    def __init__(self, rt: Runtime, config: Config, failures: Failures, changed: Callable[[], None]) -> None:
        self.rt = rt
        self.failures = failures
        self.changed = changed
        self.pattern = config.performance_hold
        self.gamemode = GameMode(rt.session) if rt.session is not None and config.gamemode else None
        self.profiles = PowerProfiles(rt.system) if rt.system is not None and self.pattern else None
        self.found: tuple[str, ...] = ()
        self.running: bool | None = None
        self.since = float("-inf")

    def start(self) -> None:
        if self.gamemode is not None:
            self.gamemode.watch(self.read)
        if self.profiles is not None:
            self.profiles.watch(self.read)
        self.read()

    def sources(self) -> tuple[str, ...]:
        found: list[str] = []
        try:
            if self.gamemode is not None and self.gamemode.installed():
                found.append(GAMEMODE)
            if self.profiles is not None and self.profiles.installed():
                found.append(PERFORMANCE_HOLD)
        except BusError:
            return self.found
        return tuple(found)

    def read(self) -> None:
        found = self.sources()
        running: bool | None = None
        try:
            if GAMEMODE in found and self.gamemode is not None:
                running = self.gamemode.clients() > 0
            if PERFORMANCE_HOLD in found and self.profiles is not None and not running:
                running = self.profiles.performance_held(self.pattern)
        except BusError as error:
            self.failures.fail("games", f"reading whether a game runs failed: {error.message}")
            return
        self.failures.clear("games")
        if found != self.found or running != self.running:
            if running is not None and self.running is not None and running != self.running:
                log.info("game %s", "started" if running else "ended")
            if running != self.running:
                self.since = self.rt.loop.monotonic()
            self.found, self.running = found, running
            self.changed()

    def resumed(self) -> None:
        self.since = self.rt.loop.monotonic()
        self.read()
