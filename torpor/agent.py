"""torpord: the agent. Observes the PC, keeps the connection to Home Assistant, runs the modules."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from torpor import __version__, host, logs, protocol
from torpor.backends import sysfs
from torpor.bootstate import BootState
from torpor.client import Client, Identity
from torpor.config import Config, Issue, load
from torpor.games import Games
from torpor.keyfile import read_key
from torpor.known import Known
from torpor.leases import scan as scan_leases
from torpor.logs import Failures
from torpor.model import Facts
from torpor.modules.base import CommandError, Context, HaConfig, Module
from torpor.modules.brightness import Brightness
from torpor.modules.core import Core
from torpor.modules.keep_awake import KeepAwake
from torpor.modules.power import Power
from torpor.modules.wake import Wake
from torpor.paths import Paths, ensure_dir
from torpor.runtime import Runtime
from torpor.status import StatusWriter

log = logging.getLogger("torpor")

TICK_S = 10
KEY_POLL_S = 10
ALLOW_KEY = {"brightness_set": "brightness"}


def comparable(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """A state without the keep-awake countdown, which changes every second but is no news."""
    if state is None or "keep_awake" not in state:
        return state
    return {**state, "keep_awake": {**state["keep_awake"], "remaining": state["keep_awake"]["remaining"] is None}}


class Agent:
    def __init__(
        self, rt: Runtime, config: Config, paths: Paths, *, observe_only: bool, readonly: bool = False
    ) -> None:
        """`readonly` is for `torporctl pair`: a look at the PC that leaves the running agent's files alone."""
        self.rt = rt
        self.config = config
        self.paths = paths
        self.observe_only = observe_only
        self.failures = Failures()
        self.facts: Facts | None = None
        self.ctx = Context(
            rt=rt,
            config=config,
            paths=paths,
            known=Known(paths.known_file, readonly=readonly),
            boot=BootState(paths.boot_state_file, rt.proc),
            failures=self.failures,
            observe_only=observe_only,
            changed=self.changed,
            facts=lambda: self.facts,
            refresh=self.refresh_facts,
            down=lambda: self.core.down or self.stopping,
        )
        self.games = Games(rt, config, self.failures, self.changed)
        self.keep_awake = KeepAwake(self.ctx)
        self.power = Power(self.ctx)
        self.wake = Wake(self.ctx)
        self.brightness = Brightness(self.ctx, self.games)
        self.core = Core(
            self.ctx,
            games=self.games,
            extra_busy=self.keep_awake.switch_reasons,
            going_down=self.going_down,
            came_back=self.came_back,
        )
        self.modules: dict[str, Module] = {
            "core": self.core,
            "power": self.power,
            "keep_awake": self.keep_awake,
            "wake": self.wake,
            "brightness": self.brightness,
        }
        self.identity = Identity(host.agent_id(rt.env), __version__)
        self.client = Client(
            rt.loop,
            rt.transport,
            identity=self.identity,
            credentials=self.credentials,
            tls_verify=config.tls_verify,
            handler=self,
        )
        self.status = StatusWriter(paths.status_file, rt.loop, self.status_data, enabled=not readonly)
        self.started = rt.loop.time()
        self.linger: bool | None = None
        self.desktop: str | None = None
        self.sent_capabilities: dict[str, Any] | None = None
        self.sent_state: dict[str, Any] | None = None
        self.last_state_at: float | None = None
        self.publish_timer: object | None = None
        self.events: list[dict[str, Any]] = []
        self.down: tuple[str, object] | None = None
        self.down_count = 0
        self.tick_timer: object | None = None
        self.key_timer: object | None = None
        self.stopping = False

    def credentials(self) -> tuple[str, bytes] | None:
        key = read_key(self.paths.key_file)
        if key is None or not self.config.ha_url:
            return None
        return self.config.ha_url, key

    def start(self) -> None:
        ensure_dir(self.paths.runtime_dir)
        ensure_dir(self.paths.leases_dir)
        mode = " in observe-only mode: it reports but executes nothing" if self.observe_only else ""
        log.info("torpord %s starting%s", __version__, mode)
        self.games.start()
        for module in self.modules.values():
            module.start()
        self.queue_started()
        self.tick()
        self.client.start()
        if self.client.state == "not_paired":
            log.info("not paired yet: run torporctl pair")
            self.watch_key()
        self.status.changed()

    def queue_started(self) -> None:
        self.events.append(
            {
                "type": "event",
                "name": "started",
                "boot_time": sysfs.boot_time(self.rt.proc),
                "uptime": sysfs.uptime(self.rt.proc),
            }
        )

    def watch_key(self) -> None:
        def check() -> None:
            self.key_timer = None
            if not self.config.ha_url:
                url = load(self.paths.config_file)[0].ha_url
                self.config = self.ctx.config = replace(self.config, ha_url=url)
            if self.credentials() is None:
                self.key_timer = self.rt.loop.call_later(KEY_POLL_S, check)
                return
            log.info("found a pairing key: connecting")
            self.client.dial()

        self.key_timer = self.rt.loop.call_later(KEY_POLL_S, check)

    def snapshot(self) -> dict[str, Any]:
        """Capabilities from a one-off, synchronous look at the PC, for `torporctl pair`. DDC/CI monitors
        come from known.json: running ddcutil beside the agent's own calls could garble both."""
        self.facts = self.core.observe()
        if self.core.logind is not None:
            self.linger = self.core.logind.linger(self.rt.uid)
        self.wake.scan_now()
        self.brightness.refresh(ddc=False)
        self.brightness.modes.check_now()
        self.games.read()
        return self.capabilities()

    def refresh_facts(self) -> Facts | None:
        facts = self.core.observe()
        if facts is not None:
            self.facts = facts
        return self.facts

    def tick(self) -> None:
        self.tick_timer = self.rt.loop.call_later(TICK_S, self.tick)
        if self.core.down or self.stopping:
            return
        facts = self.core.observe()
        if facts is not None:
            self.facts = facts
        self.games.read()
        if self.facts is not None:
            for module in self.modules.values():
                if module is self.core:
                    continue
                try:
                    module.tick(self.facts)
                except Exception as error:  # a broken module must not stop the others
                    self.failures.fail(f"tick-{module.id}", f"{module.id} failed: {type(error).__name__}: {error}")
        if self.core.logind is not None:
            self.linger = self.core.logind.linger(self.rt.uid)
        self.publish()

    def changed(self) -> None:
        if self.publish_timer is None:
            self.publish_timer = self.rt.loop.call_later(0, self.publish)

    def capabilities(self) -> dict[str, Any]:
        for session in self.facts.sessions if self.facts is not None else ():
            if session.uid == self.rt.uid and session.graphical and session.desktop:
                self.desktop = session.desktop
        return {
            "observe_only": self.observe_only,
            "host": host.host(self.rt.env, self.rt.sysfs, desktop=self.desktop, linger=self.linger),
            "modules": {name: module.capabilities() for name, module in self.modules.items()},
        }

    def state(self) -> dict[str, Any] | None:
        facts = self.facts
        if facts is None:
            return None
        message: dict[str, Any] = {
            "state": facts.state,
            "session": facts.session,
            "display_on": facts.display_on,
            "remote": list(facts.remote),
            "busy": list(facts.busy),
            "boot_time": facts.boot_time,
            "game": self.games.running,
        }
        for name in ("power", "keep_awake", "brightness"):
            section = self.modules[name].state()
            if section is not None:
                message[name] = section
        return message

    def publish(self) -> None:
        self.rt.loop.cancel(self.publish_timer)
        self.publish_timer = None
        self.status.changed()
        if not self.client.connected or self.core.down:
            return
        capabilities = self.capabilities()
        if capabilities != self.sent_capabilities and self.client.send({"type": "capabilities", **capabilities}):
            self.sent_capabilities = capabilities
        self.send_state()
        self.flush_events()

    def send_state(self) -> None:
        state = self.state()
        if state is None or comparable(state) == comparable(self.sent_state):
            return
        now = self.rt.loop.monotonic()
        if self.last_state_at is not None and now - self.last_state_at < protocol.STATE_MIN_INTERVAL_S:
            wait = self.last_state_at + protocol.STATE_MIN_INTERVAL_S - now
            self.publish_timer = self.rt.loop.call_later(wait, self.publish)
            return
        if self.client.send({"type": "state", **state}):
            if self.sent_state is None or state["state"] != self.sent_state["state"]:
                log.info("state: %s", state["state"])
            self.sent_state = state
            self.last_state_at = now

    def flush_events(self) -> None:
        while self.events and self.client.connected and self.sent_state is not None:
            if not self.client.send(self.events[0]):
                return
            self.events.pop(0)

    def authenticated(self) -> None:
        self.sent_capabilities = None
        self.sent_state = None
        self.last_state_at = None
        self.rt.loop.cancel(self.key_timer)
        self.key_timer = None
        self.publish()

    def disconnected(self) -> None:
        self.brightness.disconnected()
        self.status.changed()

    def status_changed(self) -> None:
        self.status.changed()

    def received(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "config":
            self.ctx.ha = HaConfig.from_message(message)
            for module in self.modules.values():
                module.configure()
            self.changed()
        elif kind == "input":
            value = message["value"]
            self.brightness.input(message["name"], float(value) if value is not None else None, float(message["age"]))
        elif kind == "command":
            self.command(message)
        elif kind == "ack" and self.down is not None and message["id"] == self.down[0]:
            self.finish_down()

    def owner(self, name: str) -> Module | None:
        for module in self.modules.values():
            if name in module.commands:
                return module
        return None

    def command(self, message: dict[str, Any]) -> None:
        command_id, name, args = message["id"], message["name"], message["args"]
        answered = False

        def reply(data: dict[str, Any] | None, error: CommandError | None) -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            if error is not None:
                log.info("%s refused: %s (%s)", name, error.message, error.code)
                self.client.send({"type": "result", "id": command_id, "ok": False, "error": error.wire()})
            else:
                self.client.send({"type": "result", "id": command_id, "ok": True, "data": data or {}})
            self.changed()

        module = self.owner(name)
        try:
            if module is None:
                raise CommandError("bad_request", f"unknown command {name}")
            if self.observe_only:
                raise CommandError("observe_only", "the agent runs in observe-only mode and executes nothing")
            if not module.enabled:
                raise CommandError("disabled", f"{module.id} is turned off in torpor.conf")
            if not self.ctx.ha.wants(module.id):
                raise CommandError("disabled", f"{module.id} is turned off in Home Assistant")
            allowed = self.config.allow.get(ALLOW_KEY.get(name, name), False)
            if not allowed and not (name == "keep_awake" and args.get("on") is False):
                raise CommandError("not_allowed", f"[allow] in torpor.conf does not allow {name}")
            module.command(name, args, reply)
        except CommandError as error:
            reply(None, error)

    def going_down(self, kind: str) -> None:
        self.client.pause()
        message: dict[str, Any] = {"type": "going_down", "kind": kind}
        detail = self.power.detail() if kind == "sleep" else None
        if detail:
            message["detail"] = detail
        if not self.client.connected:
            self.finish_down()
            return
        self.down_count += 1
        down_id = f"g{self.down_count}"
        if not self.client.send({**message, "id": down_id}):
            self.finish_down()
            return
        self.down = (down_id, self.rt.loop.call_later(protocol.GOING_DOWN_ACK_S, self.finish_down))

    def finish_down(self) -> None:
        if self.down is not None:
            self.rt.loop.cancel(self.down[1])
            self.down = None
        self.client.close(1001, "going down")
        self.core.release_delay()
        self.status.changed()

    def came_back(self, resumed: bool) -> None:
        if self.down is not None:
            self.rt.loop.cancel(self.down[1])
            self.down = None
        if resumed:
            self.events.append({"type": "event", "name": "resumed"})
            self.games.resumed()
            for module in self.modules.values():
                module.resumed()
        self.client.resume()
        self.changed()

    def stop(self) -> None:
        self.stopping = True
        self.rt.loop.cancel(self.tick_timer)
        self.rt.loop.cancel(self.key_timer)
        self.client.stop()
        for module in self.modules.values():
            module.stop()
        self.status.write()
        log.info("torpord stopped")

    def status_data(self) -> dict[str, Any]:
        modules = {
            name: {key: caps.get(key) for key in ("enabled", "available", "backend", "problems")}
            for name, caps in ((n, m.capabilities()) for n, m in self.modules.items())
        }
        now = self.rt.loop.time()
        leases = scan_leases(self.paths.leases_dir, now=now, ttl=self.config.lease_ttl, proc=self.rt.proc)
        return {
            "agent": __version__,
            "proto": protocol.PROTO,
            "pid": self.rt.pid,
            "started": self.started,
            "observe_only": self.observe_only,
            "paired": self.credentials() is not None,
            "ha_url": self.config.ha_url,
            "connection": self.client.status() if not self.stopping else {**self.client.status(), "state": "stopped"},
            "state": self.sent_state if self.sent_state is not None else self.state(),
            "modules": modules,
            "displays": self.brightness.summary(),
            "leases": [lease.name for lease in leases],
        }


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="torpord", description="The Torpor agent.")
    parser.add_argument("--config", type=Path, help="torpor.conf to read (default: ~/.config/torpor/torpor.conf)")
    parser.add_argument("--observe-only", action="store_true", help="report, but execute nothing")
    parser.add_argument("--self-test", action="store_true", help="check this interpreter and exit")
    parser.add_argument("--version", action="version", version=f"torpord {__version__} (proto {protocol.PROTO})")
    return parser.parse_args(argv)


def log_issues(issues: list[Issue]) -> None:
    for issue in issues:
        if issue.level == "warn":
            log.warning("torpor.conf: %s", issue.message)
        else:
            log.error("torpor.conf: %s", issue.message)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        try:
            from torpor.glib import self_test
        except (ImportError, ValueError) as error:
            print(f"self-test failed: {type(error).__name__}: {error}")
            return 1
        return self_test()
    paths = Paths.from_env()
    config, issues = load(args.config or paths.config_file)
    logs.setup(config.log_level)
    log_issues(issues)
    from torpor.glib import run_agent

    try:
        return run_agent(paths, config, observe_only=args.observe_only or config.observe_only)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
