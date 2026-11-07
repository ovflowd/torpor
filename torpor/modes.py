"""Monitor modes: one VCP code per display, read with ddcutil and never written, picks that display's anchor.

Some monitors change how bright a percentage looks with an OSD setting (the ASUS PG39WCDM's Power
Setting, VCP 0xE1), so Home Assistant can give each value of that code its own anchor. The reads are
the single-PC agent's, generalised:

- One ddcutil call at a time, never while a brightness write runs, and never within QUIET_S of a
  brightness change, a display coming or going, or a new brightness backend. PowerDevil writes 50 ms
  after a change and retries a failed write 1, 2 and 3 s later; each attempt waits at least 3 s for
  the bus lock, so 30 s covers the whole cascade.
- ddcutil is killed after TIMEOUT_S, under libddcutil's 3 s lock wait, so a PowerDevil write queued
  behind a read never fails for it.
- Each call starts at least SPACING_S after the last one ended, so a write waiting for the lock gets it
  between detect and the read.
- A failed read keeps the last known mode (or, never known, the lowest anchor), drops the bus for a
  fresh detect, and backs off from RETRY_S, doubling up to RETRY_MAX_S. After TRIES failures in a row
  writes stop waiting for a read. A resume or a renumbered display starts afresh.
- A clean reply with a value none of the rows has is no failure: the bus answered. The last known mode
  stays, and the error names the value, which may be a mode Home Assistant has no row for yet.
- A failed `ddcutil --version` is asked again on the same back-off, and after a resume.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from torpor.backends.ddcutil import (
    MODE_MIN_VERSION,
    DdcDisplay,
    mode_detect_args,
    mode_read_args,
    parse_detect,
    parse_raw_value,
    parse_version,
)
from torpor.bootstate import BootState
from torpor.logs import Failures
from torpor.runtime import Result, Runtime, run

log = logging.getLogger("torpor")

TIMEOUT_S = 2.5
QUIET_S = 30
POLL_S = 120
FRESH_S = 15
RETRY_S = 60
RETRY_MAX_S = 1800
TRIES = 3
SPACING_S = 0.1
_DISPLAY_ID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class Mode:
    """One value of the code: the anchor it gives the curve, and what Home Assistant calls it."""

    anchor_pct: float
    label: str = ""


@dataclass(frozen=True)
class ModeSetting:
    code: int
    values: Mapping[int, Mode] = field(default_factory=dict)

    @property
    def lowest(self) -> float | None:
        return min((mode.anchor_pct for mode in self.values.values()), default=None)

    def name(self, value: int) -> str:
        mode = self.values.get(value)
        return f"0x{value:02x}" + (f" ({mode.label})" if mode is not None and mode.label else "")


@dataclass(frozen=True)
class Target:
    """The display a read is for, as ddcutil detect lists it."""

    id: str
    label: str
    matches: Callable[[DdcDisplay], bool]


@dataclass
class ModeState:
    setting: ModeSetting
    value: int | None = None
    read_at: float | None = None
    stale: bool = True
    bus: int | None = None
    failures: int = 0
    next_at: float | None = None
    error: str | None = None
    gen: int = 0


def _number(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        return None
    return float(value)


def settings_from_config(data: Any) -> dict[str, ModeSetting]:
    """`brightness.modes` from HA's `config`. A display with a bad code is left out; a bad value is skipped."""
    settings: dict[str, ModeSetting] = {}
    for did, item in data.items() if isinstance(data, Mapping) else ():
        if not isinstance(did, str) or not _DISPLAY_ID.fullmatch(did) or not isinstance(item, Mapping):
            continue
        code = item.get("code")
        if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 0xFF:
            continue
        values: dict[int, Mode] = {}
        for row in item.get("values") or ():
            if not isinstance(row, Mapping):
                continue
            value, anchor = row.get("value"), _number(row.get("anchor_pct"), 0, 100)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF or anchor is None:
                continue
            label = row.get("label")
            values[value] = Mode(anchor, label if isinstance(label, str) else "")
        settings[did] = ModeSetting(code, values)
    return settings


def label_matches(label: str, found: DdcDisplay) -> bool:
    """Plasma labels a monitor "<maker> <model>", and ddcutil knows the model from its EDID."""
    model = " ".join(found.model.lower().split())
    words = " ".join(label.lower().split())
    return bool(model) and (words == model or words.endswith(f" {model}"))


def i2c_access(dev: Path) -> bool:
    return any(os.access(device, os.R_OK | os.W_OK) for device in dev.glob("i2c-*"))


class ModeReader:
    def __init__(
        self,
        rt: Runtime,
        binary: str,
        boot: BootState,
        failures: Failures,
        *,
        found: Callable[[str, int | None, int | None], None],
        again: Callable[[], None],
    ) -> None:
        self.rt = rt
        self.binary = binary
        self.boot = boot
        self.failures = failures
        self.found = found
        self.again = again
        self.dev = Path(rt.env.get("TORPOR_DEV") or "/dev")
        self.version: tuple[int, int, int] | None = None
        self.checked = False
        self.access = False
        self.states: dict[str, ModeState] = {}
        self.running: str | None = None
        self.spacer: object | None = None
        self.quiet_at: float | None = None
        self.waiting: list[Callable[[], None]] = []
        self.versioning = False
        self.version_failures = 0
        self.version_at: float | None = None

    def start(self) -> None:
        self.access = i2c_access(self.dev)
        self.ask_version()

    def stop(self) -> None:
        self.rt.loop.cancel(self.spacer)
        self.spacer = None

    def ask_version(self) -> None:
        if not self.binary or self.versioning:
            return
        self.versioning = True
        self.version_at = None
        self.rt.loop.spawn([self.binary, "--version"], timeout=TIMEOUT_S, env=self.rt.env, done=self.versioned)

    def check_now(self) -> None:
        """The same check without the main loop, for `torporctl pair`'s one-off look."""
        self.access = i2c_access(self.dev)
        if self.binary:
            self.versioned(run([self.binary, "--version"], timeout=TIMEOUT_S, env=self.rt.env))

    def versioned(self, result: Result) -> None:
        self.versioning = False
        self.checked = True
        self.version = parse_version(result.output) if result.ok else None
        if self.version is None:
            self.version_failures += 1
            delay = min(RETRY_S * 2 ** (self.version_failures - 1), RETRY_MAX_S)
            self.version_at = self.rt.loop.monotonic() + delay
        else:
            self.version_failures = 0
        self.note()

    def tick(self) -> None:
        self.access = i2c_access(self.dev)
        if self.version_at is not None and self.rt.loop.monotonic() >= self.version_at:
            self.ask_version()
        self.note()

    def note(self) -> None:
        """Say once why a mode Home Assistant asked for can't be read."""
        problems = self.problems() if self.states and (self.checked or not self.binary) else []
        if problems:
            self.failures.fail(
                "modes", f"monitor modes can't be read: {problems[0][1]}; brightness uses the lowest anchor"
            )
        else:
            self.failures.clear("modes")

    # What Home Assistant asked for.

    def configure(self, settings: Mapping[str, ModeSetting]) -> None:
        kept = self.boot.get("modes") or {}
        for did in list(self.states):
            if did not in settings:
                del self.states[did]
        for did, setting in settings.items():
            state = self.states.get(did)
            if state is not None and state.setting.code == setting.code:
                state.setting = setting
                continue
            state = self.states[did] = ModeState(setting)
            saved = kept.get(did) if isinstance(kept, dict) else None
            if isinstance(saved, dict) and saved.get("code") == setting.code and isinstance(saved.get("value"), int):
                state.value = saved["value"]
        self.save()
        self.note()

    def wants(self, did: str) -> bool:
        return did in self.states

    def problems(self) -> list[tuple[str, str]]:
        """Why this PC can't read a monitor's mode: (code, message)."""
        if not self.binary:
            return [("no_ddcutil", "ddcutil is not installed")]
        if self.checked and self.version is None:
            return [("ddcutil_failed", "ddcutil --version failed")]
        if self.version is not None and self.version < MODE_MIN_VERSION:
            found = ".".join(map(str, self.version))
            return [("old_ddcutil", f"ddcutil {found} can't share the bus safely; mode reads need 2.2 or later")]
        if not self.access:
            return [("no_i2c", "no /dev/i2c-* is readable and writable for this user")]
        return []

    @property
    def possible(self) -> bool:
        return self.checked and not self.problems()

    # The schedule.

    def due(self, did: str, now: float) -> bool:
        state = self.states.get(did)
        if state is None or not self.possible:
            return False
        return state.stale or state.read_at is None or now - state.read_at >= POLL_S

    def settled(self, did: str) -> bool:
        """The mode is as good as it gets: read since it went stale, or no read can tell more."""
        state = self.states.get(did)
        return state is None or not self.possible or not state.stale or state.failures >= TRIES

    def fresh(self, did: str, now: float) -> bool:
        """Whether a write may go ahead with the mode as known. A read failing means a bus or a monitor
        that would fail the write too, so the write waits for the retry, up to TRIES failures."""
        if self.running is not None:
            return False
        state = self.states.get(did)
        if state is None or not self.possible or state.failures >= TRIES:
            return True
        return not state.stale and state.read_at is not None and now - state.read_at < FRESH_S

    def quiet(self) -> None:
        self.quiet_at = self.rt.loop.monotonic()

    def stale(self, did: str) -> None:
        """Something may have changed the mode, or he set the brightness by hand: a read still running may
        have caught the monitor before that, so it doesn't count."""
        state = self.states.get(did)
        if state is not None:
            state.stale = True
            state.gen += 1

    def reprobe(self, did: str | None = None) -> None:
        """Back from a suspend or renumbered: find the bus again, with no back-off left over. A resume also
        asks ddcutil for its version again, which may have been updated meanwhile."""
        for key, state in self.states.items():
            if did is None or key == did:
                state.bus = None
                state.stale = True
                state.failures = 0
                state.next_at = None
                state.gen += 1
        if did is None:
            self.version_failures = 0
            self.ask_version()

    def read(self, target: Target, now: float) -> bool:
        """Start one ddcutil call for this display if the gates allow; whether one started."""
        state = self.states.get(target.id)
        if state is None or self.running is not None or self.spacer is not None or not self.possible:
            return False
        if state.next_at is not None and now < state.next_at:
            return False
        if self.quiet_at is not None and now - self.quiet_at < QUIET_S:
            return False
        gen = state.gen
        detect = state.bus is None
        args = mode_detect_args() if state.bus is None else mode_read_args(state.bus, state.setting.code)

        def finished(result: Result) -> None:
            self.running = None
            current = self.states.get(target.id) is state and state.gen == gen
            if current and detect:
                self.detected(target, result)
            elif current:
                self.answered(target, result)
            waiting, self.waiting = self.waiting, []
            for write in waiting:
                write()
            self.spacer = self.rt.loop.call_later(SPACING_S, self.spaced)
            if not detect or state.bus is None:
                self.again()

        self.running = target.id
        self.rt.loop.spawn([self.binary, *args], timeout=TIMEOUT_S, env=self.rt.env, done=finished)
        return True

    def spaced(self) -> None:
        self.spacer = None
        self.again()

    def after_read(self, write: Callable[[], None]) -> bool:
        """Hold a write until the running read ends; False when none runs."""
        if self.running is None:
            return False
        self.waiting.append(write)
        return True

    def detected(self, target: Target, result: Result) -> None:
        if not result.ok:
            self.failed(target, f"ddcutil detect failed ({said(result)})")
            return
        buses = [found.bus for found in parse_detect(result.output) if target.matches(found)]
        if len(buses) != 1:
            many = f"{len(buses)} DDC/CI displays match" if buses else "no working DDC/CI display matches"
            self.failed(target, f"ddcutil detect: {many} {target.label}")
            return
        self.states[target.id].bus = buses[0]

    def answered(self, target: Target, result: Result) -> None:
        state = self.states[target.id]
        if not result.ok:
            reason = "ddcutil getvcp timed out" if result.code is None else f"ddcutil getvcp failed ({said(result)})"
            self.failed(target, reason)
            return
        reply = parse_raw_value(result.output, state.setting.code)
        if isinstance(reply, str):
            self.failed(target, f"implausible reply: {reply}")
            return
        value, _maximum = reply
        state.failures, state.next_at = 0, None
        state.read_at = self.rt.loop.monotonic()
        state.stale = False
        if state.setting.values and value not in state.setting.values:
            state.error = f"the monitor answers 0x{value:02x}, none of the values set in Home Assistant"
            known = state.setting.name(state.value) if state.value is not None else "the lowest anchor"
            self.failures.fail(f"mode-{target.id}", f"{target.label}: {state.error}; brightness keeps {known}")
            return
        self.failures.clear(f"mode-{target.id}")
        state.error = None
        if value != state.value:
            before = state.value
            state.value = value
            log.info("%s mode: %s", target.label, state.setting.name(value))
            self.save()
            self.found(target.id, before, value)

    def failed(self, target: Target, reason: str) -> None:
        state = self.states[target.id]
        state.bus = None
        state.failures += 1
        delay = min(RETRY_S * 2 ** (state.failures - 1), RETRY_MAX_S)
        state.next_at = self.rt.loop.monotonic() + delay
        state.error = reason
        known = state.setting.name(state.value) if state.value is not None else "the lowest anchor"
        self.failures.fail(
            f"mode-{target.id}",
            f"{target.label}: reading its mode failed: {reason}; brightness keeps {known}, next try in {delay} s",
        )

    # What the curve and Home Assistant see.

    def anchored(self, did: str) -> bool:
        """Whether this display's values give anchors, so that a new mode moves its curve."""
        state = self.states.get(did)
        return state is not None and bool(state.setting.values)

    def anchor(self, did: str) -> float | None:
        """This display's anchor: its mode's, the lowest while the mode is unknown, None without modes."""
        state = self.states.get(did)
        if state is None:
            return None
        mode = state.setting.values.get(state.value) if state.value is not None else None
        return mode.anchor_pct if mode is not None else state.setting.lowest

    def name(self, did: str) -> str | None:
        state = self.states.get(did)
        return state.setting.name(state.value) if state is not None and state.value is not None else None

    def report(self, did: str) -> dict[str, Any] | None:
        state = self.states.get(did)
        if state is None:
            return None
        return {"value": state.value, "error": state.error}

    def save(self) -> None:
        kept = {
            did: {"code": s.setting.code, "value": s.value} for did, s in self.states.items() if s.value is not None
        }
        self.boot.put("modes", kept or None)


def said(result: Result) -> str:
    lines = [" ".join(line.split()) for line in result.output.splitlines() if line.strip()]
    return f"exit {result.code}: {lines[-1]}" if lines else f"exit {result.code}, no output"
