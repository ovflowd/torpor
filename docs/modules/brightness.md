# brightness: displays and automatic brightness

A Brightness slider per display, and automatic brightness that follows a light sensor in Home
Assistant, with a curve per monitor mode and a boost while you play. Writes are rare on purpose:
many monitors keep the brightness in memory that wears, and nobody wants the screen to flicker every
time a cloud passes.

## Backends

| Backend | Displays | Needs |
|---|---|---|
| `kde` | Every display Plasma's `org.kde.ScreenBrightness` lists, external and built in | Plasma 6.2 or later, detected while the agent runs |
| `ddcutil` | External monitors with DDC/CI | `ddcutil`, the `i2c-dev` module and access to `/dev/i2c-*` |
| `logind` | `/sys/class/backlight` panels, set through logind's `Session.SetBrightness` | A graphical session of yours, active |

**`auto`:** when Plasma's brightness service is on your session bus, every display it lists uses
`kde`, and `ddcutil` and `logind` aren't used at all. Plasma does its own DDC/CI and backlight
writes, and two writers would fight. Once Plasma has driven the displays on this PC, that stays so
while you are logged out, but not while you are logged in to another desktop. Without Plasma, each
backlight uses `logind` and each monitor `ddcutil` finds uses `ddcutil`.

Override it for all displays with `[brightness] backend`, or leave one out:

```ini
[display:3f2a9c1b04de]
backend = none
```

## Display IDs

Each display has a 12-character ID, stable for that display and backend, from the Plasma label (with
`#1`, `#2` when two displays share a label), the monitor's EDID (`MFG:MODEL:SERIAL`, or its I²C
bus when it has no serial), or the backlight's name. `torporctl status` lists them. A display that
moves to another backend gets a new ID and new entities. Displays seen in the last 30 days stay
listed, so their entities survive a logout or an unplugged cable.

## ddcutil setup

```bash
sudo pacman -S ddcutil          # or apt install ddcutil, dnf install ddcutil
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf
sudo modprobe i2c-dev
```

Then give yourself access to `/dev/i2c-*`. Many distributions do it through the `i2c` group:

```bash
sudo usermod -aG i2c "$USER"    # log out and in again
```

Where there is no such group, ddcutil ships a udev rule that grants access to the logged-in user;
see [ddcutil's notes on permissions](https://www.ddcutil.com/i2c_permissions/). If `ddcutil detect`
works as you, Torpor's `ddcutil` backend does too. `torporctl doctor` checks the module and the
access.

Torpor calls ddcutil one call at a time, with a 15 s timeout and one retry after 2 s. A display that
fails stays in error until the PC resumes, it is detected again, or you turn its automatic
brightness off and on. It detects again at start, after a resume and when the set of connected
displays changes. ddcutil 1.4 (Ubuntu 24.04) to 3.0 work.

## Automatic brightness

Off for every display until you turn it on. In Home Assistant's options, pick an illuminance sensor
(`sensor` with `device_class: illuminance`) and tune the curve; then turn on
`switch.<pc>_<display>_auto_brightness`. Home Assistant sends the sensor's readings to the agent.

The curve: `target = anchor_pct + slope × log10(lux / anchor_lux)`, kept between floor and ceiling.

| Option | Default | |
|---|---|---|
| `anchor_lux`, `anchor_pct` | 100 lx, 40 % | One point you like |
| `slope` | 25 | Points per tenfold change in light |
| `floor`, `ceiling` | 10 %, 80 % | |

With the defaults, 10 lx is 15 %, 100 lx 40 % and 1000 lx 65 %. Tune it: sit at your desk at a few
light levels, set what you like by hand, and fit the anchor and slope.

### When it writes

All of these must hold:

| Gate | Value |
|---|---|
| The state | `in_use` |
| Settle after the desk comes into use, a display comes back, or a resume | 30 s |
| The light reading has held for | 120 s, so lamp fades and quick on-and-off pass by |
| The change is at least | 3 points |
| Time since the last automatic write to that display | 300 s for monitors (`ddcutil`, `kde` external); 60 s for backlights (`logind`, `kde` built in) |
| After a failed write | stops for that display until a resume, the backend restarting, the display coming back, or auto off and on |
| A display with a [mode code](#monitor-modes) | a read of its mode that ended less than 15 s before |
| A [game](#game-boost) starting or ending | 10 s since, for writes and mode reads alike |

A game starting or ending (with a boost above 0), or a monitor mode with another anchor, is an edge:
it needs no settled reading, only the last settled one, and 60 s since the last write instead of
300 s. A game that keeps crashing and starting again still writes at most once a minute.

Nothing is written or read once the PC starts going to sleep or shutting down, until it is back, or
once the agent stops: not even a write that waited for a read ending then.

### Hand changes

A change Torpor didn't make pauses automatic brightness for that display:

- `kde`: Plasma's slider or keys, at least 1 % away, while the desk is in use;
- `ddcutil`: before each automatic write it reads the monitor; 2 points away from what it last knew
  counts (the monitor's own buttons, another program);
- `logind`: the backlight read every 10 s, 2 points away;
- Home Assistant's Brightness slider.

The pause takes the curve's target once the reading has settled as its baseline, and ends when the
target moves 6 points from it (the room got much darker or brighter, or a game's boost came or went),
when the monitor changes to a mode you have a row for, when you turn auto off and on, or on resume.
It survives an agent restart, not a reboot.

## Monitor modes

Some monitors change how bright a percentage looks with a setting in their own menu. On the ASUS
ROG PG39WCDM, the OSD's Power Setting is VCP code `0xE1`: `00` Standard, `01` Power Saving. At the
same 88 lx, about 40 % looks right on Standard and 70 to 80 % on Power Saving. One curve can't be
right for both.

So each display can have a mode code: one VCP code Torpor reads with ddcutil, and a curve anchor for
each of its values. In Home Assistant's options, under Automatic brightness, add one row per value:

| Display | Code | Value | Anchor | Label |
|---|---|---|---|---|
| PG39WCDM | `E1` | `00` | 40 % | Standard |
| PG39WCDM | `E1` | `01` | 75 % | Power Saving |

The value's anchor replaces `anchor_pct` for that display; the anchor light level, slope, floor and
ceiling stay. Raise the ceiling if a mode's anchor is near it. With no rows, every display has one
curve, as before.

To find a monitor's code, look for its OSD setting in its documentation or in
`ddcutil capabilities`, run once by hand. To learn the values, add one row with the code and no
value, then change the setting in the OSD and watch the display's Mode sensor in Home Assistant: it
shows the raw value the monitor answers, within 2 minutes. Once there are values, a value none of
the rows has shows as the sensor's error, which names it.

**What it needs:** ddcutil 2.2 or later and access to `/dev/i2c-*` (the [setup](#ddcutil-setup)),
whatever the backend: with `kde`, Plasma still does every write and Torpor only reads. ddcutil 1.4
(Ubuntu 24.04) has none of the options below, and 2.1 had the bus lock off by default.
`torporctl doctor` checks both, and the capabilities tell Home Assistant, which raises a repair when
a display has a mode code and the PC can't read it.

**When it reads:** once the desk has been in use for 30 s, every 2 minutes, and right before each
automatic write to that display, which waits for a read that ended less than 15 s before. A mode
counts as stale, and is read at the first moment the gates allow, after an agent start, a resume, a
display coming back or getting a new name, and a hand-set brightness; a read still running then
doesn't count, since it may have seen the monitor before you changed its OSD. Nothing is read while
the desk is locked, dark, remote or logged out.

Each read is one or two ddcutil calls, never anything else:

```bash
ddcutil --noconfig --disable-udf --enable-cross-instance-locks --disable-dynamic-sleep detect --terse
ddcutil --noconfig --disable-udf --enable-cross-instance-locks --disable-dynamic-sleep \
    --skip-ddc-checks getvcp --bus 9 --verbose 0xE1
```

Detect finds the display's bus: by its EDID for the `ddcutil` backend, by the model at the end of
Plasma's label for `kde`. Two monitors of the same model can't be told apart this way, and their
mode reads fail. The bus is kept and found again only after a start, a resume, a renumber or a
failed read.

### Why the reads can't fight PowerDevil

With `kde`, PowerDevil's libddcutil writes the brightness over the same I²C bus. Two programs
talking to a monitor at once garble both, and PowerDevil turns a monitor's brightness control off
after four failed writes, until it restarts. So:

- **Both take ddcutil's cross-instance lock.** libddcutil takes a `flock` on `/dev/i2c-N` around
  every read and write, on by default since 2.2. Torpor's ddcutil asks for it too, and `--noconfig`
  keeps a `ddcutilrc` from turning it off. `--disable-udf` keeps user feature definitions out,
  `--disable-dynamic-sleep` keeps the standard DDC/CI waits, and `--skip-ddc-checks` skips the extra
  probes of `0x10` and `0xDD` a getvcp would make.
- **A read never holds the lock long enough to fail a write.** A PowerDevil open waits at least 3 s
  for the lock. Torpor kills its ddcutil after 2.5 s, and the lock goes with it.
- **Reads stay out of PowerDevil's way.** PowerDevil writes 50 ms after a brightness change and
  retries a failed write 1, 2 and 3 s later; even if every attempt waited 3 s for the lock, that is
  over in 18 s. Torpor reads nothing within 30 s of any brightness change (yours, the keys, its own
  write, echoes), a display coming or going, or a new PowerDevil, and never while one of its own
  writes runs. A write never starts while a read runs; a Brightness set from Home Assistant waits
  for it, at most 2.5 s.
- **One call at a time, 100 ms apart.** Each call starts at least 100 ms after the last one ended,
  so a PowerDevil write waiting for the lock gets it between detect and getvcp.

A read is one DDC/CI request of about 0.1 s that changes nothing in the monitor, so it doesn't wear
its memory.

**When a read fails** (killed after 2.5 s, an error, or a reply that doesn't fit, such as another
code's data), the last known mode stays, or the lowest anchor if none was ever read. The bus is found
again on the next try, 60 s later, doubling up to 30 minutes, and a write waiting for that read waits
for the retry: a monitor that doesn't answer a read would fail a write too. After 3 failures in a
row, writes go ahead with the last known mode; reads go on at the backed-off pace, and one that
works resets it all, as does a resume or a renumber. If `ddcutil --version` fails, Torpor asks again
on the same back-off and after each resume.

**A value none of your rows has** is no failure: the monitor answered. It may be a mode you haven't
added yet, or the PG39WCDM's `FE` for a setting its OSD greys out. The last known mode stays, writes
go on, and the Mode sensor's error names the value. A value you have a row for is back at the next
read.

The last mode read survives an agent restart (`state.json`, not a reboot), so a hand-set pause isn't
ended by the lowest anchor while the monitor can't be read. The first read that works after none had
takes the pause's baseline again instead of ending it: the mode was learned, not changed.

## Game boost

While a game runs, automatic brightness adds a boost, 30 points by default, up to 100 %, past the
curve's ceiling. Set it under Automatic brightness in Home Assistant's options; 0 turns it off.

What counts as a game, set in `[games]` in `torpor.conf`:

| Source | Counts when | Default |
|---|---|---|
| Feral GameMode | `com.feralinteractive.GameMode` on your session bus has a client (`ClientCount` > 0) | on, when GameMode is installed |
| power-profiles-daemon | a `performance` hold whose reason matches `[games] performance-hold` | off until you set a pattern |

GameMode is the one most distributions and launchers share: Steam's `gamemoderun %command%` and the
GameMode options in Lutris and Heroic turn it on. On CachyOS, `game-performance` takes a hold whose
reason is `Launched with CachyOS game-performance utility`, so `performance-hold = game-performance`
covers it. Torpor never starts gamemoded or power-profiles-daemon to ask.

A start or end counts once it has held 10 s: a launcher may switch the refresh rate, a full modeset,
right before a game starts and right after it ends, and a monitor may not answer DDC/CI then. Each
source the PC has installed shows in its capabilities, whether its daemon runs right now or not, and
the PC's Game running sensor in Home Assistant follows the answer.

## Entities

| Entity | |
|---|---|
| `number.<pc>_<display>_brightness` | 0 to 100 %. Setting it counts as a hand change |
| `switch.<pc>_<display>_auto_brightness` | Only with a light sensor in the options. Attributes `overridden` and `target` |
| `sensor.<pc>_<display>_mode` | Diagnostic, only for a display with a mode code. The value's label, or the raw value. Attributes `code`, `value` and `error` |
| `binary_sensor.<pc>_game_running` | On the PC, when it has a game source. On while a game runs |

## Limitations

- A desktop's own idle dimming changes the brightness too, and may count as a hand change.
- Late-night dimming is not in this version. A mode moves only the anchor of a display's curve;
  the rest of the curve is the same for every display.
- Changes made with the monitor's own buttons are invisible to Plasma, and the next write overrides
  them. A mode is read, but nothing else in the OSD is.
- The `kde` backend's displays include virtual outputs (remote-desktop screens); leave those out
  with `[display:<id>] backend = none`.
