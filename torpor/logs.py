"""Logging to the journal through stdout, with failures that a 10 s tick would repeat logged once."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("torpor")


def setup(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger("torpor")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if level == "debug" else logging.INFO)
    root.propagate = False


class Failures:
    """Log a failure once until its message changes or it clears."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def fail(self, key: str, message: str) -> None:
        if self._last.get(key) != message:
            self._last[key] = message
            log.warning(message)

    def clear(self, key: str, message: str | None = None) -> None:
        if self._last.pop(key, None) is not None and message:
            log.info(message)

    def active(self, key: str) -> bool:
        return key in self._last
