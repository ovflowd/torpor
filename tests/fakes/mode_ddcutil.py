"""ddcutil as the mode reader's spawn sees it, on the fake clock, ported from the single-PC agent's harness.

It answers `--version`, `detect --terse` and `getvcp --verbose` as ddcutil 3.0.2 prints them, after
the time each takes, and checks every call against the rules for reading a monitor next to
PowerDevil: only those commands with the mode options, one call at a time and SPACING_S apart, at the
desk, never within QUIET_S of a brightness change or GAME_SETTLE_S of a game edge, and no brightness
write while a call runs. Every test that starts the agent ends by checking `violations` is empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fakes.bus import World
from torpor import modes
from torpor.backends.ddcutil import MODE_OPTIONS
from torpor.modules import brightness
from torpor.runtime import Result

DETECT = """Display {number}
   I2C bus:          /dev/i2c-{bus}
   DRM connector:    {connector}
   drm_connector_id: 458
   Monitor:          {mfg}:{model}:{serial}

"""
INVALID = """Invalid display
   I2C bus:          /dev/i2c-{bus}
   DRM connector:    card1-DP-3
   Monitor:          {mfg}:{model}:{serial}

"""
GETVCP_HEADER = (
    "VCP (aka MCCS) version for display is undetected or less than 2.0. Interpretation may not be accurate.\n\n"
    "Getting data for non-table VCP code 0x{code:02x} - Manufacturer Specific:\n"
)


def getvcp_output(value: int, maximum: int = 1, opcode: int = 0xE1) -> str:
    """ddcutil 3.0.2's getvcp --verbose for a manufacturer code, copied from a read-only run on a monitor."""
    return GETVCP_HEADER.format(code=opcode) + (
        f"Raw value: opcode=0x{opcode:02x}, mh=0x{maximum >> 8:02x}, ml=0x{maximum & 0xFF:02x}, "
        f"sh=0x{value >> 8:02x}, sl=0x{value & 0xFF:02x}, max_val={maximum} (0x{maximum:04x}), "
        f"cur_val={value} (0x{value:04x})\n"
        f"VCP code 0x{opcode:02x} (Manufacturer Specific         ): mh=0x{maximum >> 8:02x}, "
        f"ml=0x{maximum & 0xFF:02x}, sh=0x{value >> 8:02x}, sl=0x{value & 0xFF:02x}\n"
    )


def retries_output(opcode: int = 0xE1) -> str:
    return GETVCP_HEADER.format(code=opcode) + (
        f"VCP code 0x{opcode:02x} (Manufacturer Specific         ): Maximum retries exceeded\n"
    )


@dataclass
class Monitor:
    bus: int
    mfg: str = "AUS"
    model: str = "PG39WCDM"
    serial: str = "SYN0001"
    connector: str = "card1-DP-1"
    vcp: dict[int, int] = field(default_factory=lambda: {0xE1: 1})
    maximum: dict[int, int] = field(default_factory=lambda: {0xE1: 1})


@dataclass
class Call:
    args: list[str]
    start: float
    end: float
    status: int | None
    failure: str | None

    @property
    def kind(self) -> str:
        return "getvcp" if self.args[len(MODE_OPTIONS)] == "--skip-ddc-checks" else "detect"


class ModeDdcutil:
    def __init__(self, world: World, state: Callable[[], str | None]) -> None:
        self.world = world
        self.state = state
        self.version: str | None = "3.0.2"
        self.monitors = [Monitor(9)]
        self.invalid: list[Monitor] = []
        self.delay = 0.12
        self.detect_delay = 0.46
        self.fail: list[str] = []
        self.broken: str | None = None
        self.calls: list[Call] = []
        self.versions = 0
        self.violations: list[str] = []

    def __call__(self, argv: list[str]) -> tuple[float, Result]:
        args = argv[1:]
        now = self.world.loop.mono
        if args == ["--version"]:
            self.versions += 1
            if self.version is None:
                return 0.01, Result(1, "ddcutil: error while loading shared libraries: libddcutil.so.5\n")
            return 0.01, Result(0, f"ddcutil {self.version}\n")
        self.check(args, now)
        failure = self.fail.pop(0) if self.fail else self.broken
        delay, result = self.answer(args, failure)
        end = now + min(delay, modes.TIMEOUT_S)
        self.calls.append(Call(args, now, end, result.code if delay < modes.TIMEOUT_S else None, failure))
        return delay, result

    def check(self, args: list[str], now: float) -> None:
        options = list(MODE_OPTIONS)
        detect = args == [*options, "detect", "--terse"]
        tail = args[len(options) :]
        getvcp = (
            args[: len(options)] == options
            and len(tail) == 6
            and tail[:3] == ["--skip-ddc-checks", "getvcp", "--bus"]
            and tail[3].isdigit()
            and tail[4] == "--verbose"
        )
        if not detect and not getvcp:
            self.violations.append(f"ddcutil {args}")
        if any(call.end > now for call in self.calls):
            self.violations.append(f"ddcutil {args} at {now} while another call ran")
        elif self.calls and now - self.calls[-1].end < modes.SPACING_S - 1e-6:
            self.violations.append(f"ddcutil {args} {now - self.calls[-1].end:.3f} s after the last call ended")
        recent = [t for t in self.world.changes if now - t < modes.QUIET_S]
        if recent:
            self.violations.append(f"ddcutil {args} {now - max(recent):.1f} s after a brightness change")
        state = self.state()
        if state != "in_use":
            self.violations.append(f"ddcutil {args} in state {state}")
        if self.world.game_edges and now - self.world.game_edges[-1] < brightness.GAME_SETTLE_S:
            self.violations.append(f"ddcutil {args} {now - self.world.game_edges[-1]:.1f} s after a game edge")

    def answer(self, args: list[str], failure: str | None) -> tuple[float, Result]:
        if failure == "timeout":
            return 60.0, Result(0, "")
        if "detect" in args:
            out = [DETECT.format(number=n, **vars(m)) for n, m in enumerate(self.monitors, 1)]
            out += [INVALID.format(**vars(m)) for m in self.invalid]
            if failure == "no-monitor":
                out = [INVALID.format(**vars(m)) for m in self.monitors]
            return self.detect_delay, Result(0, "".join(out) or "No displays found.\n")
        bus, code = int(args[-3]), int(args[-1], 16)
        monitor = next((m for m in self.monitors if m.bus == bus), None)
        if monitor is None:
            return self.delay, Result(1, f"Display not found on bus {bus}\n")
        if failure == "retries":
            return self.delay, Result(1, retries_output(code))
        if failure == "greyed":
            return self.delay, Result(0, getvcp_output(0xFE, 0xFE, code))
        if failure == "other-code":
            return self.delay, Result(0, getvcp_output(1, 1, 0xDD))
        return self.delay, Result(0, getvcp_output(monitor.vcp.get(code, 0), monitor.maximum.get(code, 0xFF), code))

    def osd(self, value: int, code: int = 0xE1, bus: int | None = None) -> None:
        """The mode changed in the monitor's own OSD, which nothing on the PC hears of."""
        for monitor in self.monitors:
            if bus is None or monitor.bus == bus:
                monitor.vcp[code] = value

    def reads(self) -> list[Call]:
        return [call for call in self.calls if call.kind == "getvcp"]

    def kinds(self) -> list[str]:
        return [call.kind for call in self.calls]

    def writes_during_calls(self) -> list[str]:
        return [
            f"a brightness write at {at} while ddcutil {call.args} ran"
            for call in self.calls
            for at in self.world.write_times
            if call.start < at < call.end
        ]

    def summary(self) -> dict[str, Any]:
        return {"calls": [(c.kind, c.start, c.end, c.status, c.failure) for c in self.calls]}
