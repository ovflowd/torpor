"""state.json: what survives an agent restart but not a reboot (keep-awake end, brightness overrides)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from torpor.paths import atomic_write, boot_id

log = logging.getLogger("torpor")


class BootState:
    def __init__(self, path: Path, proc: Path) -> None:
        self.path = path
        self.boot = boot_id(proc)
        self.data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and self.boot is not None and data.get("boot") == self.boot:
            self.data = {key: value for key, value in data.items() if key != "boot"}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def put(self, key: str, value: Any) -> None:
        if self.data.get(key) == value:
            return
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value
        try:
            atomic_write(self.path, json.dumps({"boot": self.boot, **self.data}, sort_keys=True))
        except OSError as error:
            log.warning("saving %s failed: %s", self.path, error.strerror)
