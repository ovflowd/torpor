"""Leases and busy processes: until, pid, TTL, both, names, full command lines (A7)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import Env
from fakes.tree import Tree
from torpor import leases


def test_an_until_lease_lives_until_its_time(tmp_path: Path, tree: Tree) -> None:
    leases.write(tmp_path, "render", until=1000)
    assert [lease.name for lease in leases.scan(tmp_path, now=999, ttl=300, proc=tree.proc)] == ["render"]
    assert leases.scan(tmp_path, now=1000, ttl=300, proc=tree.proc) == []


def test_a_pid_lease_lives_while_its_process_does(tmp_path: Path, tree: Tree) -> None:
    tree.process(321, "/usr/bin/sleep", "60")
    leases.write(tmp_path, "job", pid=321)
    assert len(leases.scan(tmp_path, now=time.time() + 10**6, ttl=1, proc=tree.proc)) == 1
    tree.kill(321)
    assert leases.scan(tmp_path, now=time.time(), ttl=300, proc=tree.proc) == []


def test_an_empty_lease_lives_for_the_ttl_after_its_last_touch(tmp_path: Path, tree: Tree) -> None:
    path = tmp_path / "hook.lease"
    path.write_text("")
    os.utime(path, (1000, 1000))
    assert len(leases.scan(tmp_path, now=1300, ttl=300, proc=tree.proc)) == 1
    assert leases.scan(tmp_path, now=1301, ttl=300, proc=tree.proc) == []
    os.utime(path, (1301, 1301))
    assert len(leases.scan(tmp_path, now=1301, ttl=300, proc=tree.proc)) == 1


def test_with_until_and_pid_both_must_hold(tmp_path: Path, tree: Tree) -> None:
    tree.process(321, "make")
    leases.write(tmp_path, "both", until=2000, pid=321)
    assert len(leases.scan(tmp_path, now=1000, ttl=1, proc=tree.proc)) == 1
    assert leases.scan(tmp_path, now=2000, ttl=1, proc=tree.proc) == []
    tree.kill(321)
    assert leases.scan(tmp_path, now=1000, ttl=1, proc=tree.proc) == []


def test_unknown_lines_are_ignored_and_bad_numbers_are_skipped(tmp_path: Path, tree: Tree) -> None:
    (tmp_path / "odd.lease").write_text("owner=me\nuntil=soon\nuntil=5000\n")
    found = leases.scan(tmp_path, now=1000, ttl=1, proc=tree.proc)
    assert [(lease.name, lease.until) for lease in found] == [("odd", 5000.0)]


@pytest.mark.parametrize("name", ["", "..", "a/b", "x" * 65, "space name", "ümlaut"])
def test_bad_names_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(leases.LeaseError):
        leases.write(tmp_path, name)


def test_files_with_bad_names_are_not_leases(tmp_path: Path, tree: Tree) -> None:
    (tmp_path / "bad name.lease").write_text("")
    assert leases.scan(tmp_path, now=time.time(), ttl=300, proc=tree.proc) == []


def test_processes_match_the_full_command_line_of_any_user(tree: Tree) -> None:
    tree.process(10, "/usr/bin/python3", "-m", "http.server")
    tree.process(11, "rsync", "-a", "/home", "backup:/srv")
    found = leases.matching_processes(
        (r"rsync .*backup:", r"^/usr/bin/python3 -m http\.server$", "ffmpeg"), tree.proc, own_pid=1
    )
    assert found == [r"rsync .*backup:", r"^/usr/bin/python3 -m http\.server$"]


def test_the_agent_never_matches_itself(tree: Tree) -> None:
    tree.process(4242, "python3", "-m", "torpor", "agent", "rsync")
    assert leases.matching_processes(("rsync",), tree.proc, own_pid=4242) == []


def test_live_leases_and_processes_make_the_pc_busy(env: Env) -> None:
    env.pair()
    env.set("busy.processes", "borg create")
    env.world.greeter()
    env.start()
    env.advance(2)
    assert env.last_state()["state"] == "idle"
    leases.write(env.paths.leases_dir, "build", until=env.loop.time() + 60)
    env.tree.process(77, "borg", "create", "::archive")
    env.advance(10)
    assert env.last_state()["busy"] == ["lease:build", "process:borg create"]
    assert env.last_state()["state"] == "busy"
    env.advance(60)
    env.tree.kill(77)
    env.advance(10)
    assert env.last_state()["state"] == "idle"
