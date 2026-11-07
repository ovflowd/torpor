"""The automatic brightness curve: a light sensor reports on a log scale, so the curve is one too."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

GAME_BOOST = 30
GAME_CEILING = 100


@dataclass(frozen=True)
class Curve:
    anchor_lux: float = 100.0
    anchor_pct: float = 40.0
    slope: float = 25.0
    floor: float = 10.0
    ceiling: float = 80.0

    def target(self, lux: float, *, anchor_pct: float | None = None, boost: float = 0) -> int:
        """`anchor_pct` is a monitor mode's anchor in place of the curve's own; `boost` is added on top of the
        ceiling while a game runs, up to 100 %."""
        anchor = self.anchor_pct if anchor_pct is None else anchor_pct
        percent = anchor + self.slope * math.log10(max(lux, 1.0) / self.anchor_lux)
        percent = min(max(percent, self.floor), self.ceiling)
        return round(min(percent + boost, GAME_CEILING) if boost else percent)

    @classmethod
    def from_config(cls, data: Mapping[str, Any] | None) -> Curve:
        """The curve from HA's `config`; anything missing or out of range keeps its default."""
        if not isinstance(data, Mapping):
            return cls()
        default = cls()
        limits = {
            "anchor_lux": (1.0, 100000.0),
            "anchor_pct": (0.0, 100.0),
            "slope": (0.0, 100.0),
            "floor": (0.0, 100.0),
            "ceiling": (0.0, 100.0),
        }
        values: dict[str, float] = {}
        for name, (low, high) in limits.items():
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
                values[name] = getattr(default, name)
            else:
                values[name] = float(value)
        if values["ceiling"] < values["floor"]:
            values["floor"], values["ceiling"] = default.floor, default.ceiling
        return cls(**values)
