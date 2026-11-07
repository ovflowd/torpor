"""The GLib side for real: Gio on a private dbus-daemon, libsoup 3 against a real WebSocket server, and
`torpord --self-test`, also under the kernel's MemoryDenyWriteExecute (A15). Skipped without PyGObject.

Nothing here touches the system or session bus: the D-Bus test runs its own dbus-daemon with its
own socket, and the WebSocket server listens on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import FAKES, gi_only
from fakes.tree import Tree
from torpor import protocol
from torpor.bus import BusError, Variant
from torpor.client import Client, Identity

pytestmark = gi_only
REPO = Path(__file__).resolve().parent.parent
LOGIN = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
MDWE = """
import ctypes, os, sys
if ctypes.CDLL(None, use_errno=True).prctl(65, 1, 0, 0, 0) != 0:
    sys.exit("PR_SET_MDWE failed: " + os.strerror(ctypes.get_errno()))
os.execv(sys.executable, [sys.executable] + sys.argv[1:])
"""


@pytest.fixture
def private_bus(tmp_path: Path) -> Iterator[str]:
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        pytest.skip("no dbus-daemon")
    conf = tmp_path / "bus.conf"
    conf.write_text(BUS_CONF.format(dir=tmp_path))
    bus = subprocess.Popen(
        [daemon, f"--config-file={conf}", "--print-address=1", "--nofork"], stdout=subprocess.PIPE, text=True
    )
    assert bus.stdout is not None
    address = bus.stdout.readline().strip()
    service = subprocess.Popen(
        [sys.executable, "-I", "-B", str(FAKES / "dbus_service.py"), address],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert service.stdout is not None
    try:
        assert service.stdout.readline().strip() == "ready"
        yield address
    finally:
        assert service.stdin is not None
        service.stdin.close()
        service.wait(10)
        bus.terminate()
        bus.wait(10)


def test_gio_bus_calls_signals_fds_properties_and_errors(private_bus: str) -> None:
    from gi.repository import Gio

    from torpor.glib import GioBus, GLibLoop

    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    bus = GioBus(Gio.DBusConnection.new_for_address_sync(private_bus, flags, None, None))
    (rows,) = bus.call(LOGIN, "ListSessions", reply="a(susso)")
    assert rows == [("3", 1000, "alex", "seat0", "/s/3")]
    fd = bus.call_with_fd(LOGIN, "Inhibit", ("sleep:shutdown", "Torpor", "test", "delay"), signature="ssss")
    os.fstat(fd)
    os.close(fd)
    echo = bus.call(LOGIN, "Echo", ("x", 7, {"reason": Variant("s", "why")}), signature="sua{sv}", reply="s")
    assert echo == ("x:7:why",)
    display = ("org.kde.ScreenBrightness", "/org/kde/ScreenBrightness/display0", "org.kde.ScreenBrightness.Display")
    assert bus.get(*display, "Label") == "Example 27Q"
    assert bus.get_all(*display) == {"Label": "Example 27Q", "MaxBrightness": 10000}
    assert bus.has_owner("org.freedesktop.login1") is True
    assert bus.has_owner("org.gnome.SessionManager") is False
    with pytest.raises(BusError) as missing:
        bus.call(("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager"), "Inhibit")
    assert missing.value.name == "org.freedesktop.DBus.Error.NameHasNoOwner"
    signals: list[tuple[Any, ...]] = []
    owners: list[str | None] = []
    results: list[tuple[Any, BusError | None]] = []
    bus.subscribe(LOGIN[0], LOGIN[2], "PrepareForSleep", LOGIN[1], lambda _path, args: signals.append(args))
    bus.watch_name(LOGIN[0], owners.append)
    bus.call_async(
        LOGIN, "Suspend", (False,), signature="b", done=lambda result, error: results.append((result, error))
    )
    assert GLibLoop().run_until(lambda: bool(results and signals and owners), 10)
    result, error = results[0]
    assert result is None and error is not None
    assert error.name == "org.freedesktop.DBus.Error.AccessDenied" and error.denied
    assert signals == [(True,)]
    assert owners[0] is not None and owners[0].startswith(":")


class Recorder:
    def __init__(self) -> None:
        self.client: Client | None = None
        self.events: list[str] = []

    def authenticated(self) -> None:
        self.events.append("authenticated")
        assert self.client is not None
        self.client.send({"type": "state", "state": "idle"})

    def received(self, message: dict[str, Any]) -> None:
        self.events.append(message["type"])

    def disconnected(self) -> None:
        self.events.append("disconnected")

    def status_changed(self) -> None:
        return None


def test_soup_transport_runs_the_real_protocol_against_a_websocket_server() -> None:
    from gi.repository import Soup

    from torpor.glib import GLibLoop, SoupTransport

    key = os.urandom(protocol.KEY_BYTES)
    agent_id = "1f399f30-a3d9-488e-a63c-cbeb7333a961"
    received: list[dict[str, Any]] = []
    keep: list[Any] = []

    def handler(_server: Any, _msg: Any, _path: str, conn: Any) -> None:
        state: dict[str, Any] = {}
        keep.append(conn)

        def message(ws: Any, _kind: Any, data: Any) -> None:
            raw = bytes(data.get_data())
            if "handshake" not in state:
                hello = protocol.parse_hello(raw.decode())
                state["handshake"] = protocol.HomeAssistantHandshake(
                    hello, ephemeral=protocol.Ephemeral.generate(), secret=key
                )
                ws.send_text(state["handshake"].challenge())
            elif "session" not in state:
                state["session"] = state["handshake"].verify(raw.decode())
                auth_ok = {"type": "auth_ok", "pairing": False, "ha_version": "2026.11.0", "integration": "0.1.0"}
                ws.send_binary(state["session"].seal(auth_ok))
                ws.send_binary(state["session"].seal({"type": "config", "modules": {}}))
            else:
                received.append(state["session"].open(raw))
                ws.close(protocol.Close.SUPERSEDED, "superseded")

        conn.connect("message", message)

    server = Soup.Server()
    server.add_websocket_handler(protocol.WS_PATH, None, None, handler)
    server.listen_local(0, Soup.ServerListenOptions.IPV4_ONLY)
    port = server.get_uris()[0].get_port()
    loop = GLibLoop()
    recorder = Recorder()
    client = Client(
        loop,
        SoupTransport(),
        identity=Identity(agent_id, "0.1.0"),
        credentials=lambda: (f"http://127.0.0.1:{port}", key),
        tls_verify=True,
        handler=recorder,
    )
    recorder.client = client
    client.start()
    assert loop.run_until(lambda: client.last_close is not None, 10)
    assert received == [{"type": "state", "state": "idle"}]
    assert recorder.events == ["authenticated", "config", "disconnected"]
    assert client.last_close is not None and client.last_close["code"] == protocol.Close.SUPERSEDED
    assert client.state == "backoff"
    client.stop()

    missing = Client(
        loop,
        SoupTransport(),
        identity=Identity(agent_id, "0.1.0"),
        credentials=lambda: (f"http://127.0.0.1:{port}/elsewhere", key),
        tls_verify=True,
        handler=Recorder(),
    )
    missing.start()
    assert loop.run_until(lambda: missing.last_close is not None, 10)
    assert missing.last_close is not None and missing.last_close["http"] == 404
    missing.stop()
    server.disconnect()


def self_test(*prefix: str, python: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    argv = [*(python or [sys.executable]), *prefix, "-B", "-m", "torpor", "agent", "--self-test"]
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=60)


def test_self_test_passes() -> None:
    done = self_test()
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr


def test_self_test_passes_under_memory_deny_write_execute() -> None:
    done = subprocess.run(
        [sys.executable, "-c", MDWE, "-B", "-m", "torpor", "agent", "--self-test"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if "PR_SET_MDWE failed" in done.stderr:
        pytest.skip(done.stderr.strip())
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr


def test_the_zipapp_passes_the_self_test_as_the_unit_runs_it(tmp_path: Path) -> None:
    from torpor.install import build_zipapp

    pyz = tmp_path / "torpor.pyz"
    build_zipapp(pyz)
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(pyz), "agent", "--self-test"], capture_output=True, text=True, timeout=60
    )
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr


def waiting_for_a_key(status: Path) -> bool:
    if not status.exists():
        return False
    connection = json.loads(status.read_text()).get("connection") or {}
    return bool(connection.get("state") == "not_paired")


def test_torpord_runs_its_main_loop_until_sigterm_and_exits_0(tmp_path: Path, tree: Tree) -> None:
    """The real main loop and signal handlers on this interpreter's GLib, which --self-test doesn't reach.

    Both buses point at a socket that doesn't exist, so it never talks to the real ones.
    """
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    nowhere = f"unix:path={tmp_path / 'no-bus'}"
    env = {
        **tree.env(),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SYSTEM_BUS_ADDRESS": nowhere,
        "DBUS_SESSION_BUS_ADDRESS": nowhere,
        "GSETTINGS_BACKEND": "memory",
    }
    agent = subprocess.Popen(
        [sys.executable, "-B", "-m", "torpor", "agent"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    status = runtime / "torpor" / "status.json"
    deadline = time.monotonic() + 20
    while agent.poll() is None and time.monotonic() < deadline and not waiting_for_a_key(status):
        time.sleep(0.05)
    agent.send_signal(signal.SIGTERM)
    output, _ = agent.communicate(timeout=20)
    assert agent.returncode == 0, output
    assert "torpord stopped" in output


@pytest.mark.skipif(bool(os.environ.get("TORPOR_TEST_MDWE")), reason="already under MemoryDenyWriteExecute")
def test_the_private_bus_and_websocket_tests_pass_under_memory_deny_write_execute() -> None:
    """The unit's MemoryDenyWriteExecute=yes breaks PyGObject callbacks where libffi needs W+X memory."""
    tests = "tests/test_glib.py::test_gio_bus_calls_signals_fds_properties_and_errors"
    websocket = "tests/test_glib.py::test_soup_transport_runs_the_real_protocol_against_a_websocket_server"
    done = subprocess.run(
        [sys.executable, "-c", MDWE, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", tests, websocket],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "TORPOR_TEST_MDWE": "1"},
    )
    if "PR_SET_MDWE failed" in done.stderr:
        pytest.skip(done.stderr.strip())
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "2 passed" in done.stdout
