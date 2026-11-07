"""Protocol constants and close-code meanings that torporctl needs before `cryptography` is installed.

They repeat values from protocol.py, which imports cryptography, so that `torporctl install` and
`doctor` can say what's missing instead of failing on an import. tests/test_codes.py keeps them equal.
"""

from __future__ import annotations

from typing import Final

PROTO: Final = 1
WS_PATH: Final = "/api/torpor/v1/ws"
PAIRING_TIMEOUT_S: Final = 900
MAX_HOSTNAME: Final = 253
MAX_OS: Final = 128
KEY_BYTES: Final = 32

RECONNECT: Final = 4000
MALFORMED: Final = 4400
AUTH_FAILED: Final = 4401
NOT_LOCAL: Final = 4403
TIMEOUT: Final = 4408
SUPERSEDED: Final = 4409
PAIRING_CANCELLED: Final = 4410
UNSUPPORTED: Final = 4426
RATE_LIMITED: Final = 4429
INTERNAL: Final = 4500
NOT_READY: Final = 4503

MEANINGS: Final[dict[int, str]] = {
    1000: "closed normally",
    1001: "going away",
    1006: "connection lost",
    RECONNECT: "Home Assistant asked for a reconnect",
    MALFORMED: "a peer broke the protocol",
    AUTH_FAILED: "the key was refused: re-pair needed",
    NOT_LOCAL: "Home Assistant refused a connection from outside its local network",
    TIMEOUT: "timed out",
    SUPERSEDED: "another connection for this PC took over",
    PAIRING_CANCELLED: "pairing was cancelled",
    UNSUPPORTED: "Home Assistant speaks another protocol version",
    RATE_LIMITED: "too many messages",
    INTERNAL: "an internal error",
    NOT_READY: "the Torpor entry isn't loaded in Home Assistant",
}


def meaning(code: int | None, http: int | None = None) -> str:
    if http == 404:
        return "no Torpor endpoint at this URL: is the integration installed and Home Assistant running?"
    if http is not None:
        return f"the WebSocket upgrade failed with HTTP {http} (a proxy in the way?)"
    if code is None:
        return "could not connect"
    return MEANINGS.get(code, f"close code {code}")
