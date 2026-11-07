"""status.json: what the agent tells `torporctl status`, written at most once per second."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from torpor.paths import atomic_write, ensure_dir
from torpor.runtime import Loop

log = logging.getLogger("torpor")
MIN_INTERVAL_S = 1.0


class StatusWriter:
    def __init__(self, path: Path, loop: Loop, build: Callable[[], dict[str, Any]], *, enabled: bool = True) -> None:
        self.path = path
        self.loop = loop
        self.build = build
        self.enabled = enabled
        self.last: float | None = None
        self.timer: object | None = None

    def changed(self) -> None:
        if self.timer is not None or not self.enabled:
            return
        now = self.loop.monotonic()
        wait = 0.0 if self.last is None else max(0.0, self.last + MIN_INTERVAL_S - now)
        self.timer = self.loop.call_later(wait, self.write)

    def write(self) -> None:
        self.loop.cancel(self.timer)
        self.timer = None
        if not self.enabled:
            return
        self.last = self.loop.monotonic()
        try:
            ensure_dir(self.path.parent)
            atomic_write(self.path, json.dumps(self.build(), indent=2, sort_keys=True))
        except OSError as error:
            log.warning("writing %s failed: %s", self.path, error.strerror)


def read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
