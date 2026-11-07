"""power: lock, suspend, hibernate, power off and reboot through logind, within the [allow] ceiling."""

from __future__ import annotations

import logging
from typing import Any

from torpor import model
from torpor.backends.logind import WHO, Inhibitor, Logind
from torpor.bus import BusError, Done
from torpor.model import Facts
from torpor.modules.base import CommandError, Context, Module, Reply, problem

log = logging.getLogger("torpor")

ACTIONS = ("lock", "suspend", "hibernate", "poweroff", "reboot")
SLEEPS = ("suspend", "hibernate")
DETAIL_WINDOW_S = 120
POLKIT_HINT = "torporctl doctor prints the polkit rule that allows it"


class Power(Module):
    id = "power"
    commands = ACTIONS

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.logind = Logind(ctx.rt.system) if ctx.rt.system is not None else None
        self.running: str | None = None
        self.can_suspend = "na"
        self.can_hibernate = "na"
        self.blockers: list[Inhibitor] = []
        self.requested: tuple[str, float] | None = None

    def capabilities(self) -> dict[str, Any]:
        if not self.enabled:
            return self.disabled_capabilities()
        available = self.logind is not None and (self.logind.present() or self.ctx.known.worked(self.id, "logind"))
        if available:
            self.ctx.known.remember(self.id, "logind")
        problems = [] if available else [problem("no_logind", "systemd-logind is not on the system bus")]
        return {
            "enabled": True,
            "available": available,
            "backend": "logind",
            "problems": problems,
            "actions": {action: self.ctx.config.allow[action] for action in ACTIONS},
        }

    def tick(self, facts: Facts) -> None:
        if self.logind is None:
            return
        try:
            self.can_suspend = self.logind.can("suspend")
            self.can_hibernate = self.logind.can("hibernate")
            self.blockers = self.others(self.logind.inhibitors())
            self.ctx.failures.clear("power")
        except BusError as error:
            self.ctx.failures.fail("power", f"reading logind's power state failed: {error.message}")

    def others(self, inhibitors: list[Inhibitor]) -> list[Inhibitor]:
        return [
            i
            for i in inhibitors
            if i.mode == "block"
            and ("sleep" in i.what.split(":") or "shutdown" in i.what.split(":"))
            and not (i.who == WHO and i.pid == self.ctx.rt.pid)
        ]

    def state(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        return {
            "can_suspend": self.can_suspend,
            "can_hibernate": self.can_hibernate,
            "blockers": [b.wire() for b in self.blockers],
        }

    def detail(self) -> str | None:
        """`suspend` or `hibernate` when Home Assistant asked for this sleep, for going_down."""
        if self.requested is None:
            return None
        action, at = self.requested
        self.requested = None
        return action if self.ctx.rt.loop.monotonic() - at < DETAIL_WINDOW_S else None

    def command(self, name: str, args: dict[str, Any], reply: Reply) -> None:
        force = args.get("force", False)
        if not isinstance(force, bool):
            raise CommandError("bad_request", "force must be true or false")
        if self.logind is None:
            raise CommandError("unavailable", "systemd-logind is not reachable")
        if self.running is not None:
            raise CommandError("busy", f"{self.running} is still running")
        try:
            sessions = self.logind.sessions()
        except BusError as error:
            raise CommandError("failed", f"reading logind sessions failed: {error.message}") from error
        if name == "lock":
            self.lock(sessions, reply)
            return
        self.sleep_or_shutdown(name, force, sessions, reply)

    def lock(self, sessions: list[model.SessionInfo], reply: Reply) -> None:
        assert self.logind is not None
        mine = model.owner_graphical(sessions, self.ctx.uid)
        if model.another_user_at_desk(sessions, self.ctx.uid):
            raise CommandError("refused", "another user is at the desk")
        if not mine:
            raise CommandError("unavailable", "no graphical session of this user to lock")
        self.running = "lock"
        waiting = {s.path for s in mine}
        errors: list[str] = []

        def locked(path: str, error: BusError | None) -> None:
            waiting.discard(path)
            if error is not None:
                errors.append(error.message)
            if waiting:
                return
            self.running = None
            if errors:
                reply(None, CommandError("failed", f"locking failed: {errors[0]}"))
            else:
                log.info("locked %d session(s) for Home Assistant", len(mine))
                reply({}, None)

        def finished(path: str) -> Done:
            return lambda _result, error: locked(path, error)

        for session in mine:
            self.logind.lock(session.path, finished(session.path))

    def sleep_or_shutdown(self, name: str, force: bool, sessions: list[model.SessionInfo], reply: Reply) -> None:
        assert self.logind is not None
        try:
            can = self.logind.can(name)
            blockers = self.others(self.logind.inhibitors())
        except BusError as error:
            raise CommandError("failed", f"asking logind failed: {error.message}") from error
        if name == "hibernate" and can != "yes":
            raise CommandError("unavailable", f"logind says CanHibernate={can}")
        if can in ("no", "na"):
            raise CommandError("unavailable", f"logind says {name} is not possible here ({can})")
        what = "sleep" if name in SLEEPS else "shutdown"
        relevant = [b for b in blockers if what in b.what.split(":")]
        if relevant and not force:
            count = "1 app blocks" if len(relevant) == 1 else f"{len(relevant)} apps block"
            raise CommandError("refused", f"{count} {what}", blockers=[b.wire() for b in relevant])
        if model.another_user_at_desk(sessions, self.ctx.uid) and not force:
            raise CommandError("refused", "another user is at the desk")
        self.running = name
        if name in SLEEPS:
            self.requested = (name, self.ctx.rt.loop.monotonic())
        log.info("%s requested by Home Assistant%s", name, " (forced past blockers)" if relevant else "")

        def done(_result: tuple[Any, ...] | None, error: BusError | None) -> None:
            self.running = None
            if error is None:
                reply({}, None)
                return
            self.requested = None
            if error.denied:
                reply(None, CommandError("refused", f"polkit refused {name}: {error.message}", hint=POLKIT_HINT))
            else:
                reply(None, CommandError("failed", f"{name} failed: {error.message}"))

        self.logind.act(name, done)
