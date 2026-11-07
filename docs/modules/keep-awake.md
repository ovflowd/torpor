# keep-awake: the switch, leases and busy processes

Holds off the desktop's idle sleep while Home Assistant's Keep awake switch is on, while a lease is
live, or while a process you named runs. An explicit Sleep still works: the inhibit stops idling,
not requests.

## Requirements

A desktop idle inhibit on your session bus:

| Backend | Service | Status |
|---|---|---|
| `kde` | PowerDevil's `org.freedesktop.PowerManagement.Inhibit` | In use on Plasma 6; should work on Plasma 5 |
| `gnome` | `org.gnome.SessionManager.Inhibit`, suspend and idle | Should work |
| `portal` | `org.freedesktop.portal.Inhibit`, suspend and idle | Should work where the portal backend implements Inhibit; `xdg-desktop-portal-wlr` doesn't |

`auto` takes the first one running, in that order. A backend whose first call fails is listed in the
module's problems, and the next one is tried. With none, the module is unavailable; there is no
silent fallback. A backend that worked once on this PC keeps the module available while you are
logged out, so Home Assistant's switch doesn't flicker.

## Settings

| Where | Setting |
|---|---|
| `torpor.conf` | `[modules] keep-awake`, `[allow] keep-awake`, `[keep-awake] backend`, `[busy] processes`, `[busy] lease-ttl` |
| Home Assistant | How long the switch stays on (default 12 h; 0 for until you turn it off) |

## The switch

`switch.<pc>_keep_awake` and the `torpor.keep_awake` action. The PC keeps the end time: an agent
restart keeps it, a reboot drops it, and time asleep counts toward it. With no desktop running when
you turn it on, the request waits and is applied when one appears. The switch's `ends_at` attribute
says when it turns off. Setting `[allow] keep-awake = no` ends a switch left on.

While it is on, the state is at least `busy`, with the reason `keep_awake`.

## Leases

A lease is a file in `$XDG_RUNTIME_DIR/torpor/leases/`, named `<name>.lease`, where the name is 1 to
64 of `A-Z a-z 0-9 . _ -`. Each live lease makes the state `busy` (`lease:<name>`) and holds the
idle inhibit. Its content is zero or more lines:

| Line | Live while |
|---|---|
| `until=<unix seconds>` | Until then |
| `pid=<pid>` | That process exists |
| neither | The file's modification time is within `[busy] lease-ttl` seconds (300); touch it to renew |

With both lines, both must hold. Unknown lines are ignored.

```bash
torporctl hold render --for 2h            # writes until=
torporctl hold backup -- borg create ::x  # holds while the command runs, then removes the lease
torporctl release render
```

A hook needs no Python. From a shell script:

```bash
leases="$XDG_RUNTIME_DIR/torpor/leases"
mkdir -p "$leases"
echo "pid=$$" > "$leases/my-job.lease"    # live while this script runs
touch "$leases/heartbeat.lease"           # live for lease-ttl seconds; touch again to renew
```

## Busy processes

`[busy] processes` takes one regular expression per line. Each is searched for in every process's
full command line, arguments joined by spaces, any user's processes. A match makes the state
`busy` (`process:<pattern>`) and holds the idle inhibit.

## Entities

| Entity | |
|---|---|
| `switch.<pc>_keep_awake` | The switch, with `ends_at` |

## Limitations

- A display manager's own idle suspend at the login screen is out of reach: there is no desktop
  session to inhibit.
- Busy processes and leases hold the inhibit only while a desktop runs; at the login screen they
  still make the state `busy`, which your automations can use.
