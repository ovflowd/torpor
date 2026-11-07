"""DDC/CI through the ddcutil command: detect, read and write VCP 0x10 (brightness), and read a mode code.

Every brightness call runs in the agent's worker thread, one at a time, with a 15 s timeout and one
retry after 2 s. The output formats cover ddcutil 1.4 (Ubuntu 24.04) to 3.0.

Mode reads (`modes.py`) only read. They run with options no ddcutilrc can override, so the
cross-instance lock that PowerDevil's libddcutil also takes stays on, and they need ddcutil 2.2 or
later: 1.4 has none of these options, and 2.1 had the lock off by default.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from torpor.runtime import Result, run

TIMEOUT_S = 15
RETRY_AFTER_S = 2
BRIGHTNESS_VCP = "10"

_TERSE = re.compile(r"^VCP\s+10\s+C\s+(\d+)\s+(\d+)\s*$", re.MULTILINE | re.IGNORECASE)
_VERBOSE = re.compile(r"current value\s*=\s*(\d+)\s*,\s*max value\s*=\s*(\d+)", re.IGNORECASE)
_VERSION = re.compile(r"ddcutil\s+(\d+)\.(\d+)(?:\.(\d+))?")
_RAW_VALUE = re.compile(
    r"^Raw value: opcode=0x([0-9a-f]{2}), mh=0x([0-9a-f]{2}), ml=0x([0-9a-f]{2}), sh=0x([0-9a-f]{2}), "
    r"sl=0x([0-9a-f]{2})\b",
    re.IGNORECASE | re.MULTILINE,
)

MODE_OPTIONS = ("--noconfig", "--disable-udf", "--enable-cross-instance-locks", "--disable-dynamic-sleep")
MODE_MIN_VERSION = (2, 2, 0)


class DdcError(Exception):
    pass


@dataclass(frozen=True)
class DdcDisplay:
    bus: int
    mfg: str
    model: str
    serial: str
    connector: str = ""

    @property
    def input(self) -> str:
        return f"{self.mfg}:{self.model}:{self.serial}" if self.serial else f"i2c-{self.bus}"

    @property
    def label(self) -> str:
        return self.model or self.mfg or f"i2c-{self.bus}"


def parse_detect(output: str) -> list[DdcDisplay]:
    displays: list[DdcDisplay] = []
    fields: dict[str, str] | None = None

    def finish() -> None:
        if fields and "bus" in fields and "monitor" in fields:
            mfg, _, rest = fields["monitor"].partition(":")
            model, _, serial = rest.partition(":")
            displays.append(
                DdcDisplay(int(fields["bus"]), mfg.strip(), model.strip(), serial.strip(), fields.get("connector", ""))
            )

    for line in output.splitlines():
        if re.fullmatch(r"Display \d+\s*", line):
            finish()
            fields = {}
            continue
        if line[:1].strip():
            finish()
            fields = None
            continue
        if fields is None:
            continue
        if match := re.fullmatch(r"\s+I2C bus:\s+/dev/i2c-(\d+)\s*", line):
            fields["bus"] = match.group(1)
        elif match := re.fullmatch(r"\s+DRM connector:\s+(\S+)\s*", line):
            fields["connector"] = match.group(1)
        elif match := re.fullmatch(r"\s+Monitor:\s+(.*?)\s*", line):
            fields["monitor"] = match.group(1)
    finish()
    return displays


def parse_brightness(output: str) -> tuple[int, int] | None:
    match = _TERSE.search(output) or _VERBOSE.search(output)
    if match is None:
        return None
    current, maximum = int(match.group(1)), int(match.group(2))
    return (current, maximum) if maximum > 0 else None


def parse_version(output: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(output)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def mode_detect_args() -> list[str]:
    return [*MODE_OPTIONS, "detect", "--terse"]


def mode_read_args(bus: int, code: int) -> list[str]:
    return [*MODE_OPTIONS, "--skip-ddc-checks", "getvcp", "--bus", str(bus), "--verbose", f"0x{code:02X}"]


def parse_raw_value(output: str, code: int) -> tuple[int, int] | str:
    """(value, maximum) from `getvcp --verbose`, or why the reply doesn't count."""
    replies = _RAW_VALUE.findall(output)
    if len(replies) != 1:
        return "more than one reply" if replies else "no reply"
    opcode, mh, ml, sh, sl = (int(byte, 16) for byte in replies[0])
    if opcode != code:
        return f"a reply for 0x{opcode:02x}, not 0x{code:02x}"
    return sh << 8 | sl, mh << 8 | ml


def last_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


class Ddcutil:
    def __init__(
        self,
        binary: str,
        *,
        runner: Callable[..., Result] = run,
        sleep: Callable[[float], None] = time.sleep,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.binary = binary
        self.env = env
        self._run = runner
        self._sleep = sleep
        self._lock = threading.Lock()

    def _call(self, what: str, args: list[str], check: Callable[[Result], bool]) -> Result:
        with self._lock:
            result = self._run([self.binary, *args], timeout=TIMEOUT_S, env=self.env)
            if check(result):
                return result
            self._sleep(RETRY_AFTER_S)
            result = self._run([self.binary, *args], timeout=TIMEOUT_S, env=self.env)
        if not check(result):
            raise DdcError(f"ddcutil {what} failed: {last_line(result.output)}")
        return result

    def version(self) -> tuple[int, int, int] | None:
        result = self._run([self.binary, "--version"], timeout=TIMEOUT_S, env=self.env)
        return parse_version(result.output) if result.ok else None

    def detect(self) -> list[DdcDisplay]:
        result = self._call("detect", ["detect", "--terse"], lambda r: r.ok)
        return parse_detect(result.output)

    def get(self, bus: int) -> tuple[int, int]:
        result = self._call(
            "getvcp",
            ["--bus", str(bus), "getvcp", BRIGHTNESS_VCP, "--terse"],
            lambda r: r.ok and parse_brightness(r.output) is not None,
        )
        reading = parse_brightness(result.output)
        assert reading is not None
        return reading

    def set(self, bus: int, value: int) -> None:
        self._call("setvcp", ["--bus", str(bus), "setvcp", BRIGHTNESS_VCP, str(value)], lambda r: r.ok)
