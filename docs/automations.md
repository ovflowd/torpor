# Automations

Copy-paste recipes for Home Assistant. Torpor leaves smart plugs and power meters to ordinary
automations on purpose: every desk is wired differently, and Home Assistant already does this well.

The examples assume a PC named `desk`, so its entities are `sensor.desk_state`, `button.desk_wake`
and so on. Rename them to yours. Plug and sensor names are examples too. Paste a recipe into a new
automation or script in YAML mode (**⋮ → Edit in YAML**), or into `configuration.yaml` where it says
so.

## Wake a PC behind a smart plug

If the PC's power goes through a smart plug that might be off, switch it on first, give the power
supply and the network card a moment, then wake the PC.

```yaml
alias: Wake the desk PC
mode: single
sequence:
  - action: switch.turn_on
    target:
      entity_id: switch.desk_pc_plug
  - wait_template: "{{ is_state('switch.desk_pc_plug', 'on') }}"
    timeout: "00:00:30"
    continue_on_timeout: false
  - delay: "00:00:10"
  - action: button.press
    target:
      entity_id: button.desk_wake
```

Save it as a script and use it in place of the Wake button. How long to wait depends on the PC:
time how long its network port's link light takes to come on after the plug.

### Several plugs, in order

A desk strip that feeds the PC's own plug: restore them in order, waiting for each.

```yaml
alias: Wake the desk PC
mode: single
sequence:
  - repeat:
      for_each:
        - switch.desk_strip
        - switch.desk_pc_plug
      sequence:
        - action: switch.turn_on
          target:
            entity_id: "{{ repeat.item }}"
        - wait_template: "{{ is_state(repeat.item, 'on') }}"
          timeout: "00:00:30"
          continue_on_timeout: false
        - delay: "00:00:05"
  - delay: "00:00:10"
  - action: button.press
    target:
      entity_id: button.desk_wake
```

### Wait for the result

`torpor.wake` with `wait: true` returns `awake`, `already_awake` or `timeout`. Pick the PC in the
action's device field in the UI editor; it fills in `device_id`.

```yaml
  - action: torpor.wake
    data:
      device_id: 0123456789abcdef0123456789abcdef
      wait: true
    response_variable: woke
  - if:
      - condition: template
        value_template: "{{ woke.result == 'timeout' }}"
    then:
      - action: notify.notify
        data:
          message: The desk PC didn't wake up.
```

## Tell off from asleep with a power sensor

When the PC goes down cleanly, Torpor says whether it went to sleep, powered off or restarted. After
a power cut or a crash it can't, and the state becomes `unknown`. A plug with a power meter can tell.
Add to `configuration.yaml`:

```yaml
template:
  - sensor:
      - name: Desk PC power
        unique_id: desk_pc_power_state
        device_class: enum
        state: >
          {% set state = states('sensor.desk_state') %}
          {% set watts = states('sensor.desk_pc_plug_power') | float(-1) %}
          {% if state in ['in_use', 'remote', 'busy', 'idle', 'asleep', 'off', 'restarting'] %}
            {{ state }}
          {% elif watts < 0 %}
            unknown
          {% elif watts > 25 %}
            on
          {% elif watts > 1 %}
            asleep
          {% else %}
            off
          {% endif %}
```

Measure your own thresholds: watch the plug's power with the PC idle at the desktop, suspended and
off. A suspended desktop often draws 1 to 3 W, an idle one 30 W or more, and some draw a few watts
when off too; leave room between them.

## Sleep the PC when everyone has left

```yaml
alias: Sleep the desk PC when everyone has left
mode: single
triggers:
  - trigger: numeric_state
    entity_id: zone.home
    below: 1
    for: "00:10:00"
conditions:
  - condition: state
    entity_id: sensor.desk_state
    state: idle
actions:
  - action: button.press
    target:
      entity_id: button.desk_sleep
```

It only sleeps an `idle` PC: never one in use, used remotely, or busy with a lease. Sleep is refused
while another app blocks it, which shows in Home Assistant's log.

## Keep it awake while something runs

From Home Assistant, for example while a media player on the PC plays:

```yaml
alias: Keep the desk PC awake while it plays
mode: restart
triggers:
  - trigger: state
    entity_id: media_player.desk
conditions: []
actions:
  - if:
      - condition: state
        entity_id: media_player.desk
        state: playing
    then:
      - action: switch.turn_on
        target:
          entity_id: switch.desk_keep_awake
    else:
      - action: switch.turn_off
        target:
          entity_id: switch.desk_keep_awake
```

For a fixed time, `torpor.keep_awake` on the switch:

```yaml
  - action: torpor.keep_awake
    target:
      entity_id: switch.desk_keep_awake
    data:
      duration: "02:00:00"
```

From the PC itself, no Home Assistant needed: `torporctl hold render --for 2h`, or
`torporctl hold backup -- borg create ::archive`, or a [lease file](modules/keep-awake.md#leases).

## Tell me when a wake fails

```yaml
alias: Desk PC didn't wake
mode: single
triggers:
  - trigger: state
    entity_id: event.desk_power
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.desk_power
    attribute: event_type
    state: wake_failed
actions:
  - action: notify.notify
    data:
      message: The desk PC didn't come back after Wake. Is it plugged in, and is Wake-on-LAN armed?
```

## Voice assistants

New entities are exposed to Assist only if you expose them. Check **Settings → Voice assistants →
Expose** before you let a voice assistant press Sleep or Power off.
