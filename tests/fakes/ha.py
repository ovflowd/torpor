"""Home Assistant's side of protocol v1, built on protocol.HomeAssistantHandshake, behind a fake WebSocket.

Messages travel through the fake loop, one turn each way, so ordering and timers behave as they
would over a socket. The fake checks every message it receives with protocol.check().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fakes.loop import FakeLoop
from torpor import protocol
from torpor.protocol import Close, ProtocolError
from torpor.runtime import TransportEvents


class FakeConn:
    """One WebSocket: the agent holds it as its Connection, the fake HA as its peer."""

    def __init__(self, transport: FakeTransport, events: TransportEvents, url: str) -> None:
        self.transport = transport
        self.events = events
        self.url = url
        self.open = True
        self.closed_by: tuple[str, int | None, str] | None = None
        self.hello: protocol.Hello | None = None
        self.handshake: protocol.HomeAssistantHandshake | None = None
        self.session: protocol.Session | None = None
        self.received: list[dict[str, Any]] = []
        self.texts = 0

    def received_of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["type"] == kind]

    def send_text(self, text: str) -> None:
        ha = self.transport.ha
        self.transport.loop.call_later(0, lambda: ha.on_text(self, text))

    def send_binary(self, data: bytes) -> None:
        ha = self.transport.ha
        self.transport.loop.call_later(0, lambda: ha.on_binary(self, data))

    def close(self, code: int, reason: str = "") -> None:
        if not self.open:
            return
        self.open = False
        self.closed_by = ("agent", code, reason)
        self.transport.ha.closes.append(("agent", code, reason))

    def deliver(self, kind: str, data: Any) -> None:
        if not self.open:
            return
        if kind == "text":
            self.transport.loop.call_later(0, lambda: self.open and self.events.text(data))
        else:
            self.transport.loop.call_later(0, lambda: self.open and self.events.binary(data))

    def server_close(self, code: int, reason: str = "") -> None:
        if not self.open:
            return
        self.open = False
        self.closed_by = ("ha", code, reason)
        self.transport.ha.closes.append(("ha", code, reason))
        self.transport.loop.call_later(0, lambda: self.events.closed(code, reason, None))


class FakeTransport:
    def __init__(self, loop: FakeLoop, ha: FakeHA) -> None:
        self.loop = loop
        self.ha = ha
        self.urls: list[str] = []

    def connect(self, url: str, events: TransportEvents, *, tls_verify: bool) -> FakeConn:
        self.urls.append(url)
        conn = FakeConn(self, events, url)
        self.loop.call_later(0, lambda: self.ha.accept(conn))
        return conn


@dataclass
class FakeHA:
    loop: FakeLoop
    entries: dict[str, bytes] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)
    pending: dict[str, bytes] = field(default_factory=dict)
    reachable: bool = True
    http_status: int | None = None
    pair_action: str = "paired"
    auto_ack: bool = True
    send_config: bool = True
    config: dict[str, Any] = field(
        default_factory=lambda: {"modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True}}
    )
    lux: tuple[float | None, float] | None = None
    connections: list[FakeConn] = field(default_factory=list)
    closes: list[tuple[str, int | None, str]] = field(default_factory=list)
    bans: int = 0
    proofs: int = 0
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    paired_acks: int = 0
    next_command: int = 0

    def transport(self) -> FakeTransport:
        return FakeTransport(self.loop, self)

    def accept(self, conn: FakeConn) -> None:
        if not self.reachable or self.http_status is not None:
            conn.open = False
            status = self.http_status
            reason = "connection refused" if status is None else f"HTTP {status}"
            self.loop.call_later(0, lambda: conn.events.closed(None, reason, status))
            return
        self.connections.append(conn)
        conn.events.opened()

    @property
    def current(self) -> FakeConn | None:
        live = [c for c in self.connections if c.open and c.session is not None]
        return live[-1] if live else None

    def received(self, kind: str | None = None) -> list[dict[str, Any]]:
        messages = [m for conn in self.connections for m in conn.received]
        return [m for m in messages if kind is None or m["type"] == kind]

    def on_text(self, conn: FakeConn, text: str) -> None:
        if not conn.open:
            return
        conn.texts += 1
        try:
            if conn.hello is None:
                self.hello(conn, text)
            elif conn.session is None:
                self.auth(conn, text)
            else:
                raise ProtocolError(Close.MALFORMED, "text after the handshake")
        except ProtocolError as error:
            if error.ban:
                self.bans += 1
            if conn.hello is not None and conn.hello.pairing and error.close == Close.AUTH_FAILED:
                self.proofs += 1
            conn.server_close(error.close, error.reason)

    def hello(self, conn: FakeConn, text: str) -> None:
        hello = protocol.parse_hello(text)
        conn.hello = hello
        secret = self.pending.get(hello.pair_id) if hello.pairing else self.entries.get(hello.agent_id)
        conn.handshake = protocol.HomeAssistantHandshake(hello, ephemeral=protocol.Ephemeral.generate(), secret=secret)
        conn.deliver("text", conn.handshake.challenge())

    def auth(self, conn: FakeConn, text: str) -> None:
        assert conn.handshake is not None and conn.hello is not None
        session = conn.handshake.verify(text)
        if not conn.hello.pairing and conn.hello.agent_id in self.disabled:
            conn.server_close(Close.NOT_READY, "entry not loaded")
            return
        conn.session = session
        for other in self.connections:
            if (
                other is not conn
                and other.open
                and other.hello is not None
                and other.hello.agent_id == conn.hello.agent_id
            ):
                other.server_close(Close.SUPERSEDED, "superseded")
        self.send(
            conn, {"type": "auth_ok", "pairing": conn.hello.pairing, "ha_version": "2026.11.0", "integration": "0.1.0"}
        )

    def send(self, conn: FakeConn, message: dict[str, Any]) -> None:
        assert conn.session is not None
        conn.deliver("binary", conn.session.seal(message))

    def on_binary(self, conn: FakeConn, data: bytes) -> None:
        if not conn.open:
            return
        assert conn.session is not None and conn.hello is not None, "binary before the handshake"
        try:
            message = protocol.check(conn.session.open(data), "agent", pairing=conn.hello.pairing)
        except ProtocolError as error:
            conn.server_close(error.close, error.reason)
            return
        conn.received.append(message)
        kind = message["type"]
        if kind == "ping":
            self.send(conn, {"type": "pong", "t": message["t"]})
        elif kind == "result":
            self.results[message["id"]] = message
        elif conn.hello.pairing:
            self.pairing_message(conn, message)
        elif kind == "capabilities" and self.send_config and len(conn.received_of("capabilities")) == 1:
            self.send(conn, {"type": "config", **self.config})
            if self.lux is not None:
                value, age = self.lux
                self.send(conn, {"type": "input", "name": "lux", "value": value, "age": age})
        elif kind == "going_down" and self.auto_ack:
            self.send(conn, {"type": "ack", "id": message["id"]})

    def pairing_message(self, conn: FakeConn, message: dict[str, Any]) -> None:
        assert conn.handshake is not None and conn.hello is not None
        if message["type"] == "capabilities":
            if self.pair_action == "paired":
                self.send(conn, {"type": "paired", "id": "p1"})
            elif self.pair_action == "cancel":
                conn.server_close(Close.PAIRING_CANCELLED, "pairing cancelled")
            elif self.pair_action == "timeout":
                conn.server_close(Close.TIMEOUT, "pairing timed out")
        elif message["type"] == "ack" and message["id"] == "p1":
            self.paired_acks += 1
            assert conn.handshake.pairing_key is not None
            self.entries[conn.hello.agent_id] = conn.handshake.pairing_key
            self.pending.pop(conn.hello.pair_id, None)
            conn.server_close(Close.RECONNECT, "paired")

    def command(self, name: str, **args: Any) -> str:
        conn = self.current
        assert conn is not None, "no connected agent"
        self.next_command += 1
        command_id = f"c{self.next_command}"
        self.send(conn, {"type": "command", "id": command_id, "name": name, "args": args})
        return command_id

    def input(self, value: float | None, age: float = 0.0) -> None:
        conn = self.current
        assert conn is not None
        self.send(conn, {"type": "input", "name": "lux", "value": value, "age": age})

    def push_config(self, **changes: Any) -> None:
        self.config = {**self.config, **changes}
        conn = self.current
        assert conn is not None
        self.send(conn, {"type": "config", **self.config})

    def drop(self, code: int = 1006) -> None:
        conn = self.current
        assert conn is not None
        conn.server_close(code, "")
