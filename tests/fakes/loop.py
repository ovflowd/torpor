"""A main loop on a fake clock: timers fire as the clock advances, worker jobs run on the next turn.

Spawned commands go to `spawner`, which answers how long the command takes and what it returns; a
command that takes as long as its timeout is killed then, as the real loop kills it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from torpor.runtime import Result

Spawner = Callable[[list[str]], tuple[float, Result]]


class Timer:
    __slots__ = ("callback", "due", "live", "seq")

    def __init__(self, due: float, seq: int, callback: Callable[[], None]) -> None:
        self.due = due
        self.seq = seq
        self.callback = callback
        self.live = True


class FakeLoop:
    def __init__(self) -> None:
        self.mono = 5000.0
        self.wall = 1_800_000_000.0
        self.timers: list[Timer] = []
        self.seq = 0
        self.worker_jobs = 0
        self.spawner: Spawner | None = None

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def call_later(self, delay: float, callback: Callable[[], None]) -> Timer:
        self.seq += 1
        timer = Timer(self.mono + max(delay, 0.0), self.seq, callback)
        self.timers.append(timer)
        return timer

    def cancel(self, handle: object | None) -> None:
        if isinstance(handle, Timer):
            handle.live = False

    def run_in_worker(self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> None:
        self.worker_jobs += 1

        def job() -> None:
            try:
                result = work()
            except Exception as error:
                done(None, error)
                return
            done(result, None)

        self.call_later(0, job)

    def spawn(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None, done: Callable[[Result], None]
    ) -> None:
        assert self.spawner is not None, f"spawned {list(argv)} with no spawner"
        seconds, result = self.spawner(list(argv))
        if seconds >= timeout:
            seconds, result = timeout, Result(None, f"{os.path.basename(argv[0])} timed out after {timeout:g} s")
        self.call_later(seconds, lambda: done(result))

    def run_due(self) -> None:
        for _ in range(100_000):
            self.timers = [t for t in self.timers if t.live]
            due = [t for t in self.timers if t.due <= self.mono + 1e-9]
            if not due:
                return
            timer = min(due, key=lambda t: (t.due, t.seq))
            timer.live = False
            timer.callback()
        raise AssertionError("the loop did not settle")

    def advance(self, seconds: float, step: float = 1.0) -> None:
        end = self.mono + seconds
        self.run_due()
        while self.mono < end - 1e-9:
            delta = min(step, end - self.mono)
            self.mono += delta
            self.wall += delta
            self.run_due()

    def run_until(self, done: Callable[[], bool], timeout: float) -> bool:
        end = self.mono + timeout
        self.run_due()
        while not done() and self.mono < end - 1e-9:
            self.mono += 0.1
            self.wall += 0.1
            self.run_due()
        return done()
