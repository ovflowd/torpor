# Security policy

## Reporting a vulnerability

Please report a vulnerability privately, through
[GitHub's private vulnerability reporting](https://github.com/ovflowd/torpor/security/advisories/new),
and not in a public issue. Include what you found, how to reproduce it and what it affects. You will
get an answer within a week.

If that page does not open, open an issue that only says you have a security problem to report, and
without any detail, so that a way to talk privately can be arranged.

## Scope

Torpor runs on your PC as you and lets Home Assistant lock, sleep, wake and dim it. In scope are
problems in this repository's code, for example a way to:

- get past the pairing or session handshake, or make either side accept a forged, replayed or
  reordered frame;
- learn the pairing key, or decrypt traffic, from what crosses the network;
- get the PC's own address banned by Home Assistant;
- make the agent run anything beyond the fixed commands in [docs/protocol.md](docs/protocol.md), or
  go beyond the `[allow]` and `[modules]` ceilings in `torpor.conf`;
- make the agent ignore other apps' sleep inhibitors, or call logind's `*-ignore-inhibit` actions;
- make `torporctl` run `sudo`, or write outside your home folder and `$XDG_RUNTIME_DIR`.

Problems in Home Assistant, logind, polkit, your desktop's power manager, ddcutil or the kernel
belong to those projects. Problems in the Home Assistant integration belong to
[ovflowd/ha-torpor](https://github.com/ovflowd/ha-torpor), unless they are in the shared
`protocol.py`, which lives here.

## What Torpor protects

[docs/security.md](docs/security.md) has the threat model: who holds what, what an attacker on your
network can and can't do, and what plain-HTTP pairing exposes.

## Supported versions

Only the latest commit on `main`.
