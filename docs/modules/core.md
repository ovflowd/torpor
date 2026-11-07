# core: the state

Always on. It tells Home Assistant whether someone is using the PC, and says goodbye before the PC
sleeps, powers off or restarts.

## Requirements

systemd-logind on the system bus. Nothing else.

## The facts

Each fact may be unknown.

| Fact | Source |
|---|---|
| Sessions | logind's sessions: seat, class, type, state, active, remote, service, `LockedHint`, user |
| Desk session | The active session on `seat0` with class `user` and type `wayland`, `x11` or `mir`, whoever owns it |
| Login screen | An active greeter session on `seat0` and no desk session |
| Locked | The desk session's `LockedHint`. For your own session, a lock screen that answers `org.freedesktop.ScreenSaver.GetActive` (Plasma's does) also counts, because a screen locked at login may never set `LockedHint` |
| Display lit | `/sys/class/drm/card*-*/dpms` of connected connectors: any `On` is lit, all `Off` is dark, nothing readable is unknown |
| Remote | SSH sessions (`ssh`), other remote logins (`session`), established inbound connections to `[core] remote-ports` (`port:3389`) |
| Busy | The keep-awake switch (`keep_awake`), live leases (`lease:build`), `[busy] processes` matches (`process:<pattern>`) |
| Boot time | `btime` in `/proc/stat` |
| Game | A Feral GameMode client, or a power-profiles-daemon `performance` hold whose reason matches `[games] performance-hold`; unknown without either. It doesn't change the state; [brightness](brightness.md#game-boost) uses it |

## The state

First match wins:

1. `in_use`: a desk session that isn't known to be locked, and a display that isn't known to be dark.
2. `remote`: any remote reason.
3. `busy`: any busy reason.
4. `idle`.

Unknown facts lean to `in_use`, the state in which automations leave the PC alone. Another user's
session at the desk counts too: if your partner is using the PC, it's in use.

The agent reads the facts every 10 seconds and on events, and sends the state when it changes, at
most once a second. If logind can't be read for a moment, the last state stays; a bus hiccup never
looks like an empty desk.

Home Assistant adds three states of its own from the goodbye: `asleep`, `off` and `restarting`.

## Going down

The agent always holds a logind `delay` inhibitor on sleep and shutdown, observe-only included. When
logind announces a sleep or shutdown, the agent sends `going_down`, waits up to 2 seconds for Home
Assistant's acknowledgement, closes the connection and lets go of the inhibitor.

| logind says | `kind` | Home Assistant shows |
|---|---|---|
| `PrepareForSleep` | `sleep` (with `suspend` or `hibernate` when Home Assistant asked for it) | `asleep` |
| `PrepareForShutdownWithMetadata`, type `poweroff` or `halt` | `poweroff` | `off` |
| the same, type `reboot`, `kexec` or `soft-reboot` | `reboot` | `restarting` |
| only `PrepareForShutdown` (systemd older than 254) | `unknown` | `off` |

logind can't tell a suspend from a hibernate. When Home Assistant didn't ask, the kind is just `sleep`.

On resume the agent takes the inhibitor again, dials Home Assistant at once and sends `resumed`.
When it starts, it sends `started` with the boot time and uptime, so Home Assistant can tell a boot
from an agent restart.

## Entities

| Entity | |
|---|---|
| `sensor.<pc>_state` | The state, with the attributes `session`, `display_on`, `remote` and `busy` |
| `binary_sensor.<pc>_agent` | On while the agent is connected |
| `event.<pc>_power` | `suspending`, `shutting_down`, `restarting`, `resumed`, `booted`, `wake_succeeded`, `wake_failed` |
| `sensor.<pc>_last_boot` | When the PC last booted (off by default) |
| `binary_sensor.<pc>_game_running` | On while a game runs. Only on a PC with GameMode or a `[games] performance-hold` |

## Limitations

- A locker that sets neither `LockedHint` nor answers `org.freedesktop.ScreenSaver` makes a locked
  desk look `in_use`. Most wlroots lockers set `LockedHint` through logind.
- A monitor that is off but still reports its connector as connected and `On` looks lit.
- Remote desktop servers that don't create a logind session count only through
  `[core] remote-ports`.
