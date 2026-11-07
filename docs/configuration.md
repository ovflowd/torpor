# Configuration

`~/.config/torpor/torpor.conf` holds the facts about this PC and the ceilings Home Assistant can't
go beyond. What Torpor does with them (which features, the brightness curve, the light sensor, the
keep-awake length, the Wake-on-LAN address) is set in Home Assistant, in the integration's options.
No setting lives in both places.

Edit it with `torporctl config`, which keeps comments and order and restarts a running agent:

```bash
torporctl config list
torporctl config get allow.suspend
torporctl config set allow.poweroff yes
torporctl config set ha-url http://10.0.0.5:8123
torporctl config unset core.remote-ports
```

Keys are `section.key`; `ha-url` is short for `agent.ha-url`. A multi-line value takes `\n`:
`torporctl config set busy.processes 'rsync\nborg create'`. You can edit the file by hand too; then
run `torporctl restart`.

## Rules

- A missing file means every default.
- Booleans accept `yes`, `no`, `true`, `false`, `on`, `off`, `1`, `0`.
- An unknown key or section is a warning in the log and in `doctor`, and changes nothing.
- An invalid value falls back to its default and logs an error. In `[modules]` and `[allow]` an
  invalid value means `no`, never the default.
- A file that doesn't parse at all (a duplicate key, a line outside any section) allows nothing
  until you fix it, and `doctor` fails.
- A comment starts with `;` or `#` at the start of a line, or with ` ;` after a value.

## `[agent]`

| Key | Default | |
|---|---|---|
| `ha-url` | empty | Home Assistant's address, such as `http://homeassistant.local:8123`. `torporctl pair` writes it |
| `observe-only` | `no` | Report, but execute nothing ([install](install.md#observe-only)) |
| `log-level` | `info` | `debug` adds protocol traces: message types only, never keys, proofs or frames |
| `tls-verify` | `yes` | Check Home Assistant's certificate on `https://`. `no` is acceptable for a self-signed certificate: the Torpor handshake authenticates Home Assistant and encrypts every frame anyway |

To move to a new address of the same Home Assistant, `config set ha-url`; the key still works. For
another Home Assistant, or after Reconfigure, run `torporctl pair` again.

## `[modules]`

What runs on this PC at all. A module set to `no` reports nothing and does nothing, and Home
Assistant doesn't offer it.

| Key | Default |
|---|---|
| `power` | `yes` |
| `keep-awake` | `yes` |
| `wake` | `yes` |
| `brightness` | `yes` |

`core`, the state, always runs.

## `[allow]`

What Home Assistant may trigger. Home Assistant creates a button only for what this allows, and the
agent refuses anything else with `not_allowed`.

| Key | Default | |
|---|---|---|
| `lock` | `yes` | Lock your desktop sessions |
| `suspend` | `yes` | Suspend to RAM |
| `hibernate` | `no` | Hibernate; also needs logind to say it can, at that moment |
| `poweroff` | `no` | Power off |
| `reboot` | `no` | Restart |
| `keep-awake` | `yes` | The keep-awake switch. Turning it off is always accepted, and at `no` a switch left on ends when the agent restarts |
| `brightness` | `yes` | Setting a display's brightness, by hand or automatically |

## `[core]`

| Key | Default | |
|---|---|---|
| `ssh` | `yes` | An SSH session makes the state `remote` |
| `remote-ports` | empty | TCP ports, space or comma separated, such as `3389 5900` (RDP, VNC). An established inbound connection to one makes the state `remote` (`port:3389`) |

## `[busy]`

| Key | Default | |
|---|---|---|
| `processes` | empty | One regular expression per line, matched with a search against every process's full command line (arguments joined by spaces, any user's). A match makes the state `busy` (`process:<pattern>`) and holds off idle sleep |
| `lease-ttl` | `300` | Seconds a [lease](modules/keep-awake.md#leases) without `until` or `pid` stays live after its last touch, 10 to 86400 |

```ini
[busy]
processes =
    ^rsync .*backup
    borg create
```

## `[keep-awake]`

| Key | Default | |
|---|---|---|
| `backend` | `auto` | `auto`, `kde`, `gnome` or `portal`. `auto` takes the first one running on your session bus, in that order |

## `[brightness]`

| Key | Default | |
|---|---|---|
| `backend` | `auto` | `auto`, `kde`, `ddcutil`, `logind` or `none`. [brightness](modules/brightness.md) explains `auto` |
| `ddcutil` | `ddcutil` | The `ddcutil` binary, a name on `PATH` or an absolute path |

## `[games]`

What counts as a game running, for the brightness boost and the Game running sensor. How much the
boost adds is set in Home Assistant. [brightness](modules/brightness.md#game-boost) has the details.

| Key | Default | |
|---|---|---|
| `gamemode` | `yes` | A game registered with Feral GameMode counts |
| `performance-hold` | empty | A regular expression. A power-profiles-daemon `performance` hold whose reason matches counts. Empty turns it off |

```ini
[games]
performance-hold = game-performance    ; CachyOS's game-performance launcher
```

## `[display:<id>]`

One section per display you want to treat differently, by the 12-character ID that `torporctl
status` shows.

| Key | Default | |
|---|---|---|
| `backend` | `auto` | `none` leaves the display out. Another backend name leaves it out of every other backend |

```ini
[display:3f2a9c1b04de]
backend = none
```

## The default file

`torporctl install` writes this when there is no file yet:

```ini
[agent]
ha-url =                    ; e.g. http://homeassistant.local:8123, written by torporctl pair
observe-only = no
log-level = info            ; info | debug
tls-verify = yes

[modules]                   ; what runs on this PC at all
power = yes
keep-awake = yes
wake = yes
brightness = yes

[allow]                     ; what Home Assistant may trigger; it can't go beyond this
lock = yes
suspend = yes
hibernate = no
poweroff = no
reboot = no
keep-awake = yes
brightness = yes

[core]
ssh = yes                   ; SSH sessions count as remote
remote-ports =              ; e.g. 3389 5900: inbound connections to these ports count as remote

[busy]
processes =                 ; one regular expression per line, matched against full command lines
lease-ttl = 300             ; seconds an untimed lease file stays live after its last touch

[keep-awake]
backend = auto              ; auto | kde | gnome | portal

[brightness]
backend = auto              ; auto | kde | ddcutil | logind | none
ddcutil = ddcutil           ; path to the ddcutil binary

[games]                     ; what counts as a game running, for the brightness boost
gamemode = yes              ; a Feral GameMode client counts
performance-hold =          ; regular expression for a power-profiles-daemon performance hold's reason
```
