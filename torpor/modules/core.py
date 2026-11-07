"""core: what is happening on the PC (sessions, display, remote, busy) and the going-down goodbye."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from torpor import leases, model
from torpor.backends import sysfs
from torpor.backends.logind import Logind
from torpor.bus import BusError
from torpor.games import Games
from torpor.model import Facts, SessionInfo
from torpor.modules.base import Context, Module, problem

log = logging.getLogger("torpor")

SCREENSAVER = ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver")
LEGACY_SHUTDOWN_GRACE_S = 0.3
DELAY_WHY = "Tell Home Assistant the PC is going down"
REBOOT_KINDS = ("reboot", "kexec", "soft-reboot")
POWEROFF_KINDS = ("poweroff", "halt")


def shutdown_kind(metadata: dict[str, Any] | None) -> str:
    kind = str((metadata or {}).get("type", ""))
    if kind in REBOOT_KINDS:
        return "reboot"
    if kind in POWEROFF_KINDS:
        return "poweroff"
    return "unknown"


class Core(Module):
    id = "core"

    def __init__(
        self,
        ctx: Context,
        *,
        games: Games,
        extra_busy: Callable[[], list[str]],
        going_down: Callable[[str], None],
        came_back: Callable[[bool], None],
    ) -> None:
        super().__init__(ctx)
        self.logind = Logind(ctx.rt.system) if ctx.rt.system is not None else None
        self.games = games
        self.extra_busy = extra_busy
        self.going_down = going_down
        self.came_back = came_back
        self.delay_fd: int | None = None
        self.down = False
        self.typed_shutdown = False
        self.legacy_timer: object | None = None
        self.last: Facts | None = None

    @property
    def enabled(self) -> bool:
        return True

    def start(self) -> None:
        if self.logind is None:
            return
        self.logind.on_sleep(self.on_sleep)
        self.logind.on_shutdown(self.on_shutdown)
        self.take_delay()

    def capabilities(self) -> dict[str, Any]:
        available = self.logind is not None and self.logind.present()
        problems = [] if available else [problem("no_logind", "systemd-logind is not on the system bus")]
        return {
            "enabled": True,
            "available": available,
            "backend": "logind",
            "problems": problems,
            "games": list(self.games.found),
        }

    def observe(self) -> Facts | None:
        """Read the PC now. None when logind can't be read: the last facts stay."""
        if self.logind is None:
            return None
        try:
            sessions = self.logind.sessions()
        except BusError as error:
            self.ctx.failures.fail("observe", f"reading logind sessions failed: {error.message}")
            return None
        self.ctx.failures.clear("observe", "reading logind sessions works again")
        sessions = [self.refine_lock(s) for s in sessions]
        rt, config = self.ctx.rt, self.ctx.config
        remote = model.remote_sessions(sessions, ssh=config.ssh) + sysfs.inbound_ports(rt.proc, config.remote_ports)
        busy = list(self.extra_busy())
        now = rt.loop.time()
        busy += [
            f"lease:{lease.name}"
            for lease in leases.scan(self.ctx.paths.leases_dir, now=now, ttl=config.lease_ttl, proc=rt.proc)
        ]
        busy += [f"process:{p}" for p in leases.matching_processes(config.processes, rt.proc, own_pid=rt.pid)]
        facts = model.observe(
            sessions,
            display_on=sysfs.display_lit(rt.sysfs),
            remote=remote,
            busy=busy,
            boot_time=sysfs.boot_time(rt.proc),
        )
        self.last = facts
        return facts

    def refine_lock(self, session: SessionInfo) -> SessionInfo:
        """A lock screen started at login may never set LockedHint; the owner's own session bus knows."""
        bus = self.ctx.rt.session
        if bus is None or session.uid != self.ctx.uid or not session.graphical or session.locked or not session.active:
            return session
        try:
            (active,) = bus.call(SCREENSAVER, "GetActive", reply="b", timeout_ms=1500)
        except BusError:
            return session
        if not active:
            return session
        return replace(session, locked=True)

    def take_delay(self) -> None:
        if self.delay_fd is not None or self.logind is None:
            return
        try:
            self.delay_fd = self.logind.take_delay(DELAY_WHY)
            self.ctx.failures.clear("delay")
        except BusError as error:
            self.ctx.failures.fail("delay", f"taking logind's delay inhibitor failed: {error.message}")

    def release_delay(self) -> None:
        fd, self.delay_fd = self.delay_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def on_sleep(self, starting: bool) -> None:
        if starting:
            self.begin("sleep")
            return
        self.end(resumed=True)

    def on_shutdown(self, starting: bool, metadata: dict[str, Any] | None) -> None:
        if metadata is not None:
            self.typed_shutdown = starting
            if starting:
                self.begin(shutdown_kind(metadata))
            else:
                self.end(resumed=False)
            return
        if not starting:
            self.end(resumed=False)
            return

        def legacy() -> None:
            self.legacy_timer = None
            if not self.typed_shutdown:
                self.begin("unknown")

        self.legacy_timer = self.ctx.rt.loop.call_later(LEGACY_SHUTDOWN_GRACE_S, legacy)

    def begin(self, kind: str) -> None:
        if self.down:
            return
        self.down = True
        log.info("going down (%s)", kind)
        self.going_down(kind)

    def end(self, *, resumed: bool) -> None:
        self.typed_shutdown = False
        self.ctx.rt.loop.cancel(self.legacy_timer)
        self.legacy_timer = None
        if not self.down:
            return
        self.down = False
        log.info("resumed" if resumed else "shutdown cancelled")
        self.take_delay()
        self.came_back(resumed)

    def stop(self) -> None:
        self.release_delay()
