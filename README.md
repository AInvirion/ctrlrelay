# dev-sync

Multi-device development environment sync toolkit. Clone, pull, and keep all your repositories and Claude Code configuration in sync across macOS, ChromeOS, Termux (Android), and Linux.

## Quick Start

```bash
# First time on a new device
./sync setup

# Day-to-day usage
./sync repos            # Clone missing repos, pull existing ones
./sync status           # Check what's dirty/ahead/behind
```

## Commands

| Command | Description |
|---------|-------------|
| `./sync repos` | Clone missing repos and pull latest changes |
| `./sync status` | Show status of all repos (dirty, ahead/behind) |
| `./sync export` | Export Claude Code config to this repo |
| `./sync import` | Import Claude Code config to this device |
| `./sync setup` | Full device bootstrap (first time on new device) |
| `./sync manifest` | Re-scan ~/Projects and update repos.manifest |

## Options for `./sync repos`

```
-n, --dry-run     Show what would be done without doing it
-f, --filter STR  Only process repos matching STR (e.g. 'SEMCL')
-j, --jobs N      Parallel jobs (default: 4)
-s, --status      Show status of all repos
```

## How It Works

- **repos.manifest** — Lists all repositories with their org folder, branch, and git remote. Edit directly or regenerate with `./sync manifest`.
- **scripts/sync-repos.sh** — Clones missing repos and fast-forward pulls existing ones. Skips repos with uncommitted changes (fetch only).
- **scripts/sync-claude.sh** — Exports/imports Claude Code settings, plugins, and project memory files. Adjusts home directory paths automatically on import.
- **scripts/setup-device.sh** — Detects the platform, installs prerequisites, syncs repos, and imports Claude config in one step.
- **claude-config/** — Synced Claude Code configuration (settings, plugin manifest, project memory).

## Supported Platforms

- **macOS** — Assumes dev tools are installed
- **ChromeOS** — Linux container with auto nvm/Node.js setup
- **Termux (Android)** — Installs packages via `pkg`
- **Linux** — Assumes git and Node.js are available

## Adding New Repos

Either edit `repos.manifest` directly:

```
ORG/repo-name | main | git@github.com:Org/repo-name.git
```

Or clone the repo into `~/Projects/ORG/repo-name` and run `./sync manifest` to auto-detect it.
