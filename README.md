# dev-sync

Multi-device development environment sync. Keeps repos, Claude Code config, Codex config, and team-shared settings in sync across machines.

## Quick start

```bash
# First time on a new device:
./sync setup

# Sync all repos:
./sync repos

# Export your Claude Code config:
./sync export

# Import Claude Code config on another device:
./sync import
```

## Commands

| Command | Description |
|---|---|
| `./sync repos` | Clone missing repos, pull existing ones |
| `./sync repos -f SEMCL` | Sync only repos matching a filter |
| `./sync repos -n` | Dry run (show what would happen) |
| `./sync status` | Show status of all repos (dirty, ahead/behind) |
| `./sync export` | Export Claude Code config to this repo |
| `./sync export -n` | Dry run export |
| `./sync import` | Import Claude Code config to this device |
| `./sync import -n` | Dry run import |
| `./sync import --no-plugins` | Import without plugin reinstall hints |
| `./sync team-export` | Export shareable team config |
| `./sync team-import` | Import shared team config |
| `./sync setup` | Full device bootstrap (first time) |
| `./sync manifest` | Re-scan ~/Projects and update repos.manifest |
| `./sync codex-export` | Export Codex config to this repo |
| `./sync codex-import` | Import Codex config to this device |
| `./sync codex-install` | Install Codex skills (AGENTS.md files) |
| `./sync codex-install --copy` | Install skills as copies instead of symlinks |

## Claude Code config sync

### Personal sync (`export` / `import`)

Syncs everything between your own devices:

- **Settings** — `settings.json`, `settings.local.json`
- **Keybindings** — `keybindings.json`
- **Plugins** — `plugins/installed_plugins.json` (manifest only; plugins need manual reinstall)
- **Skills** — `skills/` directory
- **Rules** — `rules/` directory
- **MCP servers** — extracted from `~/.claude.json` (no secret redaction)
- **Project memory** — all `MEMORY.md` files from project dirs

Paths are automatically adjusted between devices (e.g. different home directories).

### Team sync (`team-export` / `team-import`)

Shares config that's useful for teammates without touching personal settings:

- **Skills** — custom Claude Code skills
- **Rules** — custom Claude Code rules
- **Plugins** — plugin manifest (import shows `claude /install-plugin` commands)
- **MCP servers** — server definitions with secrets redacted (`<REDACTED>`)

Does NOT touch: settings, keybindings, memory, or any personal config.

```bash
# Export team config (run once, commit + push):
./sync team-export

# Teammates import after pulling:
./sync team-import
```

**MCP server notes:**
- Servers already in your config are skipped (no clobbering)
- Servers with `<REDACTED>` env values are skipped; fill them in manually in `~/.claude.json`

## Codex config sync

Syncs OpenAI Codex CLI configuration and skills between devices.

### Skills

Codex skills are AGENTS.md instruction files for specialized code review tasks:

| Skill | Purpose |
|-------|---------|
| `code-review` | General code review (correctness, readability, maintainability) |
| `security-review` | Security analysis (STRIDE, injection, auth, data exposure) |
| `duplicate-code` | Find copy-paste code and suggest DRY refactoring |
| `dead-code` | Detect unused functions, unreachable code, orphan files |
| `vid-verification` | Risk scoring and verification checklists |

### Installation

```bash
# Install skills to ~/.codex/instructions/ (symlinked):
./sync codex-install

# Or use copies instead of symlinks:
./sync codex-install --copy
```

### Usage with Codex

```bash
cd ~/Projects/some-project
codex "review this code"
codex "security audit src/"
codex "find dead code"
codex "check for duplicates"
codex "VID check this change"
```

### Syncing to other devices

```bash
# On this machine:
./sync codex-export
git add -A && git commit -m "sync codex config" && git push

# On other machines:
git pull
./sync codex-import
./sync codex-install
```

## Typical workflow

```bash
# On your main machine — export and push:
./sync export
cd dev-sync && git add -A && git commit -m "sync config" && git push

# On another machine — pull and import:
cd dev-sync && git pull
./sync import

# Share config with team:
./sync team-export
git add -A && git commit -m "update team config" && git push
```

## Directory structure

```
dev-sync/
├── sync                    # Entry point script
├── repos.manifest          # List of repos to sync
├── scripts/
│   ├── sync-claude.sh      # Claude Code config sync
│   ├── sync-codex.sh       # Codex config sync
│   ├── sync-repos.sh       # Repo cloning/pulling
│   ├── setup-device.sh     # First-time device setup
│   └── update-manifest.sh  # Manifest regeneration
├── claude-config/          # Claude Code config (git-tracked)
│   ├── .home-path          # Source home path for path adjustment
│   ├── .last-export        # Export metadata
│   ├── settings.json
│   ├── settings.local.json
│   ├── keybindings.json
│   ├── mcp-servers.json
│   ├── plugins/
│   ├── skills/
│   ├── rules/
│   ├── memory/
│   └── team/               # Shareable subset
│       ├── README.md
│       ├── plugins/
│       ├── skills/
│       ├── rules/
│       └── mcp-servers.json
└── codex-config/           # Codex config (git-tracked)
    ├── AGENTS.md           # Master instructions
    ├── config.toml         # Codex settings (exported)
    └── skills/             # Review skills (AGENTS.md format)
        ├── code-review/
        ├── security-review/
        ├── duplicate-code/
        ├── dead-code/
        └── vid-verification/
```
