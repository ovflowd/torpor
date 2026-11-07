"""Leases: files that keep the PC busy, written by `torporctl hold` or by any hook with echo or touch."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from torpor.paths import atomic_write, ensure_dir

NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
SUFFIX = ".lease"


class LeaseError(ValueError):
    pass


@dataclass(frozen=True)
class Lease:
    name: str
    until: float | None
    pid: int | None
    mtime: float


def check_name(name: str) -> str:
    if not NAME.fullmatch(name) or name in (".", ".."):
        raise LeaseError(f"{name!r} is not a lease name: 1 to 64 of A-Z a-z 0-9 . _ -")
    return name


def read(path: Path) -> Lease | None:
    try:
        text = path.read_text(errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    until: float | None = None
    pid: int | None = None
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        try:
            if key == "until":
                until = float(value)
            elif key == "pid":
                pid = int(value)
        except ValueError:
            continue
    return Lease(path.name.removesuffix(SUFFIX), until, pid, mtime)


def pid_alive(pid: int, proc: Path) -> bool:
    return pid > 0 and (proc / str(pid)).is_dir()


def live(lease: Lease, *, now: float, ttl: float, proc: Path) -> bool:
    if lease.until is None and lease.pid is None:
        return now - lease.mtime <= ttl
    if lease.until is not None and now >= lease.until:
        return False
    return lease.pid is None or pid_alive(lease.pid, proc)


def scan(folder: Path, *, now: float, ttl: float, proc: Path) -> list[Lease]:
    try:
        paths = sorted(folder.glob(f"*{SUFFIX}"))
    except OSError:
        return []
    found: list[Lease] = []
    for path in paths:
        if not NAME.fullmatch(path.name.removesuffix(SUFFIX)):
            continue
        lease = read(path)
        if lease is not None and live(lease, now=now, ttl=ttl, proc=proc):
            found.append(lease)
    return found


def write(folder: Path, name: str, *, until: float | None = None, pid: int | None = None) -> Path:
    check_name(name)
    ensure_dir(folder)
    lines = []
    if until is not None:
        lines.append(f"until={int(until)}")
    if pid is not None:
        lines.append(f"pid={pid}")
    path = folder / f"{name}{SUFFIX}"
    atomic_write(path, "".join(f"{line}\n" for line in lines))
    return path


def remove(folder: Path, name: str) -> bool:
    check_name(name)
    try:
        (folder / f"{name}{SUFFIX}").unlink()
    except FileNotFoundError:
        return False
    return True


def command_lines(proc: Path) -> list[tuple[int, str]]:
    """Every process's full command line, NUL bytes as spaces, any user's."""
    rows: list[tuple[int, str]] = []
    try:
        entries = os.scandir(proc)
    except OSError:
        return rows
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = (Path(entry.path) / "cmdline").read_bytes()
            except OSError:
                continue
            if raw:
                rows.append((int(entry.name), raw.rstrip(b"\0").replace(b"\0", b" ").decode(errors="replace")))
    return rows


def matching_processes(patterns: tuple[str, ...], proc: Path, *, own_pid: int) -> list[str]:
    if not patterns:
        return []
    compiled = [(pattern, re.compile(pattern)) for pattern in patterns]
    lines = [line for pid, line in command_lines(proc) if pid != own_pid]
    return [pattern for pattern, regex in compiled if any(regex.search(line) for line in lines)]
