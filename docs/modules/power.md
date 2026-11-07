# power: lock, sleep, hibernate, power off, restart

Home Assistant's Lock, Sleep, Hibernate, Power off and Restart buttons, and the `torpor.sleep`
action, all through systemd-logind.

## Requirements

systemd-logind. For acting while nobody is logged in, a polkit rule ([below](#with-nobody-logged-in)).

## Settings

On the PC, `[modules] power` and the `[allow]` ceiling:

| Action | `[allow]` default | logind call |
|---|---|---|
| `lock` | yes | `Session.Lock()` on each of your graphical sessions |
| `suspend` | yes | `Manager.Suspend(false)` |
| `hibernate` | **no** | `Manager.Hibernate(false)` |
| `poweroff` | **no** | `Manager.PowerOff(false)` |
| `reboot` | **no** | `Manager.Reboot(false)` |

```bash
torporctl config set allow.poweroff yes
```

## Refusals

| Answer | When |
|---|---|
| `not_allowed` | `[allow]` doesn't permit it |
| `unavailable` | Lock: none of your graphical sessions exists. Hibernate: logind's `CanHibernate` isn't `yes` right now. Any: logind says `no` or `na` |
| `refused` | Other apps hold a `block` inhibitor on sleep (or shutdown); the refusal names them, as in "1 app blocks sleep: Example Player, Playing video". Or another user's unlocked session is at the desk. Or polkit said no |
| `busy` | Another power command is still running |
| `failed` | logind returned another error; the message says which |

`force` in `torpor.sleep` skips only Torpor's own check for other apps' blockers and for another
user at the desk. logind still decides: Torpor never asks it to ignore inhibitors, and the polkit
rule below never allows the `*-ignore-inhibit` actions.

## Hibernate

The Hibernate button exists in Home Assistant when `[allow] hibernate = yes`. It is available only
while logind says `CanHibernate=yes`, which needs a swap large enough and, with Secure Boot,
a kernel that allows it. That answer can change with free swap; the button follows it without
disappearing.

## With nobody logged in

With an active desktop session, polkit lets you suspend without a password. With no active session
(the PC at the login screen, the agent running through linger), it asks for an admin password, which
nobody can type, and the command comes back `refused`. A polkit rule fixes that for the actions you
allowed. `torporctl doctor` prints it, built from your `[allow]`:

```js
// /etc/polkit-1/rules.d/50-torpor.rules: lets alex suspend with no active session
polkit.addRule(function (action, subject) {
    var allowed = ["org.freedesktop.login1.suspend", "org.freedesktop.login1.suspend-multiple-sessions"];
    if (subject.user == "alex" && allowed.indexOf(action.id) >= 0) {
        return polkit.Result.YES;
    }
});
```

Install it as root (`sudo tee /etc/polkit-1/rules.d/50-torpor.rules`). Torpor never installs it for
you. The `-multiple-sessions` variants let the action go ahead while another user is logged in too;
Torpor itself still refuses that unless you pass `force`.

## Entities

| Entity | On by default | |
|---|---|---|
| `button.<pc>_lock` | yes | |
| `button.<pc>_sleep` | yes | |
| `button.<pc>_hibernate` | yes | Only with `[allow] hibernate = yes` |
| `button.<pc>_power_off` | no | Only with `[allow] poweroff = yes` |
| `button.<pc>_restart` | no | Only with `[allow] reboot = yes` |
| `sensor.<pc>_sleep_blockers` | no | How many apps block sleep, and who |

## Limitations

- Lock works on sessions whose desktop implements logind's lock signal (Plasma, GNOME, most
  wlroots lockers through a helper).
- A shutdown on systemd older than 254 reports `unknown`, which Home Assistant shows as `off`.
