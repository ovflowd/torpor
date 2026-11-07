"""The agent on a fake machine: fake loop and clock, fake buses, a fake Home Assistant, synthetic trees."""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fakes.bus import FakeBus, World
from fakes.ha import FakeHA
from fakes.loop import FakeLoop
from fakes.mode_ddcutil import ModeDdcutil
from fakes.tree import Tree
from torpor import config as conf
from torpor import host
from torpor.agent import Agent
from torpor.paths import Paths
from torpor.runtime import Runtime

FAKES = Path(__file__).parent / "fakes"
UID = 1000
PID = 4242


def has_gi() -> bool:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("Soup", "3.0")
        from gi.repository import Gio, Soup  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def wrapper(folder: Path, name: str, *argv: str) -> Path:
    path = folder / name
    quoted = " ".join(f'"{arg}"' for arg in argv)
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" {quoted} "$@"\n')
    path.chmod(0o755)
    return path


@dataclass
class Tools:
    folder: Path
    state_path: Path
    ddcutil_state: Path

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def set(self, **values: Any) -> None:
        self.state_path.write_text(json.dumps({**self.state(), **values}))

    def calls(self, tool: str | None = None) -> list[list[str]]:
        return [call for call in self.state().get("calls", []) if tool is None or call[0] == tool]

    def ddc(self) -> dict[str, Any]:
        return json.loads(self.ddcutil_state.read_text())

    def set_ddc(self, **values: Any) -> None:
        self.ddcutil_state.write_text(json.dumps({**self.ddc(), **values}))

    def env(self) -> dict[str, str]:
        return {
            "FAKE_TOOLS_STATE": str(self.state_path),
            "FAKE_DDCUTIL_STATE": str(self.ddcutil_state),
            "TORPOR_SYSTEMCTL": str(self.folder / "systemctl"),
            "TORPOR_SYSTEMD_RUN": str(self.folder / "systemd-run"),
            "TORPOR_LOGINCTL": str(self.folder / "loginctl"),
            "TORPOR_JOURNALCTL": str(self.folder / "journalctl"),
            "TORPOR_NMCLI": str(self.folder / "nmcli"),
            "TORPOR_DDCUTIL": str(self.folder / "ddcutil"),
            "TORPOR_PYTHON": str(self.folder / "python"),
        }


def make_tools(folder: Path) -> Tools:
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("systemctl", "systemd-run", "loginctl", "journalctl", "nmcli", "python"):
        wrapper(folder, name, str(FAKES / "fake_tools.py"), name)
    wrapper(folder, "ddcutil", str(FAKES / "fake_ddcutil.py"))
    state = folder / "state.json"
    state.write_text(
        json.dumps(
            {
                "linger": True,
                "probe": {
                    "python": "3.14.0",
                    "pygobject": "3.58.1",
                    "glib": "2.86.0",
                    "soup": "3.8.1",
                    "cryptography": "50.0.2",
                },
            }
        )
    )
    ddc = folder / "ddcutil.json"
    ddc.write_text(json.dumps({"version": "3.0.2", "monitors": []}))
    return Tools(folder, state, ddc)


@dataclass
class Env:
    tmp: Path
    tree: Tree
    tools: Tools
    loop: FakeLoop = field(default_factory=FakeLoop)
    agent: Agent | None = None
    key: bytes = b""

    def __post_init__(self) -> None:
        self.world = World(self.loop, uid=UID)
        self.world.backlight = self.tree.set_backlight
        self.ddc = ModeDdcutil(self.world, self.agent_state)
        self.loop.spawner = self.ddc
        self.ha = FakeHA(self.loop)
        self.home = self.tmp / "home"
        self.home.mkdir(exist_ok=True)
        self.env = {
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.tmp / "run-user"),
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={self.tmp / 'no-system-bus'}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.tmp / 'no-session-bus'}",
            **self.tree.env(),
            **self.tools.env(),
        }
        self.paths = Paths.from_env(self.env)
        self.agent_id = host.agent_id(self.env)

    def agent_state(self) -> str | None:
        """The state the fake ddcutil checks calls against; "going down" once nothing may touch a monitor."""
        if self.agent is not None and (self.agent.core.down or self.agent.stopping):
            return "going down"
        facts = self.agent.facts if self.agent is not None else None
        return facts.state if facts is not None else None

    def runtime(self) -> Runtime:
        return Runtime(
            loop=self.loop,
            system=FakeBus(self.world, "system"),
            session=FakeBus(self.world, "session"),
            transport=self.ha.transport(),
            sysfs=self.tree.sysfs,
            proc=self.tree.proc,
            env=self.env,
            uid=UID,
            pid=PID,
        )

    def pair(self, url: str = "http://192.0.2.10:8123") -> None:
        self.key = os.urandom(32)
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.key_file.write_text(base64.b64encode(self.key).decode() + "\n")
        self.write_config(conf.set_value(self.config_text(), "agent", "ha-url", url))
        self.ha.entries[self.agent_id] = self.key

    def config_text(self) -> str:
        return self.paths.config_file.read_text() if self.paths.config_file.exists() else conf.DEFAULT_TEXT

    def write_config(self, text: str) -> None:
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(text)

    def set(self, key: str, value: str) -> None:
        section, name, text = conf.validate(key, value)
        self.write_config(conf.set_value(self.config_text(), section, name, text))

    def start(self, *, observe_only: bool = False) -> Agent:
        config, issues = conf.load(self.paths.config_file)
        assert not [i for i in issues if i.level == "fail"], issues
        self.agent = Agent(self.runtime(), config, self.paths, observe_only=observe_only or config.observe_only)
        # No reconnect jitter: a test's timing must not depend on chance.
        self.agent.client.jitter = lambda: 0.5
        self.agent.start()
        self.loop.run_due()
        return self.agent

    def restart(self) -> Agent:
        assert self.agent is not None
        self.agent.stop()
        self.loop.run_due()
        return self.start()

    def advance(self, seconds: float, step: float = 1.0) -> None:
        self.loop.advance(seconds, step)

    def desk(self, *, locked: bool = False, plasma: bool = True) -> None:
        self.world.login("3", locked=locked)
        self.tree.connector("card0-DP-2", dpms="On")
        if plasma:
            self.world.plasma()

    def states(self) -> list[dict[str, Any]]:
        return self.ha.received("state")

    def last_state(self) -> dict[str, Any]:
        states = self.states()
        assert states, "no state sent"
        return states[-1]

    def capabilities(self) -> dict[str, Any]:
        caps = self.ha.received("capabilities")
        assert caps, "no capabilities sent"
        return caps[-1]

    def result(self, command_id: str) -> dict[str, Any]:
        self.loop.run_due()
        return self.ha.results[command_id]


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path / "root")


@pytest.fixture
def tools(tmp_path: Path) -> Tools:
    return make_tools(tmp_path / "tools")


@pytest.fixture
def env(tmp_path: Path, tree: Tree, tools: Tools) -> Iterator[Env]:
    instance = Env(tmp_path, tree, tools)
    yield instance
    if instance.agent is not None and not instance.agent.stopping:
        instance.agent.stop()
    instance.world.close()
    assert not instance.ddc.violations + instance.ddc.writes_during_calls(), instance.ddc.summary()


@pytest.fixture
def cli_env(env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    """The same fake machine, exported to os.environ for torporctl run in-process."""
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "TORPOR_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.env.items():
        monkeypatch.setenv(name, value)
    return env


gi_only = pytest.mark.skipif(not has_gi(), reason="needs PyGObject with Gio and Soup 3")
