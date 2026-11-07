# Wake-on-LAN

Torpor's Wake button needs the PC's network card to listen for a magic packet while the PC sleeps or
is off. That is set in two places, the firmware and the OS, and Torpor sets neither: arming it needs
root and a reboot into the firmware setup. `torporctl doctor` tells you whether it looks armed.

Wake-on-LAN works over Ethernet. Wi-Fi cards that support it are rare, and it seldom works from off.

## 1. The firmware (BIOS or UEFI)

For waking from off (and from hibernate), the card needs standby power after shutdown:

- Turn on the option named "Wake on LAN", "Resume by PCI-E device", "Power On By PCI-E" or similar.
- Turn **off** "ErP", "EuP", "Deep Sleep" or "Deep Power Off": those cut standby power to save a
  watt or two.

Waking from suspend usually needs no firmware change.

Check after a shutdown: the link LEDs on the network port usually stay lit while the card has power.

## 2. The OS

The card forgets Wake-on-LAN at many boots unless something sets it each time. Use the tool that
manages your network.

### NetworkManager

Most desktops. Find the connection, set `magic`, and bring it up again:

```bash
nmcli -g GENERAL.CONNECTION device show enp5s0     # the connection's name, e.g. "Wired connection 1"
sudo nmcli connection modify "Wired connection 1" 802-3-ethernet.wake-on-lan magic
sudo nmcli connection up "Wired connection 1"
```

NetworkManager then writes it to the card every time the connection comes up. The default value,
`default`, leaves the card alone, which usually means off.

### systemd-networkd

A `.link` file, for example `/etc/systemd/network/50-wol.link`:

```ini
[Match]
MACAddress=02:00:5e:10:00:01

[Link]
NamePolicy=kernel database onboard slot path
MACAddressPolicy=persistent
WakeOnLan=magic
```

Reboot, or run `sudo udevadm trigger --action=add /sys/class/net/enp5s0`.

### ethtool and a udev rule

Anywhere else, a udev rule that runs `ethtool` when the card appears, for example
`/etc/udev/rules.d/81-wol.rules`:

```text
ACTION=="add", SUBSYSTEM=="net", NAME=="enp5s0", RUN+="/usr/bin/ethtool -s $name wol g"
```

## 3. Check

```bash
sudo ethtool enp5s0 | grep Wake-on      # "Wake-on: g" means magic packet
cat /sys/class/net/enp5s0/device/power/wakeup   # "enabled" lets it wake the PC from suspend
torporctl doctor                        # "Wake-on-LAN armed on enp5s0"
```

Then suspend the PC and press Wake in Home Assistant. If that works, try it from off.

## What Torpor reports

| Status | Means |
|---|---|
| `armed` | NetworkManager sets `magic`, and the card may wake the PC |
| `off` | The card's `power/wakeup` is `disabled`: it can't wake the PC |
| `unknown` | Torpor can't tell without root, for example with systemd-networkd or a udev rule. It may well work |

## Why it rarely survives a power cut

After the PC loses power completely (a power cut, the plug switched off, the PSU switch), most
cards come back unarmed: the setting lived in the card, and only the OS sets it again, at the next
boot. So a PC that was off when the power went can't be woken until someone presses its power
button. Some firmware has an "AC power loss: last state" or "power on" option that helps.

A smart plug in front of the PC is the same: switch the plug on, and the card has power but may not
listen. If your PC sits behind a plug, restore the plug and wait before you press Wake, and test that
it works from your setup ([automations.md](automations.md#wake-a-pc-behind-a-smart-plug)).
