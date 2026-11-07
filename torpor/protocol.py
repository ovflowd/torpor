"""Torpor protocol v1: pairing, the session handshake, encrypted frames and message checks.

The same bytes ship as torpor/protocol.py in ovflowd/torpor and as custom_components/torpor/protocol.py
in ovflowd/ha-torpor; tests/vectors/SHA256SUMS in both repos pins them. Nothing here does I/O.
The specification is docs/protocol.md in ovflowd/torpor.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTO: Final = 1
WS_PATH: Final = "/api/torpor/v1/ws"
APP_ID: Final = uuid.UUID("570fe902-bf3b-439f-b2eb-3dfaf29fb91b")

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 32
PROOF_BYTES: Final = 32
CODE_BYTES: Final = 20
PAIR_ID_BYTES: Final = 8
FINGERPRINT_BYTES: Final = 5
FRAME_VERSION: Final = 1
FRAME_HEADER_BYTES: Final = 9
TAG_BYTES: Final = 16
CODE_PREFIX: Final = "trp1"

MAX_HANDSHAKE_TEXT: Final = 4096
MAX_PLAINTEXT: Final = 65536
MAX_FRAME: Final = FRAME_HEADER_BYTES + MAX_PLAINTEXT + TAG_BYTES
MAX_AGENT_VERSION: Final = 32
MAX_HOSTNAME: Final = 253
MAX_OS: Final = 128

HANDSHAKE_TIMEOUT_S: Final = 10
PAIRING_TIMEOUT_S: Final = 900
PAIRED_ACK_TIMEOUT_S: Final = 10
PING_INTERVAL_S: Final = 30
IDLE_TIMEOUT_S: Final = 90
GOING_DOWN_ACK_S: Final = 2
COMMAND_TIMEOUT_S: Final = 10
STATE_MIN_INTERVAL_S: Final = 1
INPUT_MIN_INTERVAL_S: Final = 2
PAIRING_MAX_FAILURES: Final = 3
STALE_KEY_REPAIR_AFTER: Final = 3
CONNECTS_PER_IP: Final = 10
CONNECTS_WINDOW_S: Final = 60
MAX_PENDING_HANDSHAKES: Final = 16
AGENT_MESSAGES_PER_WINDOW: Final = 50
AGENT_MESSAGES_WINDOW_S: Final = 10

STATES: Final = ("in_use", "remote", "busy", "idle")
HA_STATES: Final = (*STATES, "asleep", "off", "restarting")
SESSIONS: Final = ("unlocked", "locked", "login_screen", "none", "unknown")
GOING_DOWN_KINDS: Final = ("sleep", "poweroff", "reboot", "unknown")
MODULES: Final = ("core", "power", "keep_awake", "wake", "brightness")
COMMANDS: Final = ("lock", "suspend", "hibernate", "poweroff", "reboot", "keep_awake", "brightness_set")
ERRORS: Final = ("not_allowed", "disabled", "unavailable", "refused", "observe_only", "bad_request", "failed", "busy")
EVENTS: Final = ("resumed", "started")
INPUTS: Final = ("lux",)

Sender = Literal["agent", "ha"]

_AGENT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CODE_BODY = re.compile(r"[a-z2-7]{32}")
_CODE_NOISE = re.compile(r"[\s-]")
_NUMBER = (int, float)
_NONE = type(None)


class Close(IntEnum):
    RECONNECT = 4000
    MALFORMED = 4400
    AUTH_FAILED = 4401
    NOT_LOCAL = 4403
    TIMEOUT = 4408
    SUPERSEDED = 4409
    PAIRING_CANCELLED = 4410
    UNSUPPORTED = 4426
    RATE_LIMITED = 4429
    INTERNAL = 4500
    NOT_READY = 4503


_RETRY_AFTER_S: Final[dict[int, float]] = {
    Close.RECONNECT: 1.0,
    Close.AUTH_FAILED: 900.0,
    Close.NOT_LOCAL: 900.0,
    Close.SUPERSEDED: 60.0,
    Close.UNSUPPORTED: 3600.0,
    Close.RATE_LIMITED: 300.0,
    Close.NOT_READY: 60.0,
}


class ProtocolError(Exception):
    """A peer broke the protocol. `close` is the WebSocket close code to send.

    `ban` is true only for a wrong agent proof against a key Home Assistant holds: the one failure
    Home Assistant may count toward its own IP ban (docs/protocol.md, "Failures and bans").
    """

    def __init__(self, close: Close, reason: str, *, ban: bool = False) -> None:
        super().__init__(reason)
        self.close = close
        self.reason = reason
        self.ban = ban


@dataclass(frozen=True, slots=True)
class Ephemeral:
    """Per-connection randomness. Tests pass fixed values; real peers call generate()."""

    nonce: bytes
    private: bytes

    @classmethod
    def generate(cls) -> Ephemeral:
        return cls(secrets.token_bytes(NONCE_BYTES), secrets.token_bytes(KEY_BYTES))


@dataclass(frozen=True, slots=True)
class Hello:
    pairing: bool
    agent_id: str
    agent: str
    nonce: bytes
    epk: bytes
    pair_id: str = ""
    hostname: str = ""
    os: str = ""


@dataclass(frozen=True, slots=True)
class _Keys:
    ha_proof: bytes
    agent_proof: bytes
    agent_to_ha: bytes
    ha_to_agent: bytes
    pairing_key: bytes | None
    fingerprint: str | None


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(value: object, size: int, field: str) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError(Close.MALFORMED, f"{field} must be base64")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ProtocolError(Close.MALFORMED, f"{field} must be base64") from error
    if len(data) != size:
        raise ProtocolError(Close.MALFORMED, f"{field} must be {size} bytes")
    return data


def lp(*parts: bytes | str) -> bytes:
    out = bytearray()
    for part in parts:
        raw = part.encode() if isinstance(part, str) else part
        out += len(raw).to_bytes(4, "big")
        out += raw
    return bytes(out)


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.digest(salt, ikm, "sha256")


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    if not 0 < length <= 255 * 32:
        raise ValueError("HKDF output length out of range")
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.digest(prk, block + info + bytes([counter]), "sha256")
        out += block
        counter += 1
    return out[:length]


def public_key(private: bytes) -> bytes:
    key = X25519PrivateKey.from_private_bytes(private)
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def shared_secret(private: bytes, peer: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(private).exchange(X25519PublicKey.from_public_bytes(peer))
    except ValueError as error:
        raise ProtocolError(Close.MALFORMED, "unusable ephemeral key") from error


def agent_id_from_machine_id(machine_id: str) -> str:
    """The app-specific machine ID, as sd_id128_get_machine_app_specific() derives it, formatted as a UUID."""
    text = machine_id.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", text) or not int(text, 16):
        raise ValueError("a machine ID is 32 hex digits and not all zero")
    digest = bytearray(hmac.digest(bytes.fromhex(text), APP_ID.bytes, "sha256")[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def new_code() -> bytes:
    return secrets.token_bytes(CODE_BYTES)


def format_code(code: bytes) -> str:
    if len(code) != CODE_BYTES:
        raise ValueError(f"a pairing code is {CODE_BYTES} bytes")
    body = base64.b32encode(code).decode("ascii").lower()
    return "-".join([CODE_PREFIX, *(body[index : index + 4] for index in range(0, len(body), 4))])


def parse_code(text: str) -> bytes:
    compact = _CODE_NOISE.sub("", text).lower()
    compact = compact.removeprefix(CODE_PREFIX)
    if not _CODE_BODY.fullmatch(compact):
        raise ValueError("not a Torpor pairing code")
    return base64.b32decode(compact.upper())


def code_key(code: bytes) -> bytes:
    return hkdf_extract(b"torpor/1 pairing code", code)


def pair_id(code: bytes) -> str:
    return b64(hmac.digest(code_key(code), b"torpor/1 pair id", "sha256")[:PAIR_ID_BYTES])


def format_fingerprint(raw: bytes) -> str:
    text = base64.b32encode(raw).decode("ascii")
    return f"{text[:4]}-{text[4:8]}"


def _dump(message: Mapping[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not allowed")


def _finite_float(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError("numbers must be finite")
    return value


def _loads(data: str | bytes) -> object:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def _load_handshake(data: str | bytes, *expected: str) -> dict[str, Any]:
    raw = data.encode() if isinstance(data, str) else data
    if len(raw) > MAX_HANDSHAKE_TEXT:
        raise ProtocolError(Close.MALFORMED, "handshake message too large")
    try:
        message = _loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ProtocolError(Close.MALFORMED, "handshake message is not JSON") from error
    if not isinstance(message, dict) or message.get("type") not in expected:
        raise ProtocolError(Close.MALFORMED, f"expected {' or '.join(expected)}")
    return message


def _text(message: Mapping[str, Any], field: str, limit: int, *, empty: bool = False) -> str:
    value = message.get(field, "")
    if not isinstance(value, str) or len(value) > limit or not value.isprintable() or (not value and not empty):
        raise ProtocolError(Close.MALFORMED, f"bad {field}")
    return value


def _check_proto(message: Mapping[str, Any]) -> None:
    proto = message.get("proto")
    if isinstance(proto, bool) or proto != PROTO:
        raise ProtocolError(Close.UNSUPPORTED, f"torpor proto {PROTO}")


def parse_hello(data: str | bytes) -> Hello:
    """Home Assistant's view of the first message: a hello or a pair_hello."""
    message = _load_handshake(data, "hello", "pair_hello")
    _check_proto(message)
    agent_id = message.get("agent_id")
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ProtocolError(Close.MALFORMED, "bad agent_id")
    pairing = message["type"] == "pair_hello"
    return Hello(
        pairing=pairing,
        agent_id=agent_id,
        agent=_text(message, "agent", MAX_AGENT_VERSION),
        nonce=unb64(message.get("nonce"), NONCE_BYTES, "nonce"),
        epk=unb64(message.get("epk"), KEY_BYTES, "epk"),
        pair_id=b64(unb64(message.get("pair_id"), PAIR_ID_BYTES, "pair_id")) if pairing else "",
        hostname=_text(message, "hostname", MAX_HOSTNAME) if pairing else "",
        os=_text(message, "os", MAX_OS, empty=True) if pairing else "",
    )


def _transcript(hello: Hello, ha_nonce: bytes, ha_epk: bytes) -> bytes:
    if hello.pairing:
        head = lp(b"torpor/1 pairing", str(PROTO), hello.pair_id, hello.agent_id, hello.agent, hello.hostname, hello.os)
    else:
        head = lp(b"torpor/1 session", str(PROTO), hello.agent_id, hello.agent)
    return hashlib.sha256(head + lp(hello.nonce, hello.epk, ha_nonce, ha_epk)).digest()


def _derive(pairing: bool, salt: bytes, dh: bytes, transcript: bytes) -> _Keys:
    prk = hkdf_extract(salt, dh)
    if pairing:
        okm = hkdf_expand(prk, b"torpor/1 pairing keys" + transcript, 5 * KEY_BYTES)
        fingerprint = format_fingerprint(hkdf_expand(prk, b"torpor/1 fingerprint" + transcript, FINGERPRINT_BYTES))
        return _Keys(okm[:32], okm[32:64], okm[64:96], okm[96:128], okm[128:160], fingerprint)
    okm = hkdf_expand(prk, b"torpor/1 session keys" + transcript, 4 * KEY_BYTES)
    return _Keys(okm[:32], okm[32:64], okm[64:96], okm[96:128], None, None)


def _proof(key: bytes, transcript: bytes) -> bytes:
    return hmac.digest(key, transcript, "sha256")


def _nonce(seq: int) -> bytes:
    return bytes(4) + seq.to_bytes(8, "big")


class Session:
    """The encrypted frame layer, one per authenticated connection. Each side holds its own counters."""

    def __init__(self, *, send_key: bytes, receive_key: bytes) -> None:
        self._send = ChaCha20Poly1305(send_key)
        self._receive = ChaCha20Poly1305(receive_key)
        self.sent = 0
        self.received = 0

    def seal(self, message: Mapping[str, Any]) -> bytes:
        plaintext = encode(message)
        header = bytes([FRAME_VERSION]) + self.sent.to_bytes(8, "big")
        frame = header + self._send.encrypt(_nonce(self.sent), plaintext, header)
        self.sent += 1
        return frame

    def open(self, frame: bytes) -> dict[str, Any]:
        if not FRAME_HEADER_BYTES + TAG_BYTES <= len(frame) <= MAX_FRAME:
            raise ProtocolError(Close.MALFORMED, "bad frame size")
        if frame[0] != FRAME_VERSION:
            raise ProtocolError(Close.MALFORMED, "unknown frame version")
        seq = int.from_bytes(frame[1:FRAME_HEADER_BYTES], "big")
        if seq != self.received:
            raise ProtocolError(Close.MALFORMED, "frame out of order")
        try:
            plaintext = self._receive.decrypt(_nonce(seq), frame[FRAME_HEADER_BYTES:], frame[:FRAME_HEADER_BYTES])
        except InvalidTag as error:
            raise ProtocolError(Close.MALFORMED, "frame failed authentication") from error
        self.received += 1
        return decode(plaintext)


class AgentHandshake:
    """The PC's side. Pass `key` for a session, or `code` (the parsed pairing code) to pair."""

    def __init__(
        self,
        *,
        agent_id: str,
        agent: str,
        ephemeral: Ephemeral,
        key: bytes | None = None,
        code: bytes | None = None,
        hostname: str = "",
        os: str = "",
    ) -> None:
        self._private = ephemeral.private
        self._message: dict[str, Any] = {"proto": PROTO, "agent_id": agent_id, "agent": agent}
        if key is not None and code is None:
            if len(key) != KEY_BYTES:
                raise ValueError(f"a pairing key is {KEY_BYTES} bytes")
            self._salt = key
            self._message = {"type": "hello", **self._message}
        elif code is not None and key is None:
            self._salt = code_key(code)
            self._message = {"type": "pair_hello", **self._message, "pair_id": pair_id(code)}
            self._message |= {"hostname": hostname, "os": os}
        else:
            raise ValueError("pass either a key or a pairing code")
        self._message |= {"nonce": b64(ephemeral.nonce), "epk": b64(public_key(ephemeral.private))}
        self.hello = parse_hello(_dump(self._message))
        self.session: Session | None = None
        self.pairing_key: bytes | None = None
        self.fingerprint: str | None = None

    def hello_text(self) -> str:
        return _dump(self._message)

    def answer(self, challenge: str | bytes) -> str:
        """Check Home Assistant's proof and return the auth message. Raises AUTH_FAILED if the proof is wrong."""
        prefix = "pair_" if self.hello.pairing else ""
        message = _load_handshake(challenge, f"{prefix}challenge")
        _check_proto(message)
        nonce = unb64(message.get("nonce"), NONCE_BYTES, "nonce")
        epk = unb64(message.get("epk"), KEY_BYTES, "epk")
        proof = unb64(message.get("proof"), PROOF_BYTES, "proof")
        transcript = _transcript(self.hello, nonce, epk)
        keys = _derive(self.hello.pairing, self._salt, shared_secret(self._private, epk), transcript)
        if not hmac.compare_digest(proof, _proof(keys.ha_proof, transcript)):
            raise ProtocolError(Close.AUTH_FAILED, "home assistant did not prove the key")
        self.session = Session(send_key=keys.agent_to_ha, receive_key=keys.ha_to_agent)
        self.pairing_key = keys.pairing_key
        self.fingerprint = keys.fingerprint
        return _dump({"type": f"{prefix}auth", "proof": b64(_proof(keys.agent_proof, transcript))})


class HomeAssistantHandshake:
    """Home Assistant's side. `secret` is the PC's key, the pending pairing code, or None for a decoy.

    A decoy answers an unknown agent_id or pair_id exactly like a real challenge, so the reply tells
    nobody which PCs are paired, and it never counts toward a ban.
    """

    def __init__(self, hello: Hello, *, ephemeral: Ephemeral, secret: bytes | None) -> None:
        self.hello = hello
        self.decoy = secret is None
        if secret is None:
            secret = hmac.digest(ephemeral.private, b"torpor/1 decoy", "sha256")
        if not hello.pairing and len(secret) != KEY_BYTES:
            raise ValueError(f"a pairing key is {KEY_BYTES} bytes")
        salt = code_key(secret) if hello.pairing else secret
        self._nonce = ephemeral.nonce
        self._epk = public_key(ephemeral.private)
        self._transcript = _transcript(hello, self._nonce, self._epk)
        self._keys = _derive(hello.pairing, salt, shared_secret(ephemeral.private, hello.epk), self._transcript)
        self.pairing_key: bytes | None = None
        self.fingerprint: str | None = None

    def challenge(self) -> str:
        return _dump(
            {
                "type": "pair_challenge" if self.hello.pairing else "challenge",
                "proto": PROTO,
                "nonce": b64(self._nonce),
                "epk": b64(self._epk),
                "proof": b64(_proof(self._keys.ha_proof, self._transcript)),
            }
        )

    def verify(self, auth: str | bytes) -> Session:
        """Check the PC's proof. Raises AUTH_FAILED; `ban` is set only for a wrong proof against a real key."""
        message = _load_handshake(auth, "pair_auth" if self.hello.pairing else "auth")
        proof = unb64(message.get("proof"), PROOF_BYTES, "proof")
        good = hmac.compare_digest(proof, _proof(self._keys.agent_proof, self._transcript))
        if self.decoy or not good:
            ban = not self.decoy and not self.hello.pairing
            raise ProtocolError(Close.AUTH_FAILED, "authentication failed", ban=ban)
        self.pairing_key = self._keys.pairing_key
        self.fingerprint = self._keys.fingerprint
        return Session(send_key=self._keys.ha_to_agent, receive_key=self._keys.agent_to_ha)


def encode(message: Mapping[str, Any]) -> bytes:
    kind = message.get("type")
    if not isinstance(kind, str) or not kind:
        raise ValueError("a message needs a type")
    data = _dump(message).encode()
    if len(data) > MAX_PLAINTEXT:
        raise ValueError("message too large")
    return data


def decode(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_PLAINTEXT:
        raise ProtocolError(Close.MALFORMED, "message too large")
    try:
        message = _loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ProtocolError(Close.MALFORMED, "message is not JSON") from error
    if not isinstance(message, dict) or not isinstance(message.get("type"), str) or not message["type"]:
        raise ProtocolError(Close.MALFORMED, "message needs a type")
    return message


_AGENT: Final = frozenset({"agent"})
_HA: Final = frozenset({"ha"})
_BOTH: Final = frozenset({"agent", "ha"})

_SCHEMA: Final[dict[str, tuple[frozenset[str], dict[str, tuple[type, ...]]]]] = {
    "auth_ok": (_HA, {"pairing": (bool,), "ha_version": (str,), "integration": (str,)}),
    "capabilities": (_AGENT, {"observe_only": (bool,), "host": (dict,), "modules": (dict,)}),
    "state": (_AGENT, {"state": (str,)}),
    "event": (_AGENT, {"name": (str,)}),
    "going_down": (_AGENT, {"id": (str,), "kind": (str,)}),
    "result": (_AGENT, {"id": (str,), "ok": (bool,)}),
    "config": (_HA, {"modules": (dict,)}),
    "input": (_HA, {"name": (str,), "value": (*_NUMBER, _NONE), "age": _NUMBER}),
    "command": (_HA, {"id": (str,), "name": (str,), "args": (dict,)}),
    "paired": (_HA, {"id": (str,)}),
    "ack": (_BOTH, {"id": (str,)}),
    "ping": (_BOTH, {"t": _NUMBER}),
    "pong": (_BOTH, {"t": _NUMBER}),
}

PAIRING_TYPES: Final = frozenset({"auth_ok", "capabilities", "paired", "ack", "ping", "pong"})


def check(message: dict[str, Any], sender: Sender, *, pairing: bool = False) -> dict[str, Any]:
    """Validate the fields every receiver relies on. Unknown types pass through so a newer peer can add them."""
    kind = message["type"]
    spec = _SCHEMA.get(kind)
    if spec is None:
        return message
    senders, fields = spec
    if sender not in senders or (pairing and kind not in PAIRING_TYPES):
        raise ProtocolError(Close.MALFORMED, f"unexpected {kind}")
    for name, kinds in fields.items():
        value = message.get(name)
        if (isinstance(value, bool) and bool not in kinds) or not isinstance(value, kinds):
            raise ProtocolError(Close.MALFORMED, f"bad {kind}.{name}")
    if kind == "result" and not message["ok"]:
        error = message.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise ProtocolError(Close.MALFORMED, "bad result.error")
    return message


def retry_delay(close: int | None, failures: int) -> float:
    """Seconds the agent waits before it dials again, before jitter. `failures` counts from 1."""
    if close is not None and close in _RETRY_AFTER_S:
        return _RETRY_AFTER_S[close]
    return float(min(2 ** max(failures, 1), 60))
