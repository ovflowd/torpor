# wake: Wake-on-LAN

Home Assistant wakes the PC; the agent tells it how. The agent reports each network interface's MAC
address and whether Wake-on-LAN looks armed. Home Assistant's Wake button and `torpor.wake` action
send the magic packet to the MACs you picked, then wait for the agent to connect again.

## Requirements

Wake-on-LAN armed in the firmware and the OS. That is a manual step: [wake-on-lan.md](../wake-on-lan.md).
NetworkManager is optional; without it the status is `unknown`.

## What the agent reports

Every interface in `/sys/class/net` with a device behind it and a MAC address, so not `lo`, bridges,
Docker or VPN interfaces:

| Field | |
|---|---|
| `name`, `mac` | As the kernel has them |
| `kind` | `ethernet`, `wifi` or `other` |
| `wol` | `armed`, `off` or `unknown` (below) |
| `primary` | The interface with the route to Home Assistant; the default for the Wake button |

| `wol` | When |
|---|---|
| `armed` | NetworkManager's `802-3-ethernet.wake-on-lan` contains `magic` for the interface's active connection, and `device/power/wakeup` isn't `disabled` |
| `off` | `device/power/wakeup` reads `disabled` |
| `unknown` | Anything else. Reading the NIC's own setting needs `ethtool` as root, which the agent doesn't do |

`off` is only ever a positive read. The agent reads interfaces at start, after a resume, and every
10 minutes.

## What Home Assistant does

1. If the agent is connected, the answer is `already_awake` and nothing is sent.
2. Otherwise it sends the magic packet, three times 100 ms apart, to the broadcast address and port
   from the options (default `255.255.255.255`, port 9), once per MAC you picked.
3. It waits up to the timeout (default 120 s) for the agent to connect, and fires `wake_succeeded`
   or `wake_failed` on the power event entity.

The packet is a LAN broadcast: it must come from the PC's own network. Over a VPN it doesn't arrive.

## Settings

All in Home Assistant's options: the MACs, the broadcast address and port, and the timeout. On the
PC only `[modules] wake`.

## Entities

| Entity | |
|---|---|
| `button.<pc>_wake` | Available while the PC sleeps or is off, which is the point of it |

The `wol_off` repair appears when every MAC you picked reports `off`.

## Smart plugs

Torpor doesn't switch plugs. If your PC sits behind a smart plug, restore the plug, wait, then press
Wake, in an ordinary automation: [automations.md](../automations.md#wake-a-pc-behind-a-smart-plug).
