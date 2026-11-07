"""The main loop, WebSocket and subprocess seams. `torpor.glib` implements them; the tests fake them."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from torpor.bus import Bus

T = TypeVar("T")


class Loop(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    def call_later(self, delay: float, callback: Callable[[], None]) -> object: ...

    def cancel(self, handle: object | None) -> None: ...

    def run_in_worker(self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> None: ...

    def spawn(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None, done: Callable[[Result], None]
    ) -> None:
        """Run a command without blocking, killed after `timeout`; `done` gets its result on the main loop."""
        ...

    def run_until(self, done: Callable[[], bool], timeout: float) -> bool: ...


class TransportEvents(Protocol):
    def opened(self) -> None: ...

    def text(self, data: str) -> None: ...

    def binary(self, data: bytes) -> None: ...

    def closed(self, code: int | None, reason: str, http_status: int | None) -> None: ...


class Connection(Protocol):
    def send_text(self, text: str) -> None: ...

    def send_binary(self, data: bytes) -> None: ...

    def close(self, code: int, reason: str = "") -> None: ...


class Transport(Protocol):
    def connect(self, url: str, events: TransportEvents, *, tls_verify: bool) -> Connection: ...


@dataclass
class Runtime:
    loop: Loop
    system: Bus | None
    session: Bus | None
    transport: Transport
    sysfs: Path
    proc: Path
    env: Mapping[str, str]
    uid: int
    pid: int


def roots(env: Mapping[str, str]) -> tuple[Path, Path]:
    return Path(env.get("TORPOR_SYSFS") or "/sys"), Path(env.get("TORPOR_PROC") or "/proc")


def tool(env: Mapping[str, str], variable: str, name: str) -> str:
    """An external tool, or the fake a test names in TORPOR_<NAME>."""
    return env.get(variable) or shutil.which(name) or name


@dataclass(frozen=True)
class Result:
    code: int | None
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(argv: Sequence[str], *, timeout: float = 15, env: Mapping[str, str] | None = None) -> Result:
    """Run a command and capture stdout and stderr together. `code` is None on a timeout or a missing binary."""
    try:
        done = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired:
        return Result(None, f"{os.path.basename(argv[0])} timed out after {timeout:g} s")
    except OSError as error:
        return Result(None, f"cannot run {argv[0]}: {error.strerror}")
    return Result(done.returncode, done.stdout)
