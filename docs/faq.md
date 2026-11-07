# FAQ

**Why not a long-lived Home Assistant token, like other PC agents?**
A long-lived token opens your whole Home Assistant: every lock, alarm and camera. Home Assistant has
no scoped tokens. Torpor's key opens one thing, this PC's connection to Torpor's endpoint, and the
PC holds no Home Assistant credential at all.

**Why does the PC dial Home Assistant, and not the other way round?**
Nothing listens on the PC, so there is no port to firewall or expose. Home Assistant's address is
usually fixed; the PC's may change. And the PC knows first when it wakes up.

**Is it safe over plain `http://`?**
After pairing, yes: every message is encrypted and authenticated, with fresh keys per connection.
During pairing, the one-time code crosses the network once, to your browser. Someone watching then
still learns nothing about the key; someone who can also change traffic could try to pair in your
place, which the fingerprint check catches. [security.md](security.md) has the details.

**Does it need root?**
No. The agent runs as you. Root is needed only for things you set up once, and `torporctl` prints
the commands: the packages, the polkit rule for acting with nobody logged in, Wake-on-LAN, and
`i2c-dev` for ddcutil.

**What happens when the PC sleeps?**
The agent tells Home Assistant just before, so the state turns `asleep` at once rather than after a
timeout. Every entity stays; the controls become unavailable, and Wake stays available.

**How does Home Assistant tell "asleep" from "off" after a power cut?**
It can't from Torpor alone: a PC that loses power sends no goodbye, and Home Assistant shows
`unknown` after two minutes. A smart plug's power sensor can tell; see
[automations.md](automations.md#tell-off-from-asleep-with-a-power-sensor).

**Can Home Assistant run commands on my PC?**
Only the fixed commands in the protocol: lock, suspend, hibernate, power off, restart, keep awake
and set brightness, and only those `[allow]` in `torpor.conf` permits. Nothing else.

**Two people use this PC. Whose session counts?**
Anyone's session at the desk makes the state `in_use`. The controls act on the sessions of the user
who runs the agent, and Torpor refuses to lock or sleep the PC while another user's unlocked session
is in front, unless Home Assistant passes `force`.

**Does it work on a laptop?**
Should: the backlight goes through logind, sleep and lock through logind, keep awake through your
desktop. Wake-on-LAN on laptops is rare. It hasn't been tested on one.

**Does it work without a desktop, on a server?**
The state, power, wake and leases work. Keep awake and brightness need a desktop session.

**Can I use it with KDE Connect, GSConnect or HASS.Agent?**
They don't conflict. Two tools that both set brightness or both auto-sleep the PC will, so pick one
for each job.

**How do I move to a new Home Assistant?**
Add the integration there and run `torporctl pair --url <new URL>`. The new pairing replaces the
key.

**How do I remove it?**
`torporctl uninstall --purge` on the PC, and delete the entry in Home Assistant.
