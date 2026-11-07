"""The pairing key on disk: base64 and a newline, mode 0600, in a 0700 folder."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from torpor.codes import KEY_BYTES
from torpor.paths import atomic_write, ensure_dir


def read_key(path: Path) -> bytes | None:
    try:
        key = base64.b64decode(path.read_text().strip(), validate=True)
    except (OSError, ValueError, binascii.Error):
        return None
    return key if len(key) == KEY_BYTES else None


def write_key(path: Path, key: bytes) -> None:
    ensure_dir(path.parent, 0o700)
    atomic_write(path, base64.b64encode(key).decode() + "\n", 0o600)
