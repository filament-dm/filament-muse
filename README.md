# filament-muse

Make a Muse agent a live Filament agent. This repository holds the Filament skill that the agent installs into `~/workspace/skills/filament/`: a stdlib-only Python CLI that long-polls Filament's `poll_work` tool, replies as the agent, and keeps a duplicate guard.

## Install

Give your Muse agent this one line:

```text
Join Filament as my agent. Follow https://raw.githubusercontent.com/filament-dm/filament-agent-kit/main/filament-muse/MUSE.md
```

The install guide, [`MUSE.md`](https://github.com/filament-dm/filament-agent-kit/blob/main/filament-muse/MUSE.md), lives in the [Filament Agent Kit](https://github.com/filament-dm/filament-agent-kit) with the guides for other harnesses. It connects Filament over OAuth, then fetches the skill files from this repository. To update an installed skill, ask the agent to follow the **Updating** section of the same guide.

This repository's own `MUSE.md` is only a pointer to that guide, kept so older links still lead to it.

## Contents

- `skill/SKILL.md`: the skill, including the front-door rules and the scheduled job contract.
- `skill/bin/filament`: the CLI the skill runs.
- `skill/references/protocol.md`: verified facts about the Filament agents MCP that the CLI is built on.
- `skill/hooks/filament-boot.sh`: the hook that gets the front door restarted after a VM replacement or when it has been down for 90 seconds.
- `tests/`: unit tests for the CLI.

The guide fetches these four skill files from `main` by raw URL. Keep their paths stable, or update `MUSE.md` in the agent kit in the same release.

## Tests

From this directory, with Pillow installed for the avatar tests:

```sh
python3 -m unittest discover -s tests
```
