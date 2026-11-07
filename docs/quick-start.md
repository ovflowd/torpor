# Quick start

From the packages to a PC Home Assistant can see, sleep and wake, in about ten minutes. Every host
name, code, MAC and fingerprint on this page is synthetic.

## 1. Packages

Install Python, PyGObject, the libsoup 3 typelib and `cryptography` from your distribution. Torpor
uses the distribution's own Python because PyGObject comes from its packages.

```bash
sudo pacman -S --needed python python-gobject libsoup3 python-cryptography             # Arch, CachyOS, EndeavourOS
sudo apt install python3 python3-gi gir1.2-glib-2.0 gir1.2-soup-3.0 python3-cryptography # Debian 13, Ubuntu 24.04 and 26.04
sudo dnf install python3 python3-gobject-base libsoup3 python3-cryptography              # Fedora
```

For external monitors without KDE Plasma, also install `ddcutil` ([brightness](modules/brightness.md)).
[install.md](install.md) has the rest.

## 2. Install the agent

```bash
git clone https://github.com/ovflowd/torpor.git
cd torpor
./torporctl install
```

```text
Installed ~/.local/lib/torpor/torpor.pyz, ~/.local/bin/torporctl and ~/.config/systemd/user/torpord.service
Wrote the default ~/.config/torpor/torpor.conf
Hardening drop-in written: ~/.config/systemd/user/torpord.service.d/50-hardening.conf
Started torpord.service
Linger is off: torpord stops when you log out, and Home Assistant loses the PC.
Turn linger on now with `loginctl enable-linger`? [y/N] y
Linger is on.
Next: add the Torpor integration in Home Assistant, then run torporctl pair --url <its URL>
```

`install` checks the packages first and writes nothing if one is missing; it prints the command
that installs it. Say yes to linger: without it, the agent stops when you log out, and Home
Assistant can't sleep or wake a PC nobody is logged in to.

`torporctl` is now in `~/.local/bin`. The default bash profile on Debian and Ubuntu adds that folder
to your `PATH` at your next login, and Fedora's in each new shell; Arch's doesn't, and other shells
may not. `install` says when it isn't on your `PATH`: add it in your shell's profile, or call
`~/.local/bin/torporctl`.

## 3. Add the integration in Home Assistant

Install [ha-torpor](https://github.com/ovflowd/ha-torpor) (in HACS, as a custom repository), restart
Home Assistant, then go to **Settings → Devices & services → Add integration → Torpor**. It shows a
pairing code and the command to run, and waits up to 15 minutes:

```text
trp1-mp5s-7ncd-o7w2-vtka-jvzm-zay3-hmxu-zzv4
```

## 4. Pair

```bash
torporctl pair --url http://homeassistant.local:8123
```

```text
Pairing code from Home Assistant:
Pairing desk with http://homeassistant.local:8123 ...

In Home Assistant, check that it shows this PC as desk with the fingerprint TGVU-FESI.
If they match, confirm there and pick the features. If not, cancel there.
```

The code is read hidden, never from the command line. In Home Assistant, check the host name and
the fingerprint, choose **This is my PC**, and pick the features. Then:

```text
Paired. The key is in ~/.config/torpor/key.
The agent is connected.
```

If the fingerprints don't match, cancel in Home Assistant: someone else may be pairing with your
code. Nothing is stored on either side until you confirm.

## 5. Check

```bash
torporctl status
torporctl doctor
```

`status` should say `connection: connected`. `doctor` lists what works and prints a fix for what
doesn't, for example the polkit rule that lets Home Assistant sleep the PC while nobody is logged
in, or how to arm Wake-on-LAN.

## Next

- Allow more in [`torpor.conf`](configuration.md), for example `torporctl config set allow.poweroff yes`.
- Arm [Wake-on-LAN](wake-on-lan.md), so the Wake button works from sleep and from off.
- Set up automatic brightness ([brightness](modules/brightness.md)).
- Copy an [automation](automations.md).
