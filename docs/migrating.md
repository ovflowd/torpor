# Migrating

How to move from your own scripts, or from another PC agent, to Torpor without a day of broken
automations. This page is guidance: nothing in it runs by itself, and Torpor changes nothing in
Home Assistant except its own entities.

The order that works: run Torpor beside the old setup in observe-only mode, compare, hand over one
module at a time, then switch every consumer over in one sitting.

## 1. Know what Torpor does, and doesn't

| Torpor does | Torpor doesn't, in this version |
|---|---|
| State: `in_use`, `remote`, `busy`, `idle`, plus `asleep`, `off`, `restarting` from the goodbye | Presence: it never looks at who is home; the state comes from the PC alone |
| Lock, suspend, hibernate, power off, restart | Power-profile changes, auto-suspend at the login screen |
| Keep awake, leases, busy processes | Remote desktop on demand |
| Wake-on-LAN packet and wait | Smart plugs (they stay in your automations: [automations.md](automations.md)) |
| Brightness per display, automatic from a lux sensor, per monitor mode, with a boost while a game runs | Late-night dimming |

Whatever your old setup does from the right column, keep it running for that job.

## 2. Map the old values to the new ones

Write down every value your old state entity takes and what it meant, and map each one. A typical
map:

| Old | New | Notes |
|---|---|---|
| "active", "at the desk" | `in_use` | Torpor's `in_use` needs no presence: an unlocked session in front and a lit display |
| "remote" (SSH, remote desktop) | `remote` | |
| "remote" because a job runs | `busy` | Torpor separates "used from elsewhere" from "working on its own" |
| "idle" | `idle` | |
| "suspended" | `asleep` | |
| "off" | `off` | |
| "idle" after a reboot | `restarting` | Torpor says so, then the real state once the agent is back |

Any automation that tests the old value against a list ("not off and not suspended") needs the new
list.

## 3. Run Torpor in observe-only beside the old setup

```bash
./torporctl install --observe-only
torporctl pair --url http://homeassistant.local:8123
```

The agent reports everything and executes nothing: every command gets `observe_only`, it takes no
idle inhibit and writes no brightness. It still takes its own logind delay inhibitor and sends the
going-down goodbye, so you can compare that too. Two delay inhibitors are fine.

Compare the two states for a few days. A history graph with both entities is enough. Where they
differ, decide which one is right; Torpor's rules are in [core](modules/core.md).

A temporary helper automation can mirror Torpor's state into the old entity's values, so you can
compare like for like, or feed old automations while you move them:

```yaml
alias: Mirror Torpor's state (temporary)
mode: queued
triggers:
  - trigger: state
    entity_id: sensor.desk_state
actions:
  - variables:
      map:
        in_use: active
        remote: remote
        busy: remote
        idle: idle
        asleep: suspended
        off: "off"
        restarting: idle
  - condition: template
    value_template: "{{ trigger.to_state.state in map }}"
  - action: input_select.select_option
    target:
      entity_id: input_select.desk_state_compare
    data:
      option: "{{ map[trigger.to_state.state] }}"
```

## 4. Hand over one module at a time

When the states agree, turn observe-only off and let Torpor act, one job at a time. On the PC,
`[modules]` turns each module on or off; in Home Assistant, the options do the same per feature.

```bash
torporctl config set agent.observe-only no
torporctl config set modules.brightness no     # keep the old tool on brightness for now
```

Two tools must never drive the same thing: two brightness writers fight, two keep-awake switches
confuse everyone. Turn the job off in the old tool before you turn it on in Torpor.

### Leases and hooks

If your old setup kept the PC busy through its own files or flags, have the hooks write both formats
during the move. A Torpor lease is a file; no Python is needed:

```bash
leases="$XDG_RUNTIME_DIR/torpor/leases"
mkdir -p "$leases"
echo "pid=$$" > "$leases/my-job.lease"
```

Remove the old format once the old tool is gone.

## 5. Cut over

In one sitting:

1. Search every automation, script, scene and dashboard for the old entities. In Home Assistant,
   each entity's **Related** tab lists what uses it; a search of the YAML (`.storage` and your YAML
   files) finds the rest, templates included.
2. Change each one to Torpor's entity and Torpor's values (step 2).
3. Check voice assistant exposure: **Settings → Voice assistants → Expose**. New entities aren't
   exposed unless you expose them, and old ones you removed may have been.
4. Stop the old tool's jobs Torpor now does, then remove what's left of it, including any Home
   Assistant token it held: **Profile → Security → Long-lived access tokens → Delete**.

## Worked example: angelthesis-agent

Torpor grew out of a single-PC agent, `angelthesis-agent`, a lingering user service that wrote to
helper entities over Home Assistant's WebSocket API with a long-lived token. It is the reference for
everything above.

### What maps

| angelthesis-agent | Torpor |
|---|---|
| `input_select.angelthesis_state`: `active` | `sensor.<pc>_state`: `in_use` |
| `remote` from an SSH session or a remote-desktop client | `remote` (`ssh`, `port:3389` with `[core] remote-ports = 3389`) |
| `remote` while Claude Code works | `busy`, through a lease the hooks write |
| `idle` | `idle` |
| `suspended` | `asleep` |
| `off` | `off` |
| `idle` after a reboot | `restarting` |
| `input_boolean.angelthesis_keep_awake` and its expiry timer | `switch.<pc>_keep_awake`; the expiry is an option |
| `input_button.angelthesis_sleep` | `button.<pc>_sleep` |
| `script.angelthesis_wake` | The plug recipe in [automations.md](automations.md) with `button.<pc>_wake` or `torpor.wake` |
| The plug-power fallback automation | The power-sensor template in [automations.md](automations.md) |
| Monitor brightness through `org.kde.ScreenBrightness` | `number.<pc>_<display>_brightness` and the auto switch, `kde` backend |
| The Power Setting read (`ddcutil getvcp 0xE1`) and its two anchors | A mode code `E1` on the monitor, `00` and `01` with their anchors, and `sensor.<pc>_<display>_mode` |
| The amd-game boost (`GAME_BOOST`) | The game boost option, with `[games] performance-hold = game-performance`, and `binary_sensor.<pc>_game_running` |

Copy the old agent's curve into the options at cutover: read `ANCHOR_LUX`, the anchor percentages,
`LUX_SLOPE`, `BRIGHTNESS_FLOOR` and `BRIGHTNESS_CEILING` from `angelthesis-agent.py` then, since they
have changed over time. The old agent keeps two anchors, one per monitor Power Setting (Standard
and Power Saving): in Torpor that is the monitor's mode code `E1`, with `00` at
`ANCHOR_PCT_STANDARD` and `01` at `ANCHOR_PCT_POWER_SAVING`, and the ceiling at `BRIGHTNESS_CEILING`.
`GAME_BOOST` is the game boost. Both agents reading `0xE1` at once is safe, since they take the same
bus lock, but only one of them should write the brightness.

### What stays with angelthesis-agent for now

Remote desktop on demand (krdp, the session helper, the takeover guard), the login-screen blanking
and auto-suspend, power-profile holds, the presence-gated desk state and late-night dimming. Torpor
doesn't do these yet. Its state also ignores presence on purpose: an unlocked,
lit desk is `in_use` whoever is home.

### Differences that matter

- **Presence.** The old `active` also needed the home's presence sensor to be on. An automation that
  relied on that needs its own presence condition.
- **Busy versus remote.** Automations that kept the PC up while "remote" should accept `busy` too.
- **Brightness has one writer.** Both use Plasma's service, so each would see the other's writes as
  hand changes. The old agent has no switch for its brightness alone; adding one, or retiring the
  agent, is a change to that agent and your decision. Until then keep `[modules] brightness = no`, or
  leave Torpor's auto switches off and use its sliders only.
- **Keep awake has one switch.** Move the dashboard button and the automations that turn it on in
  the same sitting.
- **Leases.** The old agent reads its own lease folder; Torpor reads `$XDG_RUNTIME_DIR/torpor/leases/`.
  The Claude Code hooks can write both while both run.
- **The token.** Once the old agent is gone, delete its long-lived token in Home Assistant.

### Suggested order

1. Install Torpor in observe-only and pair. Compare `input_select.angelthesis_state` with
   `sensor.<pc>_state` through the mirror automation, with the value map above.
2. Turn observe-only off with only `power` and `wake` on. Move the Sleep and Wake buttons and the
   wake script.
3. Turn `keep_awake` on, move the keep-awake switch and its automations, and add the Torpor lease to
   the hooks.
4. Once the old agent no longer sets brightness, turn Torpor's `brightness` on with the old curve.
5. Search every consumer of the old entities and switch them over; check exposure; keep the old
   agent for remote desktop until Torpor does it.
