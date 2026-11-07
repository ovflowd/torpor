"""Where Torpor keeps its files, following the XDG base directories, and how it writes them."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

UNIT = "torpord.service"
HARDENING = "50-hardening.conf"


@dataclass(frozen=True)
class Paths:
    home: Path
    config_home: Path
    state_home: Path
    runtime: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Paths:
        env = os.environ if env is None else env
        home = Path(env.get("HOME") or Path.home())
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return cls(
            home=home,
            config_home=Path(env.get("XDG_CONFIG_HOME") or home / ".config"),
            state_home=Path(env.get("XDG_STATE_HOME") or home / ".local" / "state"),
            runtime=Path(runtime),
        )

    @property
    def config_dir(self) -> Path:
        return self.config_home / "torpor"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "torpor.conf"

    @property
    def key_file(self) -> Path:
        return self.config_dir / "key"

    @property
    def state_dir(self) -> Path:
        return self.state_home / "torpor"

    @property
    def known_file(self) -> Path:
        return self.state_dir / "known.json"

    @property
    def no_hardening_file(self) -> Path:
        return self.state_dir / "no-hardening"

    @property
    def runtime_dir(self) -> Path:
        return self.runtime / "torpor"

    @property
    def status_file(self) -> Path:
        return self.runtime_dir / "status.json"

    @property
    def boot_state_file(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def leases_dir(self) -> Path:
        return self.runtime_dir / "leases"

    @property
    def lib_dir(self) -> Path:
        return self.home / ".local" / "lib" / "torpor"

    @property
    def zipapp(self) -> Path:
        return self.lib_dir / "torpor.pyz"

    @property
    def launcher(self) -> Path:
        return self.home / ".local" / "bin" / "torporctl"

    @property
    def unit_dir(self) -> Path:
        return self.config_home / "systemd" / "user"

    @property
    def unit_file(self) -> Path:
        return self.unit_dir / UNIT

    @property
    def dropin_dir(self) -> Path:
        return self.unit_dir / f"{UNIT}.d"

    @property
    def hardening_file(self) -> Path:
        return self.dropin_dir / HARDENING


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    path.chmod(mode)


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Write through a temporary file in the same folder, fsync, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode() if isinstance(data, str) else data
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def boot_id(proc: Path) -> str | None:
    try:
        return (proc / "sys" / "kernel" / "random" / "boot_id").read_text().strip() or None
    except OSError:
        return None
