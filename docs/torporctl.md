# torporctl

`torporctl` installs, pairs, configures and checks the agent. It runs as you and never runs `sudo`;
where something needs root, it prints the command for you.

| Command | What it does |
|---|---|
| `install [--observe-only] [--no-start] [--no-hardening]` | Checks the dependencies, installs the agent under your home and starts its user service ([install](install.md#install)) |
| `uninstall [--purge]` | Stops and removes it; `--purge` also removes the config and the key |
| `update SOURCE [--force]` | Re-installs from a newer checkout or release `.tar.gz`, keeping config and key ([update](install.md#update)) |
| `pair [--url URL] [--code-stdin]` | Pairs with Home Assistant using the code it shows ([quick start](quick-start.md#4-pair)) |
| `config list`, `get KEY`, `set KEY VALUE`, `unset KEY` | Reads or edits `torpor.conf`, keeping its comments, and restarts the agent ([configuration](configuration.md)) |
| `status [--json]` | The agent, its connection, the state, modules, displays and leases |
| `logs [-f] [-n N]` | The agent's journal |
| `doctor [--json]` | Checks everything and prints the commands that fix what's missing; never runs them |
| `hold NAME [--for DURATION] [-- CMD …]` | Keeps the PC busy for a while, or while a command runs ([leases](modules/keep-awake.md#leases)) |
| `release NAME` | Ends a hold |
| `restart` | Restarts the agent |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Fine |
| 1 | Failed |
| 2 | Usage error |
| 3 | Not installed, not paired, or no such lease |
| 4 | `doctor` found a failure |
| 5 | Home Assistant rejected the code or key |
| 6 | Timeout, or Home Assistant unreachable |
| 130 | Ctrl-C |

`status` returns 0 only while connected.
