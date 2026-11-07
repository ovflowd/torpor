"""The GLib side: main loop, Gio D-Bus, libsoup 3 WebSocket, worker thread, signals and --self-test.

Everything that imports PyGObject lives here, so the rest of the package imports without it.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup  # noqa: E402

from torpor import protocol  # noqa: E402
from torpor.bus import BusError, Done, SignalCallback, Target, Variant  # noqa: E402
from torpor.config import Config  # noqa: E402
from torpor.paths import Paths  # noqa: E402
from torpor.runtime import Result, Runtime, TransportEvents, roots  # noqa: E402

log = logging.getLogger("torpor")

NO_AUTO_START = Gio.DBusCallFlags.NO_AUTO_START
DBUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
ABNORMAL = 1006
QUIT_AFTER_MS = 300


def guarded(callback: Callable[..., Any], *args: Any) -> None:
    try:
        callback(*args)
    except Exception:
        log.exception("unexpected error in a callback")


class _Handle:
    __slots__ = ("source",)

    def __init__(self) -> None:
        self.source: int | None = None


class GLibLoop:
    def __init__(self) -> None:
        self.jobs: queue.Queue[tuple[Callable[[], Any], Callable[[Any, BaseException | None], None]]] = queue.Queue()
        self.thread: threading.Thread | None = None

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def call_later(self, delay: float, callback: Callable[[], None]) -> object:
        handle = _Handle()

        def fire() -> bool:
            handle.source = None
            guarded(callback)
            return False

        handle.source = GLib.timeout_add(max(0, int(delay * 1000)), fire)
        return handle

    def cancel(self, handle: object | None) -> None:
        if isinstance(handle, _Handle) and handle.source is not None:
            GLib.source_remove(handle.source)
            handle.source = None

    def _work(self) -> None:
        while True:
            work, done = self.jobs.get()
            result: Any = None
            failure: BaseException | None = None
            try:
                result = work()
            except Exception as error:
                failure = error

            def deliver(
                done: Callable[[Any, BaseException | None], None] = done,
                result: Any = result,
                failure: BaseException | None = failure,
            ) -> bool:
                guarded(done, result, failure)
                return False

            GLib.idle_add(deliver)

    def run_in_worker(self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, name="torpor-worker", daemon=True)
            self.thread.start()
        self.jobs.put((work, done))

    def spawn(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None, done: Callable[[Result], None]
    ) -> None:
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        if env is not None:
            launcher.set_environ([f"{key}={value}" for key, value in env.items()])
        name = os.path.basename(argv[0])
        try:
            process = launcher.spawnv(list(argv))
        except GLib.Error as error:
            failed = Result(None, f"cannot run {argv[0]}: {error.message}")

            def report() -> bool:
                guarded(done, failed)
                return False

            GLib.idle_add(report)
            return
        killed = False

        def kill() -> bool:
            nonlocal killed
            killed = True
            process.force_exit()
            return False

        timer = GLib.timeout_add(max(1, int(timeout * 1000)), kill)

        def finish(source: Gio.Subprocess, result: Gio.AsyncResult) -> None:
            if not killed:
                GLib.source_remove(timer)
            try:
                _ok, stdout, _stderr = source.communicate_finish(result)
            except GLib.Error as error:
                guarded(done, Result(None, error.message))
                return
            if killed:
                guarded(done, Result(None, f"{name} timed out after {timeout:g} s"))
                return
            output = bytes(stdout.get_data() or b"").decode(errors="replace") if stdout is not None else ""
            guarded(done, Result(source.get_exit_status() if source.get_if_exited() else None, output))

        process.communicate_async(None, None, finish)

    def run_until(self, done: Callable[[], bool], timeout: float) -> bool:
        loop = GLib.MainLoop()
        deadline = time.monotonic() + timeout

        def check() -> bool:
            if done() or time.monotonic() >= deadline:
                loop.quit()
                return False
            return True

        GLib.timeout_add(50, check)
        loop.run()
        return done()


def _convert(value: Any) -> Any:
    if isinstance(value, Variant):
        return GLib.Variant(value.signature, _convert(value.value))
    if isinstance(value, dict):
        return {key: _convert(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_convert(item) for item in value)
    return value


def _params(signature: str, args: tuple[Any, ...]) -> GLib.Variant | None:
    if not signature:
        return None
    return GLib.Variant(f"({signature})", _convert(args))


def _error(error: GLib.Error) -> BusError:
    name = Gio.DBusError.get_remote_error(error) or ""
    if name:
        Gio.DBusError.strip_remote_error(error)
    return BusError(name, error.message)


class GioBus:
    def __init__(self, connection: Gio.DBusConnection) -> None:
        self.connection = connection

    def call(
        self,
        target: Target,
        method: str,
        args: tuple[Any, ...] = (),
        *,
        signature: str = "",
        reply: str | None = None,
        timeout_ms: int = 5000,
    ) -> tuple[Any, ...]:
        name, path, iface = target
        reply_type = GLib.VariantType(f"({reply})") if reply is not None else None
        try:
            result = self.connection.call_sync(
                name, path, iface, method, _params(signature, args), reply_type, NO_AUTO_START, timeout_ms, None
            )
        except GLib.Error as error:
            raise _error(error) from None
        return tuple(result.unpack()) if result is not None else ()

    def call_async(
        self,
        target: Target,
        method: str,
        args: tuple[Any, ...] = (),
        *,
        signature: str = "",
        done: Done,
        timeout_ms: int = 25000,
    ) -> None:
        name, path, iface = target

        def finish(source: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
            try:
                value = source.call_finish(result)
            except GLib.Error as error:
                guarded(done, None, _error(error))
                return
            guarded(done, tuple(value.unpack()) if value is not None else (), None)

        self.connection.call(
            name, path, iface, method, _params(signature, args), None, NO_AUTO_START, timeout_ms, None, finish
        )

    def call_with_fd(
        self, target: Target, method: str, args: tuple[Any, ...], *, signature: str, timeout_ms: int = 5000
    ) -> int:
        name, path, iface = target
        try:
            result, fds = self.connection.call_with_unix_fd_list_sync(
                name,
                path,
                iface,
                method,
                _params(signature, args),
                GLib.VariantType("(h)"),
                NO_AUTO_START,
                timeout_ms,
                None,
                None,
            )
        except GLib.Error as error:
            raise _error(error) from None
        (index,) = result.unpack()
        return int(fds.steal_fds()[index])

    def get(self, name: str, path: str, iface: str, prop: str, *, timeout_ms: int = 1500) -> Any:
        (value,) = self.call(
            (name, path, "org.freedesktop.DBus.Properties"),
            "Get",
            (iface, prop),
            signature="ss",
            reply="v",
            timeout_ms=timeout_ms,
        )
        return value

    def get_all(self, name: str, path: str, iface: str, *, timeout_ms: int = 1500) -> dict[str, Any]:
        (values,) = self.call(
            (name, path, "org.freedesktop.DBus.Properties"),
            "GetAll",
            (iface,),
            signature="s",
            reply="a{sv}",
            timeout_ms=timeout_ms,
        )
        return dict(values)

    def subscribe(self, sender: str | None, iface: str, member: str, path: str | None, callback: SignalCallback) -> int:
        def handler(_conn: Any, _sender: str, object_path: str, _iface: str, _member: str, params: Any) -> None:
            guarded(callback, object_path, tuple(params.unpack()))

        return int(
            self.connection.signal_subscribe(sender, iface, member, path, None, Gio.DBusSignalFlags.NONE, handler)
        )

    def watch_name(self, name: str, callback: Callable[[str | None], None]) -> int:
        return int(
            Gio.bus_watch_name_on_connection(
                self.connection,
                name,
                Gio.BusNameWatcherFlags.NONE,
                lambda _conn, _name, owner: guarded(callback, owner),
                lambda _conn, _name: guarded(callback, None),
            )
        )

    def has_owner(self, name: str) -> bool:
        try:
            (owned,) = self.call(DBUS, "NameHasOwner", (name,), signature="s", reply="b", timeout_ms=1500)
        except BusError:
            return False
        return bool(owned)


class _SoupConnection:
    def __init__(self, message: Soup.Message, events: TransportEvents) -> None:
        self.message = message
        self.events = events
        self.cancellable = Gio.Cancellable()
        self.ws: Soup.WebsocketConnection | None = None
        self.closed = False

    def connected(self, session: Soup.Session, result: Gio.AsyncResult) -> None:
        try:
            ws = session.websocket_connect_finish(result)
        except GLib.Error as error:
            if self.closed:
                return
            self.closed = True
            status = int(self.message.get_status())
            http = status if status >= 100 and status != 101 else None
            guarded(self.events.closed, None, error.message, http)
            return
        if self.closed:
            ws.close(1000, None)
            return
        self.ws = ws
        ws.set_max_incoming_payload_size(protocol.MAX_FRAME + 1024)
        ws.set_keepalive_interval(0)
        ws.connect("message", self.received)
        ws.connect("closed", self.lost)
        guarded(self.events.opened)

    def received(self, _ws: Any, kind: Soup.WebsocketDataType, data: GLib.Bytes) -> None:
        raw = bytes(data.get_data() or b"")
        if kind == Soup.WebsocketDataType.TEXT:
            guarded(self.events.text, raw.decode("utf-8", errors="replace"))
        else:
            guarded(self.events.binary, raw)

    def lost(self, ws: Soup.WebsocketConnection) -> None:
        if self.closed:
            return
        self.closed = True
        code = int(ws.get_close_code()) or ABNORMAL
        guarded(self.events.closed, code, ws.get_close_data() or "", None)

    def send_text(self, text: str) -> None:
        if self.ws is not None and not self.closed:
            self.ws.send_text(text)

    def send_binary(self, data: bytes) -> None:
        if self.ws is not None and not self.closed:
            self.ws.send_binary(data)

    def close(self, code: int, reason: str = "") -> None:
        self.closed = True
        if self.ws is None:
            self.cancellable.cancel()
        elif self.ws.get_state() == Soup.WebsocketState.OPEN:
            self.ws.close(code, reason or None)


class SoupTransport:
    def __init__(self) -> None:
        self.session = Soup.Session()

    def connect(self, url: str, events: TransportEvents, *, tls_verify: bool) -> _SoupConnection:
        message = Soup.Message.new("GET", url)
        if message is None:
            raise ValueError(f"not a URL: {url}")
        if not tls_verify:
            message.connect("accept-certificate", lambda *_args: True)
        connection = _SoupConnection(message, events)
        self.session.websocket_connect_async(
            message, None, None, GLib.PRIORITY_DEFAULT, connection.cancellable, connection.connected
        )
        return connection


def bus(kind: Gio.BusType) -> GioBus | None:
    try:
        return GioBus(Gio.bus_get_sync(kind, None))
    except GLib.Error as error:
        log.warning("no %s bus: %s", "system" if kind == Gio.BusType.SYSTEM else "session", error.message)
        return None


def buses() -> tuple[GioBus | None, GioBus | None]:
    return bus(Gio.BusType.SYSTEM), bus(Gio.BusType.SESSION)


def build_runtime(env: dict[str, str] | None = None) -> Runtime:
    env = dict(os.environ if env is None else env)
    sysfs, proc = roots(env)
    return Runtime(
        loop=GLibLoop(),
        system=bus(Gio.BusType.SYSTEM),
        session=bus(Gio.BusType.SESSION),
        transport=SoupTransport(),
        sysfs=sysfs,
        proc=proc,
        env=env,
        uid=os.getuid(),
        pid=os.getpid(),
    )


def add_signal(signum: int, handler: Callable[[], bool]) -> None:
    """GLib.unix_signal_add is deprecated where GLibUnix.signal_add exists. GLib 2.80 (Ubuntu 24.04) has
    GLibUnix with only signal_add_full, and older GLib has no GLibUnix at all."""
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
    except (ImportError, ValueError):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, handler)
        return
    add = getattr(GLibUnix, "signal_add", None) or GLibUnix.signal_add_full
    add(GLib.PRIORITY_DEFAULT, signum, handler)


def run_agent(paths: Paths, config: Config, *, observe_only: bool) -> int:
    from torpor.agent import Agent

    agent = Agent(build_runtime(), config, paths, observe_only=observe_only)
    main = GLib.MainLoop()

    def stop() -> bool:
        if not agent.stopping:
            agent.stop()
            GLib.timeout_add(QUIT_AFTER_MS, main.quit)
        return True

    for signum in (signal.SIGTERM, signal.SIGINT):
        add_signal(signum, stop)
    agent.start()
    main.run()
    return 0


def self_test() -> int:
    """Checks what the unit's hardening can break: Gio, Soup 3, Python callbacks through libffi, the crypto."""
    try:
        loop = GLib.MainLoop()
        seen: list[str] = []

        def info(source: Gio.File, result: Gio.AsyncResult) -> None:
            source.query_info_finish(result)
            seen.append("async")
            loop.quit()

        def idle() -> bool:
            seen.append("idle")
            Gio.File.new_for_path("/").query_info_async(
                "standard::type", Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT, None, info
            )
            return False

        GLib.idle_add(idle)
        GLib.timeout_add(5000, loop.quit)
        loop.run()
        if seen != ["idle", "async"]:
            raise RuntimeError(f"the GLib main loop ran {seen}, not an idle and an async callback")
        Soup.Session()
        key = os.urandom(protocol.KEY_BYTES)
        agent = protocol.AgentHandshake(
            agent_id="00000000-0000-4000-8000-000000000000",
            agent="self-test",
            ephemeral=protocol.Ephemeral.generate(),
            key=key,
        )
        ha = protocol.HomeAssistantHandshake(
            protocol.parse_hello(agent.hello_text()), ephemeral=protocol.Ephemeral.generate(), secret=key
        )
        auth = agent.answer(ha.challenge())
        session = ha.verify(auth)
        assert agent.session is not None
        if agent.session.open(session.seal({"type": "ping", "t": 1})) != {"type": "ping", "t": 1}:
            raise RuntimeError("a frame did not survive a round trip")
    except Exception as error:
        print(f"self-test failed: {type(error).__name__}: {error}")
        return 1
    print("ok")
    return 0
