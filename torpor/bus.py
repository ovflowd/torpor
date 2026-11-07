"""D-Bus as the agent sees it: plain Python values with explicit signatures.

`torpor.glib.GioBus` maps this onto Gio; the tests use a fake. Keeping GLib.Variant out of the
modules lets them, and most tests, run without PyGObject.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

Target = tuple[str, str, str]
Done = Callable[[tuple[Any, ...] | None, "BusError | None"], None]
SignalCallback = Callable[[str, tuple[Any, ...]], None]

PROPERTIES = "org.freedesktop.DBus.Properties"
DENIED = frozenset(
    {
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
        "org.freedesktop.PolicyKit1.Error.NotAuthorized",
    }
)


@dataclass(frozen=True)
class Variant:
    """A value that travels as a D-Bus variant, for `a{sv}` arguments."""

    signature: str
    value: Any


class BusError(Exception):
    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name
        self.message = message

    @property
    def denied(self) -> bool:
        return (
            self.name in DENIED or "not authorized" in self.message.lower() or "access denied" in self.message.lower()
        )


class Bus(Protocol):
    def call(
        self,
        target: Target,
        method: str,
        args: tuple[Any, ...] = (),
        *,
        signature: str = "",
        reply: str | None = None,
        timeout_ms: int = 5000,
    ) -> tuple[Any, ...]: ...

    def call_async(
        self,
        target: Target,
        method: str,
        args: tuple[Any, ...] = (),
        *,
        signature: str = "",
        done: Done,
        timeout_ms: int = 25000,
    ) -> None: ...

    def call_with_fd(
        self, target: Target, method: str, args: tuple[Any, ...], *, signature: str, timeout_ms: int = 5000
    ) -> int: ...

    def get(self, name: str, path: str, iface: str, prop: str, *, timeout_ms: int = 1500) -> Any: ...

    def get_all(self, name: str, path: str, iface: str, *, timeout_ms: int = 1500) -> dict[str, Any]: ...

    def subscribe(
        self, sender: str | None, iface: str, member: str, path: str | None, callback: SignalCallback
    ) -> int: ...

    def watch_name(self, name: str, callback: Callable[[str | None], None]) -> int: ...

    def has_owner(self, name: str) -> bool: ...
