# Troubleshooting

Start with these two:

```bash
torporctl status     # the connection, the last close and what it means
torporctl doctor     # every check, with the command that fixes it
```

`torporctl logs -f` follows the agent's log. For more detail, `torporctl config set agent.log-level
debug`; it logs message types, never keys, proofs or frames.

## Connecting

**"re-pair needed: the key was refused" (close 4401).** Home Assistant didn't prove this PC's key:
the entry was reconfigured or deleted, or you paired with another Home Assistant. The agent waits
15 minutes between tries and never sends a proof it can't verify, so your PC's address can't get
banned. In Home Assistant, use **Reconfigure** on the PC's entry (or add the integration again),
then run `torporctl pair`.

**HTTP 404.** Nothing answers `/api/torpor/v1/ws` at that address: ha-torpor isn't installed, Home
Assistant hasn't loaded it yet, or `ha-url` points somewhere else. `torporctl doctor` checks the
endpoint without sending anything that could count against you.

**"refused a connection from outside its local network" (close 4403).** Home Assistant only accepts
agents from its local network: loopback, private ranges (10/8, 172.16/12, 192.168/16), `fc00::/7`
and link-local. Tailscale's IPv4 range is not local by that rule. Either connect over the LAN, or turn
on *Allow agents outside the local network* in that PC's options. Pairing is always local only.

**"the Torpor entry isn't loaded" (close 4503).** The entry exists but is disabled, failed to set up,
or Home Assistant is still starting. The agent tries again every minute.

**The WebSocket upgrade fails with another HTTP status.** A reverse proxy in front of Home Assistant
must pass WebSocket upgrades on `/api/torpor/v1/ws`, as it does for `/api/websocket`.

**A self-signed certificate.** `torporctl config set agent.tls-verify no`. The Torpor handshake still
authenticates Home Assistant and encrypts every frame.

**Home Assistant moved to another address.** `torporctl config set ha-url http://10.0.0.5:8123`.
The key still works. A DHCP reservation keeps the address from moving.

**Pairing says the code is wrong, expired or already used.** Codes live 15 minutes and are burned
after three wrong tries. Start the pairing again in Home Assistant and copy the new code.

## Power

**Sleep, power off or restart do nothing while nobody is logged in.** polkit asks for an admin
password with no active session. `torporctl doctor` prints a rule for the actions you allowed;
install it as root. See [power](modules/power.md#with-nobody-logged-in).

**Sleep is refused with "1 app blocks sleep".** Another app holds a sleep inhibitor, often a video
player or a download. The refusal names it. `torpor.sleep` with `force: true` skips Torpor's check;
logind still decides.

**The Hibernate button is unavailable.** logind's `CanHibernate` isn't `yes`: no swap large enough,
or Secure Boot without hibernation support. `busctl call org.freedesktop.login1
/org/freedesktop/login1 org.freedesktop.login1.Manager CanHibernate` shows what logind says.

## The agent

**The agent stops when I log out.** Linger is off: `loginctl enable-linger`.

**doctor warns "no hardening drop-in".** The probe failed under the hardening settings, usually
because user namespaces are restricted, or you installed with `--no-hardening`. The agent works
without it. `torporctl install` probes again, and its output ends with why the probe failed.

**`torporctl` isn't found after install.** It is in `~/.local/bin`. The default bash profile on
Debian and Ubuntu adds that folder to `PATH` at your next login, and Fedora's in each new shell;
Arch's doesn't, and other shells may not. Add it in your shell's profile, or call
`~/.local/bin/torporctl`.

**The agent fails to start after a distribution upgrade.** The unit names the interpreter it was
installed with. After a Python upgrade, PyGObject and `cryptography` must be there for the new one
too; run `torporctl install` again from your checkout.

## Brightness

**No `ddcutil` displays.** Check, in order: `ddcutil` is installed; `lsmod | grep i2c_dev` shows the
module (`echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf`); you can read and write
`/dev/i2c-*` (the `i2c` group, or ddcutil's udev rule); DDC/CI is on in the monitor's own menu; and
`ddcutil detect` works as you. `torporctl doctor` checks the first three.

**Automatic brightness doesn't change anything.** It writes only while the desk is in use, after
the light reading has held for two minutes, for a change of 3 points or more, and at most every 5
minutes per monitor. A hand change pauses it until the light changes a lot: turn the auto switch off
and on to resume at once. `torporctl status --json` shows each display's `target` and whether it is
`overridden`.

**Brightness jumps between two values.** Two programs are writing: with Plasma, leave `[brightness]
backend` on `auto` so ddcutil isn't used; with another tool such as wluma or clightd, turn one off.

**A repair says the PC can't read monitor modes.** Mode reads need ddcutil 2.2 or later (Ubuntu
24.04 has 1.4) and access to `/dev/i2c-*`. `torporctl doctor` says which is missing under `modes`,
also before any display has a mode code, and lists each display whose reads fail. Until then the
display uses its lowest mode anchor. If the repair says the agent is too old, update it with
`torporctl update` and a newer checkout or release: an agent without mode reads ignores the rows and
uses the curve's own anchor.

**The Mode sensor shows an error.** `torporctl logs -f` has the reason, one line per failed read,
with the next try. "no working DDC/CI display matches" means detect didn't find the monitor's model
at the end of its Plasma label; "2 DDC/CI displays match" means two monitors of the same model, which
mode reads can't tell apart; "none of the values set in Home Assistant" names the value the monitor
answered, which needs a row of its own; until it has one, the last known mode stays. A read that
times out usually means the monitor is waking or switching inputs; the next try comes on its own.

**The game boost never comes.** `binary_sensor.<pc>_game_running` must turn on: check
`torporctl doctor` under `games`. A game started without GameMode, and with no performance hold
matching `[games] performance-hold`, is invisible to Torpor. The boost waits 10 s after a game
starts, and at least a minute after the last write.

## Keep awake

**The switch is on but the PC still sleeps.** `torporctl status` shows whether the inhibit is
`applied`. It needs a desktop: at the login screen there is nothing to inhibit. On wlroots
compositors whose portal has no Inhibit, the module is unavailable.
