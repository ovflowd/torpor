"""The PC's side of protocol v1: dial Home Assistant, run the handshake, exchange encrypted frames.

`Client` keeps the agent connected (reconnect, back-off, keepalive); `Pairing` runs the one-time
pairing for `torporctl pair`. Both work over the `Transport` seam, so the tests drive them with a
fake Home Assistant that speaks the real protocol.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from torpor import protocol
from torpor.codes import meaning
from torpor.protocol import Close, ProtocolError
from torpor.runtime import Connection, Loop, Transport

log = logging.getLogger("torpor")

GOING_AWAY = 1001
STABLE_AFTER_S = 60
JITTER = 0.2
REJECTED = frozenset({Close.AUTH_FAILED, Close.NOT_LOCAL, Close.UNSUPPORTED})


def ws_url(ha_url: str) -> str:
    parts = urlsplit(ha_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + protocol.WS_PATH, "", ""))


@dataclass(frozen=True)
class Identity:
    agent_id: str
    version: str


class Handler(Protocol):
    def authenticated(self) -> None: ...

    def received(self, message: dict[str, Any]) -> None: ...

    def disconnected(self) -> None: ...

    def status_changed(self) -> None: ...


class _Link:
    """One WebSocket connection and its handshake."""

    def __init__(self, handshake: protocol.AgentHandshake) -> None:
        self.handshake = handshake
        self.conn: Connection | None = None
        self.session: protocol.Session | None = None
        self.answered = False
        self.authed = False
        self.done = False
        self.last_rx = 0.0
        self.timers: list[object] = []


class _Events:
    """Routes one connection's transport events to its owner, and drops them once it is closed."""

    def __init__(self, owner: Any, link: _Link) -> None:
        self.owner = owner
        self.link = link

    def opened(self) -> None:
        if not self.link.done:
            self.owner._opened(self.link)

    def text(self, data: str) -> None:
        if not self.link.done:
            self.owner._text(self.link, data)

    def binary(self, data: bytes) -> None:
        if not self.link.done:
            self.owner._binary(self.link, data)

    def closed(self, code: int | None, reason: str, http_status: int | None) -> None:
        if not self.link.done:
            self.owner._closed(self.link, code, reason, http_status, local=False)


class Client:
    def __init__(
        self,
        loop: Loop,
        transport: Transport,
        *,
        identity: Identity,
        credentials: Callable[[], tuple[str, bytes] | None],
        tls_verify: bool,
        handler: Handler,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.loop = loop
        self.transport = transport
        self.identity = identity
        self.credentials = credentials
        self.tls_verify = tls_verify
        self.handler = handler
        self.jitter = jitter
        self.link: _Link | None = None
        self.state = "connecting"
        self.since = loop.time()
        self.failures = 0
        self.last_close: dict[str, Any] | None = None
        self.retry_at: float | None = None
        self.retry_timer: object | None = None
        self.stable_timer: object | None = None
        self.paused = False
        self.stopped = False
        self.next_ping = 1

    @property
    def connected(self) -> bool:
        return self.link is not None and self.link.authed

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.since = self.loop.time()
        self.handler.status_changed()

    def start(self) -> None:
        self.dial()

    def dial(self) -> None:
        self.loop.cancel(self.retry_timer)
        self.retry_timer = None
        self.retry_at = None
        if self.paused or self.stopped or self.link is not None:
            return
        credentials = self.credentials()
        if credentials is None:
            self._set_state("not_paired")
            return
        url, key = credentials
        handshake = protocol.AgentHandshake(
            agent_id=self.identity.agent_id,
            agent=self.identity.version,
            ephemeral=protocol.Ephemeral.generate(),
            key=key,
        )
        link = _Link(handshake)
        self.link = link
        self._set_state("connecting")
        link.timers.append(self.loop.call_later(protocol.HANDSHAKE_TIMEOUT_S, lambda: self._handshake_timeout(link)))
        log.debug("dialing %s", ws_url(url))
        try:
            link.conn = self.transport.connect(ws_url(url), _Events(self, link), tls_verify=self.tls_verify)
        except (OSError, ValueError) as error:
            self._closed(link, None, f"can't connect: {error}", None, local=True)

    def _handshake_timeout(self, link: _Link) -> None:
        if not link.authed and not link.done:
            self.close_link(link, Close.TIMEOUT, "handshake timed out")

    def _opened(self, link: _Link) -> None:
        assert link.conn is not None
        link.conn.send_text(link.handshake.hello_text())
        log.debug("sent hello")

    def _text(self, link: _Link, data: str) -> None:
        if link.answered:
            self.close_link(link, Close.MALFORMED, "text message after the handshake")
            return
        try:
            auth = link.handshake.answer(data)
        except ProtocolError as error:
            if error.close == Close.AUTH_FAILED:
                log.warning("Home Assistant did not prove the pairing key: re-pair needed (torporctl pair)")
            self.close_link(link, error.close, error.reason)
            return
        link.answered = True
        link.session = link.handshake.session
        assert link.conn is not None
        link.conn.send_text(auth)
        log.debug("sent auth")

    def _binary(self, link: _Link, data: bytes) -> None:
        if link.session is None:
            self.close_link(link, Close.MALFORMED, "binary message before the handshake")
            return
        try:
            message = protocol.check(link.session.open(data), "ha")
        except ProtocolError as error:
            self.close_link(link, error.close, error.reason)
            return
        link.last_rx = self.loop.monotonic()
        kind = message["type"]
        if not link.authed:
            if kind != "auth_ok" or message.get("pairing"):
                self.close_link(link, Close.MALFORMED, "expected auth_ok")
                return
            self._authenticated(link, message)
            return
        log.debug("received %s", kind)
        if kind == "ping":
            self.send({"type": "pong", "t": message["t"]})
            return
        if kind == "pong":
            return
        self.handler.received(message)

    def _authenticated(self, link: _Link, message: dict[str, Any]) -> None:
        link.authed = True
        log.info("connected to Home Assistant %s (integration %s)", message["ha_version"], message["integration"])
        self.last_close = None
        self._set_state("connected")
        self.stable_timer = self.loop.call_later(STABLE_AFTER_S, self._stable)
        link.timers.append(self.loop.call_later(protocol.PING_INTERVAL_S, lambda: self._keepalive(link)))
        self.handler.authenticated()

    def _stable(self) -> None:
        self.stable_timer = None
        self.failures = 0

    def _keepalive(self, link: _Link) -> None:
        if link.done:
            return
        if self.loop.monotonic() - link.last_rx >= protocol.IDLE_TIMEOUT_S:
            self.close_link(link, Close.TIMEOUT, f"no frame for {protocol.IDLE_TIMEOUT_S} s")
            return
        self.send({"type": "ping", "t": self.next_ping})
        self.next_ping += 1
        link.timers.append(self.loop.call_later(protocol.PING_INTERVAL_S, lambda: self._keepalive(link)))

    def send(self, message: dict[str, Any]) -> bool:
        link = self.link
        if link is None or not link.authed or link.session is None or link.conn is None:
            return False
        link.conn.send_binary(link.session.seal(message))
        log.debug("sent %s", message["type"])
        return True

    def close_link(self, link: _Link, code: int, reason: str) -> None:
        if link.done:
            return
        if link.conn is not None:
            link.conn.close(code, reason[:120])
        self._closed(link, code, reason, None, local=True)

    def close(self, code: int = GOING_AWAY, reason: str = "") -> None:
        if self.link is not None:
            self.close_link(self.link, code, reason)

    def _closed(self, link: _Link, code: int | None, reason: str, http: int | None, *, local: bool) -> None:
        link.done = True
        for timer in link.timers:
            self.loop.cancel(timer)
        self.loop.cancel(self.stable_timer)
        self.stable_timer = None
        was_connected = link.authed
        if self.link is link:
            self.link = None
        self.last_close = {"code": code, "reason": reason or meaning(code, http), "http": http, "local": local}
        log.info(
            "connection closed: %s%s",
            meaning(code, http),
            f" ({code})" if code is not None else "",
        )
        if was_connected:
            self.handler.disconnected()
        self.failures += 1
        if self.paused or self.stopped:
            self._set_state("paused" if self.paused else "stopped")
            return
        self._schedule(code if http is None else None)

    def _schedule(self, code: int | None) -> None:
        delay = protocol.retry_delay(code, self.failures)
        delay *= 1 + JITTER * (2 * self.jitter() - 1)
        self.retry_at = self.loop.time() + delay
        self.retry_timer = self.loop.call_later(delay, self.dial)
        self._set_state("rejected" if code in REJECTED else "backoff")

    def pause(self) -> None:
        self.paused = True
        self.loop.cancel(self.retry_timer)
        self.retry_timer = None
        self.retry_at = None
        if self.link is None:
            self._set_state("paused")

    def resume(self) -> None:
        """Dial at once, unless Home Assistant rejected the key, the address or the version: then wait."""
        self.paused = False
        code = (self.last_close or {}).get("code")
        if code not in REJECTED:
            self.dial()
        elif self.retry_at is None and self.link is None:
            self._schedule(code)

    def stop(self) -> None:
        self.stopped = True
        self.loop.cancel(self.retry_timer)
        self.close(GOING_AWAY, "agent stopping")

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "since": self.since,
            "last_close": self.last_close,
            "retry_at": self.retry_at,
            "failures": self.failures,
        }


class Pairing:
    """`torporctl pair`: one pairing connection.

    `outcome` ends as paired, wrong_code, cancelled, timeout, unreachable or failed.
    """

    def __init__(
        self,
        loop: Loop,
        transport: Transport,
        *,
        url: str,
        code: bytes,
        identity: Identity,
        hostname: str,
        os_name: str,
        capabilities: Callable[[], dict[str, Any]],
        tls_verify: bool,
        fingerprint: Callable[[str], None],
        paired: Callable[[bytes], None],
    ) -> None:
        self.loop = loop
        self.transport = transport
        self.url = url
        self.handshake = protocol.AgentHandshake(
            agent_id=identity.agent_id,
            agent=identity.version,
            ephemeral=protocol.Ephemeral.generate(),
            code=code,
            hostname=hostname,
            os=os_name,
        )
        self.capabilities = capabilities
        self.tls_verify = tls_verify
        self.on_fingerprint = fingerprint
        self.on_paired = paired
        self.link = _Link(self.handshake)
        self.outcome: str | None = None
        self.detail = ""
        self.stored = False

    def start(self) -> None:
        self.link.timers.append(self.loop.call_later(protocol.HANDSHAKE_TIMEOUT_S, self._handshake_timeout))
        self.link.conn = self.transport.connect(ws_url(self.url), _Events(self, self.link), tls_verify=self.tls_verify)

    def _handshake_timeout(self) -> None:
        if not self.link.authed:
            self._finish("timeout", "Home Assistant did not finish the handshake within 10 s", Close.TIMEOUT)

    def _finish(self, outcome: str, detail: str, code: int | None = None) -> None:
        if self.link.done:
            return
        if self.outcome is None:
            self.outcome = outcome
            self.detail = detail
        self.link.done = True
        for timer in self.link.timers:
            self.loop.cancel(timer)
        if code is not None and self.link.conn is not None:
            self.link.conn.close(code, detail[:120])

    def _opened(self, link: _Link) -> None:
        assert link.conn is not None
        link.conn.send_text(self.handshake.hello_text())

    def _text(self, link: _Link, data: str) -> None:
        if link.answered:
            self._finish("failed", "text message after the handshake", Close.MALFORMED)
            return
        try:
            auth = self.handshake.answer(data)
        except ProtocolError as error:
            if error.close == Close.AUTH_FAILED:
                self._finish("wrong_code", "the code is wrong, expired or already used", Close.AUTH_FAILED)
            else:
                self._finish("failed", error.reason, error.close)
            return
        link.answered = True
        link.session = self.handshake.session
        assert link.conn is not None
        link.conn.send_text(auth)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.link.conn is not None and self.link.session is not None
        self.link.conn.send_binary(self.link.session.seal(message))

    def _binary(self, link: _Link, data: bytes) -> None:
        if link.session is None:
            self._finish("failed", "binary message before the handshake", Close.MALFORMED)
            return
        try:
            message = protocol.check(link.session.open(data), "ha", pairing=True)
        except ProtocolError as error:
            self._finish("failed", error.reason, error.close)
            return
        kind = message["type"]
        if not link.authed:
            if kind != "auth_ok" or message.get("pairing") is not True:
                self._finish("failed", "expected auth_ok for pairing", Close.MALFORMED)
                return
            link.authed = True
            for timer in link.timers:
                self.loop.cancel(timer)
            link.timers.clear()
            assert self.handshake.fingerprint is not None
            self.on_fingerprint(self.handshake.fingerprint)
            self._send({"type": "capabilities", **self.capabilities()})
            return
        if kind == "ping":
            self._send({"type": "pong", "t": message["t"]})
        elif kind == "paired" and not self.stored:
            assert self.handshake.pairing_key is not None
            self.on_paired(self.handshake.pairing_key)
            self.stored = True
            self.outcome = "paired"
            self._send({"type": "ack", "id": message["id"]})
            self.link.timers.append(
                self.loop.call_later(protocol.PAIRED_ACK_TIMEOUT_S, lambda: self._finish("paired", ""))
            )

    def _closed(self, link: _Link, code: int | None, reason: str, http: int | None, *, local: bool) -> None:
        if self.stored:
            self._finish("paired", "")
            return
        if not link.answered and (code is None or http is not None):
            self._finish("unreachable", meaning(code, http))
        elif code == Close.PAIRING_CANCELLED:
            self._finish("cancelled", reason or "Home Assistant cancelled the pairing")
        elif code == Close.TIMEOUT:
            self._finish("timeout", reason or "the pairing timed out in Home Assistant")
        elif code == Close.AUTH_FAILED:
            self._finish("wrong_code", reason or "the code is wrong, expired or already used")
        else:
            self._finish("failed", reason or meaning(code, http))

    @property
    def finished(self) -> bool:
        return self.link.done
