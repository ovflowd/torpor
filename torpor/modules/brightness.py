"""brightness: displays through KDE, ddcutil or logind, and automatic brightness from HA's lux reading.

The gates and the hand-change rules are ported from the single-PC agent Torpor grew out of: writes
are rare (settled readings only, 3-point steps, minutes apart), a change the agent didn't make
pauses automatic brightness until the room changes enough, and a failed write stops it.

A monitor's mode (`modes.py`) can move its curve's anchor, and a running game adds a boost. A boost
coming or going, or a new anchor, is an edge: it needs no settled reading and only EDGE_GAP_S since
the last write, so a hold that keeps flapping still writes at most once a minute. Nothing is written
or read within GAME_SETTLE_S of a game edge, whatever the boost: a launcher may switch the refresh
rate (a full modeset) right next to it, and DDC/CI may not answer then. Nothing is written or read
once the PC is going down or the agent stops, even when a read that ends then would ask for a write.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from torpor.backends import sysfs
from torpor.backends.ddcutil import DdcDisplay, DdcError, Ddcutil
from torpor.backends.kde import BRIGHTNESS as KDE_BRIGHTNESS
from torpor.backends.kde import CONTEXT, KdeDisplay, ScreenBrightness
from torpor.backends.logind import Logind
from torpor.bus import BusError
from torpor.games import Games
from torpor.known import KnownDisplay, over_ddc
from torpor.model import IN_USE, Facts
from torpor.modes import ModeReader, Target, label_matches
from torpor.modules.base import CommandError, Context, Module, Reply, problem

log = logging.getLogger("torpor")

KDE_NAME = KDE_BRIGHTNESS[0]
PENDING = "pending"
DESK_SETTLE_S = 30
LUX_SETTLE_S = 120
STEP = 3
GAP_DDC_S = 300
GAP_BACKLIGHT_S = 60
RELEASE_STEP = 6
HAND_POINTS = 2
GAME_SETTLE_S = 10
EDGE_GAP_S = 60


def display_id(backend: str, value: str) -> str:
    return hashlib.sha256(b"torpor/display\0" + backend.encode() + b"\0" + value.encode()).hexdigest()[:12]


def percent_of(raw: int, maximum: int) -> int:
    return round(raw * 100 / maximum)


def raw_of(percent: int, maximum: int) -> int:
    value = round(percent * maximum / 100)
    return max(value, 1) if percent > 0 else 0


@dataclass
class Display:
    id: str
    label: str
    backend: str
    internal: bool
    key: str
    maximum: int
    raw: int | None = None
    present: bool = True
    error: str | None = None
    stopped: bool = False
    manual: int | str | None = None
    written_at: float | None = None
    settle_since: float | None = None
    target: int | None = None
    inflight: bool = False
    written_boost: float = 0.0
    written_anchor: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def percent(self) -> int | None:
        return percent_of(self.raw, self.maximum) if self.raw is not None and self.maximum > 0 else None

    @property
    def gap(self) -> float:
        backlight = self.backend == "logind" or (self.backend == "kde" and self.internal)
        return GAP_BACKLIGHT_S if backlight else GAP_DDC_S

    @property
    def ddc(self) -> bool:
        return over_ddc(self.backend, self.internal)

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "backend": self.backend,
            "internal": self.internal,
            "modes": self.ddc,
        }


def kde_inputs(found: list[KdeDisplay]) -> list[tuple[KdeDisplay, str, str]]:
    """(display, id input, label): the label, plus `#n` among displays with the same label."""
    counts: dict[str, int] = {}
    for display in found:
        counts[display.label] = counts.get(display.label, 0) + 1
    seen: dict[str, int] = {}
    rows: list[tuple[KdeDisplay, str, str]] = []
    for display in sorted(found, key=lambda d: d.name):
        if counts[display.label] == 1:
            rows.append((display, display.label, display.label))
            continue
        seen[display.label] = seen.get(display.label, 0) + 1
        number = seen[display.label]
        rows.append((display, f"{display.label}#{number}", f"{display.label} #{number}"))
    return rows


class Brightness(Module):
    id = "brightness"
    commands = ("brightness_set",)

    def __init__(self, ctx: Context, games: Games) -> None:
        super().__init__(ctx)
        self.games = games
        self.kde = ScreenBrightness(ctx.rt.session) if ctx.rt.session is not None else None
        self.logind = Logind(ctx.rt.system) if ctx.rt.system is not None else None
        self.ddc_binary = shutil.which(ctx.rt.env.get("TORPOR_DDCUTIL") or ctx.config.ddcutil) or ""
        self.ddc = Ddcutil(self.ddc_binary, env=ctx.rt.env) if self.ddc_binary else None
        self.displays: dict[str, Display] = {}
        self.reading: tuple[float | None, float] | None = None
        self.lux: float | None = None
        self.manuals: dict[str, Any] = dict(ctx.boot.get("brightness") or {})
        self.ddc_connectors: frozenset[str] | None = None
        self.ddc_detecting = False
        self.ddc_problem: str | None = None
        self.kde_problem: str | None = None
        self.auto_seen: dict[str, bool] | None = None
        self.modes = ModeReader(
            ctx.rt, self.ddc_binary, ctx.boot, ctx.failures, found=self.mode_found, again=self.again
        )
        self.gaming = False
        self.desk_since: float | None = None
        self.connectors: frozenset[str] | None = None
        self.again_timer: object | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        self.modes.start()
        bus = self.ctx.rt.session
        if bus is not None and self.kde is not None and self.uses("kde"):
            bus.watch_name(KDE_NAME, self.kde_owner_changed)
            self.kde.on_changed(self.kde_changed)
            self.kde.on_displays(self.kde_displays_changed)
        self.refresh()

    def mode(self) -> set[str]:
        choice = self.ctx.config.brightness_backend
        if choice == "none":
            return set()
        if choice != "auto":
            return {choice}
        if self.kde is not None and self.kde.present():
            return {"kde"}
        if self.ctx.known.worked(self.id, "kde") and not self.other_desktop():
            return {"kde"}
        return {"logind", "ddcutil"}

    def other_desktop(self) -> bool:
        """A graphical session of this user that isn't Plasma: Plasma once on this PC doesn't mean always."""
        facts = self.ctx.facts()
        for session in facts.sessions if facts is not None else ():
            if session.uid == self.ctx.uid and session.graphical and session.desktop:
                names = {name.lower() for name in session.desktop.split(":")}
                if not names & {"kde", "plasma"}:
                    return True
        return False

    def uses(self, backend: str) -> bool:
        choice = self.ctx.config.brightness_backend
        return choice in ("auto", backend)

    def excluded(self, did: str, backend: str) -> bool:
        override = self.ctx.config.displays.get(did, "auto")
        return override not in ("auto", backend)

    def adopt(self, display: Display) -> None:
        old = self.displays.get(display.id)
        if old is not None:
            if not old.present:
                old.stopped, old.error, old.settle_since = False, None, None
            if not old.present or old.key != display.key:
                self.modes.reprobe(old.id)
            old.present = True
            old.label, old.key, old.maximum, old.internal = (
                display.label,
                display.key,
                display.maximum,
                display.internal,
            )
            if display.raw is not None:
                old.raw = display.raw
            return
        manual = self.manuals.get(display.id)
        display.manual = manual if manual == PENDING or isinstance(manual, int) else None
        self.displays[display.id] = display
        self.ctx.changed()

    def remember(self, display: Display) -> None:
        self.ctx.known.seen(
            KnownDisplay(display.id, display.label, display.backend, display.internal, self.ctx.rt.loop.time())
        )
        self.ctx.known.remember(self.id, display.backend)

    def mark_missing(self, backend: str, present: set[str]) -> None:
        for display in self.displays.values():
            if display.backend == backend and display.id not in present and display.present:
                display.present = False
                display.settle_since = None
                self.ctx.changed()

    def leave_out(self, mode: set[str]) -> None:
        for display in self.displays.values():
            if display.backend not in mode and display.present:
                display.present = False
                display.settle_since = None
                self.ctx.changed()
        if "ddcutil" not in mode:
            self.ddc_connectors = None

    def refresh(self, *, ddc: bool = True) -> None:
        mode = self.mode()
        self.leave_out(mode)
        if "kde" in mode:
            self.refresh_kde()
        if "logind" in mode:
            self.refresh_logind(None)
        if "ddcutil" in mode and ddc:
            self.check_ddc()

    def refresh_kde(self) -> None:
        if self.kde is None or not self.kde.present():
            self.mark_missing("kde", set())
            return
        try:
            found = self.kde.displays()
        except BusError as error:
            self.kde_problem = f"reading org.kde.ScreenBrightness failed: {error.message}"
            self.ctx.failures.fail("brightness-kde", self.kde_problem)
            return
        self.kde_problem = None
        self.ctx.failures.clear("brightness-kde")
        present: set[str] = set()
        for item, value, label in kde_inputs(found):
            did = display_id("kde", value)
            if self.excluded(did, "kde"):
                continue
            display = Display(did, label, "kde", item.internal, item.name, item.maximum, item.value)
            self.adopt(display)
            self.remember(self.displays[did])
            present.add(did)
        self.mark_missing("kde", present)

    def refresh_logind(self, facts: Facts | None) -> None:
        present: set[str] = set()
        in_use = facts is not None and facts.state == IN_USE
        for light in sysfs.backlights(self.ctx.rt.sysfs):
            did = display_id("logind", light.name)
            if self.excluded(did, "logind"):
                continue
            known = self.displays.get(did)
            before = known.raw if known is not None and known.present else None
            self.adopt(Display(did, light.name, "logind", light.internal, light.name, light.maximum, light.value))
            display = self.displays[did]
            self.remember(display)
            present.add(did)
            if in_use and before is not None and not display.inflight:
                moved = abs(percent_of(light.value, light.maximum) - percent_of(before, light.maximum))
                if moved >= HAND_POINTS:
                    self.hand_change(display, "by hand")
        self.mark_missing("logind", present)

    def check_ddc(self) -> None:
        if self.modes.running is not None:
            return
        connectors = sysfs.connected_connectors(self.ctx.rt.sysfs)
        if connectors != self.ddc_connectors:
            self.ddc_connectors = connectors
            self.detect_ddc()

    def detect_ddc(self) -> None:
        if self.ddc is None:
            self.ddc_problem = "ddcutil is not installed"
            return
        if self.ddc_detecting:
            return
        self.ddc_detecting = True
        ddc = self.ddc

        def work() -> list[tuple[DdcDisplay, tuple[int, int] | str]]:
            rows: list[tuple[DdcDisplay, tuple[int, int] | str]] = []
            for found in ddc.detect():
                try:
                    rows.append((found, ddc.get(found.bus)))
                except DdcError as error:
                    rows.append((found, str(error)))
            return rows

        self.ctx.rt.loop.run_in_worker(work, self.detected)

    def detected(
        self, rows: list[tuple[DdcDisplay, tuple[int, int] | str]] | None, error: BaseException | None
    ) -> None:
        self.ddc_detecting = False
        if error is not None or rows is None:
            self.ddc_problem = str(error) if error else "ddcutil detect failed"
            self.ctx.failures.fail("brightness-ddc", self.ddc_problem)
            return
        self.ddc_problem = None if rows else "ddcutil found no DDC/CI display"
        self.ctx.failures.clear("brightness-ddc")
        present: set[str] = set()
        for found, reading in rows:
            did = display_id("ddcutil", found.input)
            if self.excluded(did, "ddcutil"):
                continue
            raw, maximum = reading if isinstance(reading, tuple) else (None, 100)
            display = Display(did, found.label, "ddcutil", False, str(found.bus), maximum, raw)
            self.adopt(display)
            current = self.displays[did]
            current.settle_since = None
            current.error = reading if isinstance(reading, str) else None
            current.stopped = current.error is not None
            self.remember(current)
            present.add(did)
        self.mark_missing("ddcutil", present)
        self.ctx.changed()

    def kde_owner_changed(self, owner: str | None) -> None:
        self.modes.quiet()
        for display in self.displays.values():
            if display.backend == "kde":
                display.stopped = False
                display.error = None
                display.settle_since = None
        self.refresh_kde()
        self.ctx.changed()

    def kde_displays_changed(self, member: str, name: str) -> None:
        self.modes.quiet()
        self.refresh_kde()
        if member == "DisplayAdded":
            for display in self.displays.values():
                if display.backend == "kde" and display.key == name:
                    display.settle_since = None
                    display.stopped = False
                    display.error = None

    def kde_changed(self, name: str, value: int, context: str) -> None:
        self.modes.quiet()
        display = next((d for d in self.displays.values() if d.backend == "kde" and d.key == name and d.present), None)
        known: int | None
        if display is None:
            self.refresh_kde()
            display = next((d for d in self.displays.values() if d.backend == "kde" and d.key == name), None)
            if display is None:
                return
            known = None
        else:
            known = display.raw
        display.raw = value
        self.ctx.changed()
        if context == CONTEXT or (known is not None and abs(value - known) < max(display.maximum // 100, 1)):
            return
        if not self.at_desk_now():
            return
        self.hand_change(display, "by hand")

    def at_desk_now(self) -> bool:
        facts = self.ctx.facts()
        if facts is None or facts.state != IN_USE:
            facts = self.ctx.refresh()
        return facts is not None and facts.state == IN_USE

    def hand_change(self, display: Display, how: str) -> None:
        if display.manual is None:
            log.info("%s brightness set to %s %% %s; automatic brightness paused", display.label, display.percent, how)
        self.modes.stale(display.id)
        if display.manual != PENDING:
            self.set_manual(display, PENDING)

    def set_manual(self, display: Display, manual: int | str | None) -> None:
        display.manual = manual
        if manual is None:
            self.manuals.pop(display.id, None)
        else:
            self.manuals[display.id] = manual
        self.ctx.boot.put("brightness", dict(self.manuals) or None)
        self.ctx.changed()

    def input(self, name: str, value: float | None, age: float) -> None:
        if name != "lux":
            return
        changed_at = self.ctx.rt.loop.monotonic() - max(age, 0.0)
        self.reading = (value, changed_at)

    def disconnected(self) -> None:
        self.reading = None

    def settled(self) -> float | None:
        if self.reading is None:
            return None
        value, changed_at = self.reading
        if value is None or self.ctx.rt.loop.monotonic() - changed_at < LUX_SETTLE_S:
            return None
        return value

    def auto(self, display: Display) -> bool:
        return self.ctx.ha.auto.get(display.id, False)

    def tick(self, facts: Facts) -> None:
        if not self.enabled:
            return
        now = self.ctx.rt.loop.monotonic()
        settled = self.settled()
        if settled is not None:
            self.lux = settled
        mode = self.mode()
        self.leave_out(mode)
        if "logind" in mode:
            self.refresh_logind(facts)
        if "ddcutil" in mode:
            self.check_ddc()
        if "kde" in mode and not any(d.backend == "kde" and d.present for d in self.displays.values()):
            self.refresh_kde()
        connectors = sysfs.connected_connectors(self.ctx.rt.sysfs)
        if self.connectors is not None and connectors != self.connectors:
            self.modes.quiet()
        self.connectors = connectors
        self.modes.tick()
        if facts.state != IN_USE:
            self.desk_since = None
        elif self.desk_since is None:
            self.desk_since = now
        game_settled = now - self.games.since >= GAME_SETTLE_S
        if game_settled:
            self.gaming = self.games.running is True
        for display in self.displays.values():
            if not display.present:
                continue
            anchor = self.modes.anchor(display.id)
            target = self.target(anchor)
            if display.target != target:
                display.target = target
                self.ctx.changed()
            if self.may_read(facts, now, game_settled) and self.wants_mode(display) and self.modes.due(display.id, now):
                self.modes.read(self.mode_target(display), now)
            self.automatic(display, facts, settled, target, anchor, game_settled)

    def boost(self) -> float:
        return self.ctx.ha.game_boost if self.gaming else 0.0

    def target(self, anchor: float | None) -> int | None:
        if self.lux is None or not self.ctx.ha.lux:
            return None
        return self.ctx.ha.curve.target(self.lux, anchor_pct=anchor, boost=self.boost())

    def wants_mode(self, display: Display) -> bool:
        return display.ddc and self.modes.wants(display.id)

    def may_read(self, facts: Facts, now: float, game_settled: bool) -> bool:
        """At the desk for a while, clear of a game's modeset, and no brightness call of Torpor's own running."""
        if facts.state != IN_USE or self.desk_since is None or now - self.desk_since < DESK_SETTLE_S:
            return False
        return game_settled and not self.ddc_detecting and not any(d.inflight for d in self.displays.values())

    def mode_target(self, display: Display) -> Target:
        if display.backend == "ddcutil":
            return Target(display.id, display.label, lambda found: display_id("ddcutil", found.input) == display.id)
        return Target(display.id, display.label, lambda found: label_matches(display.label, found))

    def mode_found(self, did: str, before: int | None, value: int | None) -> None:
        """A first read finds the mode rather than a change in it, so a baseline taken on the lowest anchor is
        taken again; a change ends a hand-set pause. A code read only to learn its values moves no curve."""
        display = self.displays.get(did)
        if display is not None and isinstance(display.manual, int) and self.modes.anchored(did):
            if before is None:
                self.set_manual(display, PENDING)
            else:
                self.set_manual(display, None)
                log.info("%s: automatic brightness resumed for a new mode", display.label)
        self.ctx.changed()

    def again(self) -> None:
        """A read ended: a write waiting for it can go now, not at the next tick, on facts read now."""
        if self.again_timer is not None or self.ctx.down():
            return

        def run() -> None:
            self.again_timer = None
            if self.ctx.down():
                return
            facts = self.ctx.refresh()
            if facts is not None:
                self.tick(facts)

        self.again_timer = self.ctx.rt.loop.call_later(0, run)

    def stop(self) -> None:
        self.ctx.rt.loop.cancel(self.again_timer)
        self.again_timer = None
        self.modes.stop()

    def automatic(
        self,
        display: Display,
        facts: Facts,
        settled: float | None,
        target: int | None,
        anchor: float | None,
        game_settled: bool,
    ) -> None:
        now = self.ctx.rt.loop.monotonic()
        if facts.state != IN_USE or display.stopped or not self.auto(display) or display.inflight:
            display.settle_since = None
            return
        if display.settle_since is None:
            display.settle_since = now
        if target is None:
            return
        if display.manual == PENDING:
            if settled is not None and game_settled and self.modes.settled(display.id):
                self.set_manual(display, target)
            return
        if isinstance(display.manual, int):
            if not game_settled or abs(target - display.manual) < RELEASE_STEP:
                return
            self.set_manual(display, None)
            log.info("%s: automatic brightness resumed", display.label)
        if not game_settled or now - display.settle_since < DESK_SETTLE_S:
            return
        boost = self.boost()
        edge = boost != display.written_boost or display.written_anchor not in (None, anchor)
        if settled is None and not edge:
            return
        if display.percent is not None and abs(target - display.percent) < STEP:
            return
        if display.written_at is not None and now - display.written_at < (EDGE_GAP_S if edge else display.gap):
            return
        if not self.active or not self.ctx.config.allow["brightness"] or self.modes.running is not None:
            return
        if self.wants_mode(display) and not self.modes.fresh(display.id, now):
            if self.may_read(facts, now, game_settled):
                self.modes.read(self.mode_target(display), now)
            return
        display.written_boost = boost
        display.written_anchor = anchor
        self.write(display, target, facts, automatic=True)

    def write(
        self, display: Display, percent: int, facts: Facts | None, *, automatic: bool, reply: Reply | None = None
    ) -> None:
        before = display.percent
        value = raw_of(percent, display.maximum)
        display.written_at = self.ctx.rt.loop.monotonic()
        self.modes.quiet()
        why = [f"{self.lux:g} lx"] if self.lux is not None else []
        mode = self.modes.name(display.id)
        if mode is not None:
            why.append(f"mode {mode}")
        if self.boost():
            why.append("game")
        lux = f" ({', '.join(why)})" if automatic and why else ""

        def finished(error: str | None) -> None:
            display.inflight = False
            if error is None:
                display.error = None
                self.ctx.failures.clear(f"brightness-{display.id}")
                if reply is not None:
                    reply({"percent": percent}, None)
                self.ctx.changed()
                return
            display.error = error
            self.ctx.changed()
            if automatic:
                display.stopped = True
                self.ctx.failures.fail(
                    f"brightness-{display.id}",
                    f"{display.label}: setting brightness to {percent} % failed: {error}; automatic brightness stops "
                    "until the PC resumes, the display or its backend comes back, or auto is turned off and on",
                )
            if reply is not None:
                reply(None, CommandError("failed", error))

        if automatic:
            log.info("%s brightness: %s -> %s %%%s", display.label, before, percent, lux)
        if display.backend == "kde":
            self.write_kde(display, value, finished)
        elif display.backend == "logind":
            self.write_logind(display, value, facts, finished)
        else:
            self.write_ddc(display, value, automatic, finished)

    def write_kde(self, display: Display, value: int, finished: Any) -> None:
        assert self.kde is not None
        display.raw = value
        self.kde.set(display.key, value, lambda _result, error: finished(error.message if error else None))

    def write_logind(self, display: Display, value: int, facts: Facts | None, finished: Any) -> None:
        session = self.owner_session(facts)
        if session is None or self.logind is None:
            finished("no active session of this user to set the backlight through")
            return
        display.inflight = True

        def done(_result: Any, error: BusError | None) -> None:
            if error is None:
                display.raw = value
            finished(error.message if error else None)

        self.logind.set_brightness(session, display.key, value, done)

    def owner_session(self, facts: Facts | None) -> str | None:
        facts = facts or self.ctx.facts()
        if facts is None:
            return None
        for session in facts.sessions:
            if session.uid == self.ctx.uid and session.active and session.graphical:
                return session.path
        return None

    def write_ddc(self, display: Display, value: int, automatic: bool, finished: Any) -> None:
        assert self.ddc is not None
        ddc = self.ddc
        bus = int(display.key)
        known = display.raw
        maximum = display.maximum
        display.inflight = True

        def work() -> tuple[str, int, int]:
            if automatic:
                current, top = ddc.get(bus)
                if known is not None and abs(percent_of(current, top) - percent_of(known, maximum)) >= HAND_POINTS:
                    return "hand", current, top
            ddc.set(bus, value)
            return "written", value, maximum

        def done(outcome: tuple[str, int, int] | None, error: BaseException | None) -> None:
            if error is not None or outcome is None:
                finished(str(error) if error else "ddcutil failed")
                return
            kind, raw, top = outcome
            display.raw, display.maximum = raw, top
            if kind == "hand":
                display.inflight = False
                display.written_at = None
                self.hand_change(display, "on the monitor or by another program")
                return
            finished(None)

        self.ctx.rt.loop.run_in_worker(work, done)

    def configure(self) -> None:
        self.modes.configure(self.ctx.ha.modes if self.ctx.ha.wants(self.id) else {})
        for did, on in self.ctx.ha.auto.items():
            if self.auto_seen is None:
                break
            was = self.auto_seen.get(did, False)
            display = self.displays.get(did)
            if on and not was and display is not None:
                display.stopped = False
                display.error = None
                display.settle_since = None
                if display.manual is not None:
                    self.set_manual(display, None)
        self.auto_seen = dict(self.ctx.ha.auto)
        self.ctx.changed()

    def resumed(self) -> None:
        self.desk_since = None
        self.modes.reprobe()
        for display in self.displays.values():
            display.stopped = False
            display.error = None
            display.settle_since = None
            if display.manual is not None:
                self.set_manual(display, None)
        self.ddc_connectors = None
        self.refresh()

    def command(self, name: str, args: dict[str, Any], reply: Reply) -> None:
        did = args.get("display")
        percent = args.get("percent")
        if not isinstance(did, str):
            raise CommandError("bad_request", "display must be a display ID")
        if isinstance(percent, bool) or not isinstance(percent, int | float) or not 0 <= percent <= 100:
            raise CommandError("bad_request", "percent must be a number from 0 to 100")
        display = self.displays.get(did)
        if display is None or not display.present:
            raise CommandError("unavailable", "that display is not connected")
        if display.inflight:
            raise CommandError("busy", "a brightness change for that display is still running")
        log.info("%s brightness set to %d %% from Home Assistant", display.label, round(percent))
        self.set_manual(display, PENDING)
        self.modes.stale(display.id)

        def write() -> None:
            if self.ctx.down():
                reply(None, CommandError("unavailable", "the PC is going down"))
                return
            self.write(display, round(percent), None, automatic=False, reply=reply)

        if not self.modes.after_read(write):
            write()

    def capabilities(self) -> dict[str, Any]:
        if not self.enabled:
            return self.disabled_capabilities()
        now = self.ctx.rt.loop.time()
        listed: dict[str, dict[str, Any]] = {}
        for known in self.ctx.known.recent(now):
            if not self.excluded(known.id, known.backend) and known.backend in self.mode():
                listed[known.id] = known.wire()
        for display in self.displays.values():
            if display.present:
                listed[display.id] = display.wire()
        problems = self.problems()
        available = bool(listed) or any(self.ctx.known.worked(self.id, b) for b in ("kde", "ddcutil", "logind"))
        if not available and not problems:
            problems.append(problem("no_display", "no display with a brightness control found"))
        modes = self.modes.problems()
        return {
            "enabled": True,
            "available": available,
            "backend": None,
            "problems": problems,
            "actions": {"brightness": self.ctx.config.allow["brightness"]},
            "displays": sorted(listed.values(), key=lambda d: (d["label"], d["id"])),
            "modes": {
                "available": self.modes.possible,
                "problems": [problem(code, message, "torporctl doctor") for code, message in modes],
            },
        }

    def problems(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        mode = self.mode()
        if self.ctx.config.brightness_backend == "none":
            return found
        if "kde" in mode and self.kde_problem:
            found.append(problem("kde_failed", self.kde_problem))
        if "ddcutil" in mode:
            if self.ddc is None:
                found.append(problem("no_ddcutil", "ddcutil is not installed", "torporctl doctor"))
            elif self.ddc_problem:
                found.append(problem("ddcutil_failed", self.ddc_problem, "torporctl doctor"))
        return found

    def state(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        displays: dict[str, Any] = {}
        for display in self.displays.values():
            if not display.present:
                continue
            displays[display.id] = {
                "percent": display.percent,
                "auto": self.auto(display),
                "overridden": display.manual is not None,
                "target": display.target,
                "error": display.error,
            }
            mode = self.modes.report(display.id) if display.ddc else None
            if mode is not None:
                displays[display.id]["mode"] = mode
        return {"lux": self.lux, "displays": displays}

    def summary(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for display in self.displays.values():
            if not display.present:
                continue
            row = display.wire()
            mode = self.modes.report(display.id)
            if mode is not None and display.ddc:
                row["mode"] = {**mode, "name": self.modes.name(display.id)}
            rows.append(row)
        return rows
