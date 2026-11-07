"""keep_awake: the desktop's idle inhibit, held for Home Assistant's switch and for busy sources."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from torpor.backends.gnome import GnomeInhibit
from torpor.backends.kde import PowerDevilInhibit
from torpor.backends.portal import PortalInhibit
from torpor.bus import BusError
from torpor.model import Facts
from torpor.modules.base import CommandError, Context, Module, Reply, problem

log = logging.getLogger("torpor")

SWITCH_REASON = "Kept awake from Home Assistant"
MAX_FOR_S = 7 * 86400


class Backend(Protocol):
    name: str
    bus_name: str

    def inhibit(self, reason: str) -> object: ...

    def release(self, token: object) -> None: ...


@dataclass(frozen=True)
class Held:
    backend: Backend
    token: object
    reason: str
    owner: str


class KeepAwake(Module):
    id = "keep_awake"
    commands = ("keep_awake",)

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        bus = ctx.rt.session
        backends: list[Backend] = (
            [PowerDevilInhibit(bus), GnomeInhibit(bus), PortalInhibit(bus)] if bus is not None else []
        )
        choice = ctx.config.keep_awake_backend
        self.backends = [b for b in backends if choice in ("auto", b.name)]
        self.failed: dict[str, str] = {}
        self.owners: dict[str, str | None] = {}
        self.held: Held | None = None
        self.timer: object | None = None
        saved = ctx.boot.get("keep_awake") or {}
        self.on = bool(saved.get("on", False))
        until, until_wall = saved.get("until"), saved.get("until_wall")
        self.until: tuple[float, float] | None = None
        if isinstance(until, int | float) and isinstance(until_wall, int | float):
            self.until = (float(until), float(until_wall))
        self.busy: tuple[str, ...] = ()

    def start(self) -> None:
        if self.on and not self.ctx.config.allow["keep_awake"]:
            log.info("keep-awake ended: [allow] in torpor.conf no longer allows it")
            self.set(False, None)
        bus = self.ctx.rt.session
        if bus is None:
            return
        for backend in self.backends:
            bus.watch_name(backend.bus_name, self.watcher(backend))
        self.expire()
        self.arm_timer()

    def watcher(self, backend: Backend) -> Callable[[str | None], None]:
        return lambda owner: self.owner_changed(backend, owner)

    def owner_changed(self, backend: Backend, owner: str | None) -> None:
        """A cookie dies with the owner that issued it. apply() waits for a backend's first report, so
        every held cookie records its owner, and only a different owner drops it unreleased."""
        self.owners[backend.name] = owner
        self.failed.pop(backend.name, None)
        if self.held is not None and self.held.backend is backend and self.held.owner != owner:
            self.held = None
        self.apply()
        self.ctx.changed()

    def present(self) -> list[Backend]:
        bus = self.ctx.rt.session
        return [b for b in self.backends if bus is not None and bus.has_owner(b.bus_name)]

    def capabilities(self) -> dict[str, Any]:
        if not self.enabled:
            return self.disabled_capabilities()
        present = self.present()
        known = [b for b in self.backends if self.ctx.known.worked(self.id, b.name)]
        working = [b for b in present if b.name not in self.failed]
        backend = self.held.backend if self.held else (working[0] if working else (known[0] if known else None))
        available = bool(working or known)
        problems = [problem(f"{name}_failed", message) for name, message in self.failed.items()]
        if not available:
            problems.append(
                problem("no_backend", "no desktop idle inhibit found (PowerDevil, GNOME session manager or portal)")
            )
        return {
            "enabled": True,
            "available": available,
            "backend": backend.name if backend else None,
            "problems": problems,
            "actions": {"keep_awake": self.ctx.config.allow["keep_awake"]},
        }

    def remaining(self) -> float | None:
        """The monotonic clock stops while the PC sleeps and the wall clock can jump: the sooner end wins."""
        if self.until is None:
            return None
        loop = self.ctx.rt.loop
        monotonic, wall = self.until
        return max(0.0, min(monotonic - loop.monotonic(), wall - loop.time()))

    def state(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        remaining = self.remaining() if self.on else None
        return {
            "on": self.on,
            "remaining": round(remaining) if remaining is not None else None,
            "backend": self.held.backend.name if self.held else None,
            "applied": self.held is not None,
        }

    def switch_reasons(self) -> list[str]:
        return ["keep_awake"] if self.on and self.enabled else []

    def tick(self, facts: Facts) -> None:
        self.busy = tuple(reason for reason in facts.busy if reason != "keep_awake")
        self.expire()
        self.apply()

    def expire(self) -> None:
        left = self.remaining()
        if self.on and left is not None and left <= 0:
            log.info("keep-awake ended: its time ran out")
            self.set(False, None)

    def arm_timer(self) -> None:
        loop = self.ctx.rt.loop
        loop.cancel(self.timer)
        self.timer = None
        left = self.remaining()
        if self.on and left is not None:

            def fire() -> None:
                self.timer = None
                self.expire()
                self.apply()
                self.ctx.changed()

            self.timer = loop.call_later(left, fire)

    def set(self, on: bool, until: tuple[float, float] | None) -> None:
        self.on = on
        self.until = until if on else None
        saved = {"on": True, "until": self.until[0], "until_wall": self.until[1]} if self.until else {"on": True}
        self.ctx.boot.put("keep_awake", saved if on else None)
        self.arm_timer()

    def wanted_reason(self) -> str | None:
        if self.on:
            return SWITCH_REASON
        if self.busy:
            return "Busy: " + ", ".join(self.busy)
        return None

    def apply(self) -> None:
        reason = self.wanted_reason() if self.active else None
        if reason is None:
            self.release()
            return
        if self.held is not None and self.held.reason == reason:
            return
        for backend in self.backends:
            owner = self.owners.get(backend.name)
            if owner is None or backend.name in self.failed:
                continue
            try:
                token = backend.inhibit(reason)
            except BusError as error:
                self.failed[backend.name] = f"{backend.name} inhibit failed: {error.message}"
                log.warning(self.failed[backend.name])
                continue
            self.release()
            self.held = Held(backend, token, reason, owner)
            self.ctx.known.remember(self.id, backend.name)
            log.info("idle inhibited through %s: %s", backend.name, reason)
            self.ctx.changed()
            return

    def release(self) -> None:
        held, self.held = self.held, None
        if held is None:
            return
        try:
            held.backend.release(held.token)
        except BusError as error:
            log.debug("releasing the %s inhibit failed: %s", held.backend.name, error.message)
        log.info("idle inhibit released")
        self.ctx.changed()

    def command(self, name: str, args: dict[str, Any], reply: Reply) -> None:
        on = args.get("on")
        length = args.get("for")
        if not isinstance(on, bool):
            raise CommandError("bad_request", "on must be true or false")
        if length is not None and (
            isinstance(length, bool) or not isinstance(length, int | float) or not 0 < length <= MAX_FOR_S
        ):
            raise CommandError("bad_request", f"for must be seconds from 1 to {MAX_FOR_S}, or null")
        if on:
            loop = self.ctx.rt.loop
            until = (loop.monotonic() + float(length), loop.time() + float(length)) if length is not None else None
            self.set(True, until)
            log.info("keep-awake on for Home Assistant%s", f" for {round(length)} s" if length else "")
        else:
            self.set(False, None)
            log.info("keep-awake off for Home Assistant")
        self.apply()
        self.ctx.refresh()
        self.ctx.changed()
        remaining = self.remaining() if self.on else None
        reply({"remaining": round(remaining) if remaining is not None else None}, None)

    def configure(self) -> None:
        if not self.ctx.ha.wants(self.id) and self.on:
            self.set(False, None)
        self.apply()

    def resumed(self) -> None:
        self.expire()
        self.arm_timer()
        self.apply()

    def stop(self) -> None:
        self.ctx.rt.loop.cancel(self.timer)
        self.release()
