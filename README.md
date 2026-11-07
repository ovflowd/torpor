# torpor

[![CI](https://github.com/ovflowd/torpor/actions/workflows/ci.yml/badge.svg)](https://github.com/ovflowd/torpor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Let Home Assistant see, sleep, wake and dim your Linux PC.

Home Assistant can't tell whether someone is at your PC, and it can't put it to sleep without an SSH
key or a long-lived token that opens your whole house. Torpor is a small agent that runs as you on
the PC and talks to Home Assistant over an encrypted connection, so the PC holds no Home Assistant
token.

This repository has the agent (`torpord`), its command-line tool (`torporctl`) and the protocol.
The Home Assistant side is the [ha-torpor](https://github.com/ovflowd/ha-torpor) integration.

> [!WARNING]
> Torpor lets Home Assistant, and anyone who can use your Home Assistant, lock your PC and put it to
> sleep. Power off, restart and hibernate stay off until you allow them on the PC; read
> [docs/security.md](docs/security.md) first. Nothing has been tested on a real PC yet, so use it at
> your own risk. Torpor isn't affiliated with Home Assistant or the Open Home Foundation.

## What you need

- Linux with systemd, and Python 3.12 or later
- A few packages from your distribution: [docs/install.md](docs/install.md#packages) lists them
- Home Assistant with the [ha-torpor](https://github.com/ovflowd/ha-torpor) integration

## Install

```bash
git clone https://github.com/ovflowd/torpor.git
cd torpor
./torporctl install
```

It installs the agent under your home as a systemd user service, and never runs `sudo`. If a package
is missing, it writes nothing and prints the command that installs it.

Then add the Torpor integration in Home Assistant. It shows a pairing code. On the PC, run this and
type the code when it asks:

```bash
torporctl pair --url http://homeassistant.local:8123
```

If both sides show the same fingerprint, confirm in Home Assistant and pick the features.
[docs/quick-start.md](docs/quick-start.md) walks through it with the output you should see.

## What Home Assistant gets

- The PC's state: `in_use`, `remote`, `busy`, `idle`, `asleep`, `off` or `restarting`
- Lock and Sleep buttons, plus Hibernate, Power off and Restart if you allow them
- A Keep awake switch, for while something runs
- A Wake button, through Wake-on-LAN
- Brightness per display, by hand or from a light sensor

The entities stay while the PC sleeps, so automations and dashboards keep working. ha-torpor's
[entities page](https://github.com/ovflowd/ha-torpor/blob/main/docs/entities.md) lists them all.

## Usage

```bash
torporctl status                          # is it connected, and what state is the PC in
torporctl doctor                          # check everything, and print the fix for what's missing
torporctl config set allow.poweroff yes   # let Home Assistant power the PC off
torporctl hold backup -- borg create ::x  # keep the PC awake while a command runs
```

| Command | What it does |
|---|---|
| `install`, `update SOURCE`, `uninstall` | Installs, updates or removes the agent |
| `pair` | Pairs with Home Assistant |
| `config list\|get\|set\|unset` | Reads or changes `torpor.conf`, and restarts the agent |
| `status` | The connection, the state, displays and leases |
| `logs` | The agent's log |
| `doctor` | Checks everything and prints the commands that fix what's missing |
| `hold NAME`, `release NAME` | Keeps the PC busy for a while, or while a command runs |
| `restart` | Restarts the agent |

[docs/torporctl.md](docs/torporctl.md) has every option and the exit codes, and
[docs/configuration.md](docs/configuration.md) every `torpor.conf` setting.

## Supported systems

| System | Status |
|---|---|
| Ubuntu 24.04 and 26.04, Debian 13, Arch, Fedora | CI-tested: the tests pass on their own packages |
| Other systemd distributions, Fedora Atomic (Kinoite, Bazzite) | Should work, not tested |
| Debian 12, SteamOS, distributions without systemd, Flatpak, Snap | Unsupported |

Nothing has run on a real desktop yet. Plasma, GNOME and other desktops get different parts of it:
[docs/install.md](docs/install.md#desktops) has what works where.

## How it works

The agent dials Home Assistant, so nothing listens on the PC. You pair once with a code Home
Assistant shows, and every message after that is encrypted. `torpor.conf` on the PC says what Home
Assistant may do, and Home Assistant's options choose what it does within that. Before the PC sleeps
or shuts down, the agent tells Home Assistant, so it shows `asleep` or `off` right away.

[docs/how-it-works.md](docs/how-it-works.md) has the details and the modules,
[docs/security.md](docs/security.md) what the agent reads, writes and sends, and
[docs/protocol.md](docs/protocol.md) the wire format. There are guides for
[Wake-on-LAN](docs/wake-on-lan.md), [automations](docs/automations.md) and
[migrating from another agent](docs/migrating.md), and a [FAQ](docs/faq.md).

## Something not working?

`torporctl status` shows the connection and what the last error means, and `torporctl doctor` prints
the fix for what's missing. [docs/troubleshooting.md](docs/troubleshooting.md) covers the usual
problems. To report a bug, see [CONTRIBUTING.md](CONTRIBUTING.md#reporting-a-problem).

## Related projects

- [ha-torpor](https://github.com/ovflowd/ha-torpor), the Home Assistant integration.
- [ddcutil](https://www.ddcutil.com), which Torpor runs for external monitors when Plasma doesn't
  handle their brightness.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The tests run the real agent against fakes, so they need no desktop, Home Assistant or monitor. See
[CONTRIBUTING.md](CONTRIBUTING.md), including how to run the PyGObject tests and the distribution
containers.

## License

[MIT](LICENSE)
