"""known.json: backends that worked once and displays seen lately, so modules and entities don't flap."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from torpor.paths import atomic_write

DISPLAY_DAYS = 30
log = logging.getLogger("torpor")


def over_ddc(backend: str, internal: bool) -> bool:
    """An external monitor through Plasma or ddcutil: one whose modes Torpor can read over DDC/CI."""
    return backend == "ddcutil" or (backend == "kde" and not internal)


@dataclass(frozen=True)
class KnownDisplay:
    id: str
    label: str
    backend: str
    internal: bool
    seen: float

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "backend": self.backend,
            "internal": self.internal,
            "modes": over_ddc(self.backend, self.internal),
        }


class Known:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        self.backends: dict[str, list[str]] = {}
        self.displays: dict[str, KnownDisplay] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        backends = data.get("backends")
        if isinstance(backends, dict):
            self.backends = {
                str(module): [str(name) for name in names]
                for module, names in backends.items()
                if isinstance(names, list)
            }
        displays = data.get("displays")
        if isinstance(displays, dict):
            for key, value in displays.items():
                try:
                    self.displays[key] = KnownDisplay(
                        key, str(value["label"]), str(value["backend"]), bool(value["internal"]), float(value["seen"])
                    )
                except (KeyError, TypeError, ValueError):
                    continue

    def save(self) -> None:
        if self.readonly:
            return
        data = {
            "backends": self.backends,
            "displays": {
                key: {"label": d.label, "backend": d.backend, "internal": d.internal, "seen": d.seen}
                for key, d in self.displays.items()
            },
        }
        try:
            atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True))
        except OSError as error:
            log.warning("saving %s failed: %s", self.path, error.strerror)

    def worked(self, module: str, backend: str) -> bool:
        return backend in self.backends.get(module, [])

    def remember(self, module: str, backend: str) -> None:
        names = self.backends.setdefault(module, [])
        if backend not in names:
            names.append(backend)
            self.save()

    def seen(self, display: KnownDisplay) -> None:
        old = self.displays.get(display.id)
        self.displays[display.id] = display
        if old is None or old.label != display.label or display.seen - old.seen > 3600:
            self.save()

    def recent(self, now: float) -> list[KnownDisplay]:
        cutoff = now - DISPLAY_DAYS * 86400
        return [d for d in self.displays.values() if d.seen >= cutoff]
