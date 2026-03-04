# Shared Claude Code Config

This directory contains shareable Claude Code configuration for the team.

## Contents

- `skills/` — Custom Claude Code skills
- `rules/` — Custom Claude Code rules
- `plugins/` — Plugin manifest (install commands shown on import)
- `mcp-servers.json` — MCP server definitions (secrets redacted)

## How to import

```bash
# From the dev-sync repo root:
./sync team-import

# Dry run first to see what would change:
./sync team-import -n
```

## Notes

- MCP server entries with `<REDACTED>` env values will be skipped on import.
  Fill in the actual values in your `~/.claude.json` after importing.
- Servers that already exist in your config will NOT be overwritten.
- Plugins are not auto-installed. The import command will show you the
  `claude /install-plugin` commands to run manually.
- This does NOT touch your personal settings, keybindings, or memory.
