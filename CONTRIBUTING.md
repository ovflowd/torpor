# Contributing

Thanks for helping. Torpor locks, sleeps and powers off people's PCs, so a change should be small
and easy to review: one change per pull request, and a behavior change comes with a test.

## Setup

```sh
git clone https://github.com/ovflowd/torpor.git
cd torpor
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The tests need no desktop, no Home Assistant and no monitor. They run the real agent against a
fake clock and main loop, fake system and session buses that check every D-Bus signature, a fake
Home Assistant that speaks the real protocol through `protocol.HomeAssistantHandshake`, fake
`ddcutil`, `systemctl`, `systemd-run`, `loginctl`, `journalctl` and `nmcli` scripts chosen through
`TORPOR_*` environment variables, and synthetic `/sys`, `/proc` and `/etc` trees. Nothing touches
your real buses, units or monitors.

In a plain `uv` venv the tests that need PyGObject skip. To run them, use your distro's Python,
which has PyGObject from its packages. Name it with `--python`, or uv may pick a Python it manages,
which has no PyGObject:

```sh
uv venv --python /usr/bin/python3 --system-site-packages .venv-gi
VIRTUAL_ENV=.venv-gi uv pip install cryptography pytest
.venv-gi/bin/python -m pytest
```

They start a private `dbus-daemon` (never your session or system bus), a WebSocket server on
127.0.0.1, and `torpord --self-test`, also under the kernel's MemoryDenyWriteExecute.

CI also runs the whole suite in Ubuntu 24.04, Debian 13, Ubuntu 26.04, Arch and Fedora containers
on each distro's own packages. To do the same locally:

```sh
docker run --rm -v "$PWD:/src:ro" debian:13 sh /src/scripts/distro-test.sh
```

## Style

- Ruff formats and lints, mypy checks types strictly, with the settings in `pyproject.toml`. CI
  checks all three.
- A comment says why, not what. Do not restate the code.
- Modules that don't touch D-Bus or the network (`config`, `model`, `leases`, `known`, `curve`,
  `status`) import without PyGObject. Keep it that way: only `torpor/glib.py` imports `gi`.
- The agent needs nothing beyond the standard library, PyGObject, libsoup 3 and `cryptography`,
  all from distro packages.
- Commit messages are an imperative sentence with a type, as in `fix: keep the key folder private`,
  and the body says why.

## Changing the protocol

`torpor/protocol.py` is shared, byte for byte, with
[ovflowd/ha-torpor](https://github.com/ovflowd/ha-torpor). To change it:

1. Edit `torpor/protocol.py` and `docs/protocol.md`.
2. Run `uv run python scripts/make-vectors.py` and `uv run pytest`.
3. Copy `torpor/protocol.py` to `custom_components/torpor/protocol.py`, and `tests/test_protocol.py`
   and `tests/vectors/` to the same paths in ha-torpor, unchanged, in a pull request there.

A change that needs new vectors for old inputs is a version 2 change.

## AI assistants

If you used an AI assistant for a change, say so in the pull request. You remain responsible for
it, and for having read it.

## License of contributions

By contributing, you agree that your work is licensed under the [MIT license](LICENSE). This
project follows a [code of conduct](CODE_OF_CONDUCT.md).

## Reporting a problem

Questions and ideas are welcome as issues too. For a bug, include `torporctl --version`, your
distribution and desktop, and `torporctl doctor --json`. Never paste your pairing code or
`~/.config/torpor/key`. For a security problem, see [SECURITY.md](SECURITY.md).
