# Install

Torpor's agent is Python on your distribution's own interpreter, PyGObject, the libsoup 3 typelib
and `cryptography`, all from distribution packages. `torporctl install` puts it under your home and
runs it as a systemd user service. It never runs `sudo`; where something needs root, it prints the
command for you.

## Packages

| Need | Arch | Debian 13, Ubuntu 24.04 and 26.04 | Fedora |
|---|---|---|---|
| Required | `python python-gobject libsoup3 python-cryptography` | `python3 python3-gi gir1.2-glib-2.0 gir1.2-soup-3.0 python3-cryptography` | `python3 python3-gobject-base libsoup3 python3-cryptography` |
| Brightness of external monitors without Plasma | `ddcutil` | `ddcutil` | `ddcutil` |
| Wake-on-LAN status | `networkmanager` (optional) | `network-manager` (optional) | `NetworkManager` (optional) |
| Running the tests | `python-pytest` | `python3-pytest` | `python3-pytest` |

These names were checked in each distribution's official container, and CI installs exactly these
packages before it runs the test suite.

### Arch, CachyOS, EndeavourOS, Manjaro

```bash
sudo pacman -S --needed python python-gobject libsoup3 python-cryptography
```

### Debian 13, Ubuntu 24.04 and 26.04

```bash
sudo apt install python3 python3-gi gir1.2-glib-2.0 gir1.2-soup-3.0 python3-cryptography
```

Debian 12 is not supported: its Python is 3.11.

### Fedora

```bash
sudo dnf install python3 python3-gobject-base libsoup3 python3-cryptography
```

### Fedora Atomic (Kinoite, Silverblue, Bazzite)

Layer the same packages, then reboot:

```bash
sudo rpm-ostree install python3-gobject-base libsoup3 python3-cryptography
```

This should work but is not tested.

### Other distributions

You need systemd, Python 3.12 or later, PyGObject with the GLib, Gio and Soup 3.0 typelibs, and
`cryptography` 41 or later. `torporctl doctor` tells you what's missing. SteamOS (read-only root),
distributions without systemd, Flatpak and Snap are not supported.

## Install

From a checkout or an unpacked release:

```bash
./torporctl install
```

| Option | |
|---|---|
| `--observe-only` | Sets `agent.observe-only = yes`: the agent reports but executes nothing ([below](#observe-only)) |
| `--no-start` | Writes everything but neither enables nor starts the service |
| `--no-hardening` | Skips the hardening probe and removes the drop-in |

What `install` does, in order:

1. Runs `doctor`'s required checks against the interpreter it will use: Python 3.12 or later,
   PyGObject, Gio, Soup 3, a `cryptography` round trip, the systemd user manager, logind, and
   `torpor.conf` if there is one. If one fails, it writes nothing, prints the install line for your
   distribution and exits 4.
2. Builds `~/.local/lib/torpor/torpor.pyz`, a zipapp of the agent and `torporctl`, from the checkout
   it runs from.
3. Writes `~/.local/bin/torporctl`, which runs the zipapp with the same interpreter.
4. Writes `~/.config/systemd/user/torpord.service`, with the absolute path of the interpreter that
   passed the checks.
5. Writes the default `~/.config/torpor/torpor.conf` if there is none. An existing one is kept.
6. Probes the hardening drop-in and writes it if the probe passes ([below](#hardening)).
7. Runs `systemctl --user daemon-reload`, `enable` and `restart`.
8. Reports linger, and at a terminal offers to turn it on.

### Files

| Path | What | Mode |
|---|---|---|
| `~/.local/lib/torpor/torpor.pyz` | The agent and `torporctl` | 0644 |
| `~/.local/bin/torporctl` | Launcher | 0755 |
| `~/.config/systemd/user/torpord.service` | The unit | 0644 |
| `~/.config/systemd/user/torpord.service.d/50-hardening.conf` | Hardening, only after the probe passes | 0644 |
| `~/.config/torpor/` | Config folder | 0700 |
| `~/.config/torpor/torpor.conf` | [Configuration](configuration.md) | 0600 |
| `~/.config/torpor/key` | The pairing key | 0600 |
| `~/.local/state/torpor/known.json` | Backends that worked and displays seen in the last 30 days | 0600 |
| `$XDG_RUNTIME_DIR/torpor/status.json` | What `torporctl status` reads | 0600 |
| `$XDG_RUNTIME_DIR/torpor/state.json` | Keep-awake end and brightness overrides, until the next boot | 0600 |
| `$XDG_RUNTIME_DIR/torpor/leases/` | [Leases](modules/keep-awake.md#leases) | 0700 |

`XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` are honoured.

## Linger

A systemd user service runs while you are logged in. With linger on, your user manager starts at
boot and keeps running after you log out, and so does the agent. Without it, Home Assistant loses
the PC at the login screen. Turn it on for yourself; polkit allows that without root:

```bash
loginctl enable-linger
```

## Hardening

`50-hardening.conf` locks the service down: a read-only system and home (except Torpor's own
folders), no new privileges, no writable and executable memory, a system-call filter, and only
UNIX, IP and netlink sockets. It leaves `/dev` alone, because `ddcutil` needs `/dev/i2c-*`.

In a user unit some of these settings need user namespaces, which some systems restrict. So
`install` first runs `torpord --self-test` under the same settings with `systemd-run --user`, and
writes the drop-in only if that passes. If it doesn't, the agent runs without it and `doctor` warns.
`install` probes again each time you run it.

The drop-in's `ReadWritePaths` follow `XDG_CONFIG_HOME` and `XDG_STATE_HOME` as they are set when
you run `install`. If you move those folders later, run `install` again.

## Observe-only

With `agent.observe-only = yes` (or `torporctl install --observe-only`), the agent
connects, reports its capabilities, state and events, and sends the going-down goodbye, but never
locks, sleeps, powers off, holds an idle inhibit or writes a brightness. Every command gets
`observe_only`. It still computes the brightness it would set. Use it to run Torpor beside another
agent while you compare them ([migrating](migrating.md)).

## Update

```bash
git -C torpor pull
torporctl update ./torpor
```

`SOURCE` is a checkout folder or a release `.tar.gz`. `update` refuses an older version unless you
pass `--force`, then runs the new version's own `install`, which keeps your config, key and
observe-only setting, and restarts the agent. It keeps `--no-hardening` if you used it, and
otherwise probes for the hardening drop-in again.

## Uninstall

```bash
torporctl uninstall          # keeps ~/.config/torpor and ~/.local/state/torpor
torporctl uninstall --purge  # removes them too, the key included
```

`uninstall` stops and disables the service and removes the unit, its drop-in, the zipapp, the
launcher and the runtime folder. It leaves linger as it is; `loginctl disable-linger` turns it off.
Delete the PC's entry in Home Assistant too.

It also leaves what you installed as root. The polkit rule keeps granting the actions it lists
with nobody logged in until you remove it:

```bash
sudo rm /etc/polkit-1/rules.d/50-torpor.rules
sudo rm /etc/modules-load.d/i2c-dev.conf    # only if nothing else needs i2c-dev at boot
```

## Desktops

| Module | Plasma 6.2+ | Plasma 5 | GNOME | Others (wlroots, …) |
|---|---|---|---|---|
| `core` | logind, lock screen, DRM | same | same | same; the lock state depends on the locker |
| `power` | logind | logind | logind | logind |
| `keep_awake` | `kde` | `kde` (should work) | `gnome` (should work) | `portal` if the portal backend has Inhibit, else unavailable |
| `brightness`, external monitors | `kde` | `ddcutil` | `ddcutil` | `ddcutil` |
| `brightness`, laptop panel | `kde` | `logind` | `logind` (should work) | `logind` |

Ubuntu 24.04 ships Plasma 5.27, which has no `org.kde.ScreenBrightness`, so brightness goes through
`ddcutil` and `logind` there. Plasma's brightness service is detected while the agent runs; the
first Plasma version that has it is 6.2, as far as we know.
