# Security

What Torpor protects, what it doesn't, and why the integration's endpoint does its own
authentication instead of Home Assistant's. The wire details are in [protocol.md](protocol.md).
To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Who holds what

| Piece | Runs as | Holds | Can do |
|---|---|---|---|
| `torpord` | You, as a systemd user service | The pairing key, `~/.config/torpor/key` (mode 0600, folder 0700) | What you can already do as a logged-in user: logind calls that polkit allows your session, your session bus, sysfs reads, and DDC/CI if you have access to `/dev/i2c-*`. Never root, never sudo |
| `torporctl` | You | Reads the same files | Installs files under your home, prints commands for anything that needs root, never runs `sudo` |
| The integration | Home Assistant | The same pairing key, in the config entry | Sends the fixed commands in [protocol.md](protocol.md#command-ha--pc-and-result-pc--ha), within the PC's `[allow]` list |

The pairing key opens one thing: this PC's connection to `/api/torpor/v1/ws`. It is not a Home
Assistant credential. A long-lived Home Assistant token, which PC agents often use, lets anyone who
reads it control every entity in the house, locks and alarm included, because Home Assistant has no
scoped tokens. Torpor puts no such token on the PC.

## Why the endpoint sets `requires_auth = False`

Home Assistant's security guide for integrations allows an unauthenticated view only when there is
no other way, and then the view must check every request itself. There is no other way here: the
alternative is a Home Assistant token on the PC, with the reach described above. So the view checks
every connection with the Torpor handshake before it acts on anything, as Home Assistant's own
WebSocket API checks its `auth` message. Until a connection has proved its key:

- each IP may open 10 connections per minute, and at most 16 may be unauthenticated at once;
- the whole handshake must finish within 10 seconds;
- messages are text, at most 4,096 bytes each;
- a connection from outside the local network is refused right after its first message, before any
  key work, unless that PC's entry allows outside connections; pairing is always local only;
- an unknown PC or pairing code gets a decoy challenge, so the answer doesn't reveal what is paired.

## Threat model

| Who | Can | Can't |
|---|---|---|
| Someone on your network who watches traffic | See that a PC connects, its `agent_id`, its agent version, and the size and timing of frames. While pairing, also its host name and OS. If Home Assistant is served over plain HTTP, the pairing code on its way to your browser | Read or change any message after the handshake. Learn the pairing key from a pairing they only watched. Decrypt recorded traffic later, even if the pairing key leaks then |
| Someone on your network who can also change traffic | Block or delay the connection, which shows in Home Assistant as disconnected. With a pairing code they saw: try to pair their own machine first, or sit in the middle, during the 15 minutes the code lives. The fingerprint check exposes both | Forge, replay or reorder a command or a state. Make either side accept an older protocol version. Get the PC's IP banned |
| Any program running as you on the PC | Read the pairing key and act as your PC toward Home Assistant: report false states, refuse commands | Reach anything else in Home Assistant |
| Any Home Assistant user, admin or not | Press any Torpor button. Home Assistant has no per-entity permissions | Run a command that isn't in the protocol, or one your `[allow]` list forbids |
| Someone with a copy of your Home Assistant backup | Read the pairing key and, with access to your network, act as the PC or as Home Assistant toward the other | Decrypt traffic recorded before |
| A compromised Home Assistant | Whatever `[allow]` permits: lock, sleep, keep awake, set brightness; power off, restart and hibernate only if you allowed them | Run arbitrary commands, read files, or raise `[allow]` |
| Someone on the internet, if Home Assistant is reachable from it | Reach the endpoint and get refused | Get past the local-only check, unless you turned on *Allow agents outside the local network* for that PC; even then, they still need the key |

## Pairing over plain HTTP

The pairing code crosses the network once: to your browser, when Home Assistant shows it. If Home
Assistant is served over plain HTTP, anyone watching the network can read it then. The pairing key
never crosses the network.

That one exposure is limited:

- The code only authenticates an X25519 exchange. The pairing key comes out of that exchange, so
  watching the code and the exchange is not enough to learn the key.
- The code is 160 random bits, lives at most 15 minutes, is burned after three wrong proofs, and is
  worthless once pairing ends.
- To use a stolen code, an attacker must act during those minutes, either by pairing first or by
  sitting in the middle. Both change the fingerprint: `torporctl pair` and Home Assistant's confirm
  step show it, and you confirm only when they match.
- An attacker who can rewrite plain-HTTP traffic could also rewrite the Home Assistant page and the
  fingerprint it shows. That attacker can already take over your Home Assistant login on the same
  plain-HTTP connection, so Torpor can't do better than Home Assistant here.

To avoid all of this, serve Home Assistant over https, or open it in a browser on the Home Assistant
host itself while you pair.

## On the wire after pairing

Every message after the handshake is encrypted and authenticated with ChaCha20-Poly1305, under keys
made fresh for each connection from the pairing key and an X25519 exchange. That holds over plain
`ws://` too. Each direction has its own key and a strict sequence number, so a captured frame can't
be replayed, reordered or sent back to its sender.

What stays visible: the handshake messages (`agent_id`, agent version, and while pairing the host
name and OS), and the size and timing of frames. A watcher can guess when the PC changes state from
the timing alone, but can't read the state.

## TLS

When Home Assistant is served over https, the agent connects with `wss://` and checks the
certificate against the system's CA store. A Home Assistant reached by IP with a certificate for a
public name, or with a self-signed certificate, fails that check. Setting `tls-verify = no` in
`torpor.conf` is acceptable there: the Torpor handshake still authenticates Home Assistant and
encrypts every frame, and you lose only the cover TLS gives the handshake itself. Torpor does not
pin certificates.

## Failed logins and bans

Home Assistant can ban an IP after repeated failed logins. A PC with an old key must not get its
own address banned, since that would also lock its browser out of Home Assistant. Home Assistant
proves itself first, so a PC with an old key gives up before it sends anything that could count, and
waits 15 minutes before it tries again. Home Assistant counts a failure only when someone presents a
wrong proof for a PC it knows, which the real PC can't do. The details are in
[protocol.md](protocol.md#failures-and-bans).

## Keys at rest, rotation and removal

- **On the PC**, the key is a 0600 file in a 0700 folder. Any process running as you can read it;
  backups of your home folder carry it.
- **In Home Assistant**, it sits in the config entry, in `.storage/core.config_entries`, and in
  every backup. Turn on backup encryption.
- **Rotate** with *Reconfigure* on the entry, then `torporctl pair` on the PC. The old key stops
  working at once.
- **Revoke** by deleting the entry in Home Assistant. The PC then sees a challenge it can't verify,
  backs off, and `torporctl status` says it needs pairing. `torporctl uninstall --purge` deletes the
  key on the PC.

## What the PC refuses, whatever Home Assistant asks

- Any action that `[allow]` in `torpor.conf` forbids. Power off, restart and hibernate are off
  until you allow them.
- Sleep while another app holds a sleep blocker, unless the command says `force`. `force` only skips
  Torpor's own check; logind still decides, and Torpor never asks it to ignore blockers.
- Automatic brightness writes beyond its gates (settled readings, 3-point steps, minutes apart).
  A Brightness command from Home Assistant is one write per command.
- Everything, in observe-only mode.
- Running commands, reading or writing files on request, or opening a shell. The protocol has no
  message for any of that.

## What Torpor reads and writes

The agent reads logind's sessions and inhibitors, `/sys/class/drm` (is a display lit),
`/proc/net/tcp` (connections to ports you list), `/proc/*/cmdline` (only to match `[busy]
processes`), `/sys/class/net` and NetworkManager (MACs and Wake-on-LAN status),
`/sys/class/backlight`, your desktop's brightness service, `ddcutil` output, and whether GameMode
has a client or power-profiles-daemon a performance hold.

Torpor writes, never as root:

- `~/.config/torpor/torpor.conf` and `~/.config/torpor/key` (0600, folder 0700), only through
  `torporctl`;
- `~/.local/state/torpor/` (0700) for `known.json`;
- `$XDG_RUNTIME_DIR/torpor/` (0700) for the status, boot-scoped state and leases;
- the unit and its hardening drop-in under `~/.config/systemd/user/`;
- the zipapp under `~/.local/lib/torpor/`, and `~/.local/bin/torporctl`.

[install.md](install.md#files) lists every file with its mode.

## What the PC sends to Home Assistant

- Host name, OS name, kernel and Python versions, the PC's maker and model from DMI, the desktop's
  name, whether lingering is on, the boot time and uptime.
- Network interface names, MAC addresses and whether Wake-on-LAN looks armed.
- Display labels (maker and model), their brightness, and the value of a monitor's mode code when
  Home Assistant set one.
- Whether a game runs (a GameMode client, or a performance hold matching your pattern), not which.
- Whether a session is unlocked, locked or at the login screen, whether a display is lit, whether
  someone is connected over SSH, another remote session or a port you listed (`port:3389`), the
  names of your leases, and which of your `[busy]` patterns matched.
- The names and reasons of other apps' sleep blockers, as logind reports them.

It sends this to your Home Assistant only, encrypted, and nowhere else. It never sends user names,
process command lines, file contents or the pairing key.
