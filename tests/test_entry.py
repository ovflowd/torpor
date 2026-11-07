"""The entry points: `python -m torpor agent|ctl`, torpord's options, and --self-test without PyGObject."""

from __future__ import annotations

import pytest

from conftest import has_gi
from torpor import __main__ as entry
from torpor import __version__, agent


def test_the_package_entry_dispatches_agent_and_ctl(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as done:
        entry.main(["agent", "--version"])
    assert done.value.code == 0
    with pytest.raises(SystemExit):
        entry.main(["ctl", "--version"])
    out = capsys.readouterr().out
    assert out == f"torpord {__version__} (proto 1)\ntorporctl {__version__} (proto 1)\n"


def test_the_package_entry_needs_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main([]) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.skipif(has_gi(), reason="checks the failure without PyGObject")
def test_self_test_fails_cleanly_without_pygobject(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent.main(["--self-test"]) == 1
    assert capsys.readouterr().out.startswith("self-test failed: ")


def test_torpord_options() -> None:
    args = agent.parse_args(["--observe-only", "--config", "/x/torpor.conf"])
    assert args.observe_only is True and str(args.config) == "/x/torpor.conf" and args.self_test is False
