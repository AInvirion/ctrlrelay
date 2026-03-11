#!/usr/bin/env bash
set -euo pipefail

# sync-claude.sh — Sync Claude Code configuration between devices
# Syncs: settings, plugins, memory, skills, rules, keybindings, MCP servers
# Team mode: shares skills, rules, plugins, and MCP server definitions (secrets redacted)
# Does NOT sync: history, todos, debug logs, session data (device-specific)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_DIR="$SCRIPT_DIR/../claude-config"
CLAUDE_DIR="${CLAUDE_HOME:-$HOME/.claude}"
CLAUDE_JSON="$HOME/.claude.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[claude-sync]${NC} $*"; }
ok()   { echo -e "${GREEN}  OK${NC} $*"; }
warn() { echo -e "${YELLOW}  WARN${NC} $*"; }
err()  { echo -e "${RED}  ERROR${NC} $*"; }

usage() {
    echo "Usage: $(basename "$0") <command> [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  export         Copy Claude config from this device into the sync repo"
    echo "  import         Apply Claude config from the sync repo to this device"
    echo "  team-export    Export shareable config (skills, rules, plugins, MCP) to team/ dir"
    echo "  team-import    Import shared team config (skills, rules, plugins, MCP only)"
    echo ""
    echo "Options:"
    echo "  -n, --dry-run    Show what would be done"
    echo "  --no-plugins     Skip plugin reinstallation on import"
    echo "  -h, --help       Show this help"
    echo ""
    echo "Personal export/import syncs everything: settings, plugins, memory,"
    echo "skills, rules, keybindings, and MCP servers."
    echo ""
    echo "Team export/import only syncs shareable items: skills, rules, plugins,"
    echo "and MCP server definitions (with secrets redacted)."
    exit 0
}

[[ $# -lt 1 ]] && usage

COMMAND="$1"; shift
DRY_RUN=false
NO_PLUGINS=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--dry-run) DRY_RUN=true; shift ;;
        --no-plugins) NO_PLUGINS=true; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Files to sync (relative to ~/.claude/)
SYNC_FILES=(
    "settings.json"
    "settings.local.json"
    "keybindings.json"
)

SYNC_DIRS=(
    "plugins/installed_plugins.json"
)

# Directories to sync recursively (relative to ~/.claude/)
SYNC_RECURSIVE_DIRS=(
    "skills"
    "rules"
)

# --- Path adjustment helpers ---

# Read the saved home path from export, falling back to current $HOME
get_export_home() {
    if [[ -f "$1/.home-path" ]]; then
        cat "$1/.home-path"
    else
        echo "$HOME"
    fi
}

# Sed replace: old home -> current home (no-op if same)
adjust_paths() {
    local src_file="$1" dst_file="$2" old_home="$3"
    if [[ "$old_home" == "$HOME" ]]; then
        cp "$src_file" "$dst_file"
    else
        sed "s|$old_home|$HOME|g" "$src_file" > "$dst_file"
    fi
}

# --- MCP server export/import (python3) ---

export_mcp_servers() {
    local target="$1"
    local redact="${2:-false}"  # true for team export

    if [[ ! -f "$CLAUDE_JSON" ]]; then
        warn "~/.claude.json not found, skipping MCP servers"
        return
    fi

    if [[ "$DRY_RUN" == true ]]; then
        ok "(dry-run) would export MCP servers -> $(basename "$target")"
        return
    fi

    python3 -c "
import json, re, sys

with open('$CLAUDE_JSON') as f:
    data = json.load(f)

servers = data.get('mcpServers', {})
if not servers:
    print('  No MCP servers found', file=sys.stderr)
    sys.exit(0)

redact = $( [[ "$redact" == "true" ]] && echo "True" || echo "False" )

if redact:
    secret_pattern = re.compile(r'.*(KEY|TOKEN|SECRET|PASS).*', re.IGNORECASE)
    for name, cfg in servers.items():
        env = cfg.get('env', {})
        for k, v in env.items():
            if secret_pattern.match(k):
                env[k] = '<REDACTED>'

with open('$target', 'w') as f:
    json.dump(servers, f, indent=2)
    f.write('\n')
"
    ok "mcp-servers.json ($(python3 -c "import json; print(len(json.load(open('$target'))))" 2>/dev/null || echo '?') servers)"
}

import_mcp_servers() {
    local source="$1"

    if [[ ! -f "$source" ]]; then
        return
    fi

    if [[ "$DRY_RUN" == true ]]; then
        ok "(dry-run) would import MCP servers from $(basename "$source")"
        return
    fi

    python3 -c "
import json, sys

with open('$source') as f:
    new_servers = json.load(f)

# Read existing ~/.claude.json or start fresh
try:
    with open('$CLAUDE_JSON') as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}

existing = data.get('mcpServers', {})
added = 0
skipped_redacted = 0
skipped_existing = 0

for name, cfg in new_servers.items():
    if name in existing:
        skipped_existing += 1
        continue
    # Check for redacted env values
    env = cfg.get('env', {})
    has_redacted = any(v == '<REDACTED>' for v in env.values())
    if has_redacted:
        skipped_redacted += 1
        print(f'  WARN  {name}: has <REDACTED> env values, skipping (fill them in manually)', file=sys.stderr)
        continue
    existing[name] = cfg
    added += 1

data['mcpServers'] = existing
with open('$CLAUDE_JSON', 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')

print(f'  OK  MCP servers: {added} added, {skipped_existing} already present, {skipped_redacted} skipped (redacted)')
"
}

# --- Export functions ---

export_single_files() {
    local target_dir="$1"
    shift
    local files=("$@")

    for f in "${files[@]}"; do
        if [[ -f "$CLAUDE_DIR/$f" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would copy $f"
            else
                mkdir -p "$(dirname "$target_dir/$f")"
                cp "$CLAUDE_DIR/$f" "$target_dir/$f"
                ok "$f"
            fi
        else
            warn "$f not found, skipping"
        fi
    done
}

export_recursive_dirs() {
    local target_dir="$1"
    shift
    local dirs=("$@")

    for d in "${dirs[@]}"; do
        if [[ -d "$CLAUDE_DIR/$d" ]]; then
            local count
            count=$(find "$CLAUDE_DIR/$d" -type f 2>/dev/null | wc -l | tr -d ' ')
            if [[ "$count" -eq 0 ]]; then
                warn "$d/ is empty, skipping"
                continue
            fi
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would copy $d/ ($count files)"
            else
                mkdir -p "$target_dir/$d"
                rsync -a --delete "$CLAUDE_DIR/$d/" "$target_dir/$d/"
                ok "$d/ ($count files)"
            fi
        else
            warn "$d/ not found, skipping"
        fi
    done
}

do_export() {
    log "Exporting Claude config -> sync repo"
    log "Source: $CLAUDE_DIR"
    log "Target: $SYNC_DIR"
    echo ""

    mkdir -p "$SYNC_DIR"

    # Save home path for import-time path adjustment
    if [[ "$DRY_RUN" != true ]]; then
        echo "$HOME" > "$SYNC_DIR/.home-path"
    fi

    # Copy settings + keybindings
    export_single_files "$SYNC_DIR" "${SYNC_FILES[@]}"

    # Copy plugin manifest
    export_single_files "$SYNC_DIR" "${SYNC_DIRS[@]}"

    # Copy skills and rules directories
    export_recursive_dirs "$SYNC_DIR" "${SYNC_RECURSIVE_DIRS[@]}"

    # Export MCP servers (no redaction for personal export)
    export_mcp_servers "$SYNC_DIR/mcp-servers.json" false

    # Export project memory files
    if [[ -d "$CLAUDE_DIR/projects" ]]; then
        log "Exporting project memory files..."
        mkdir -p "$SYNC_DIR/memory"
        while IFS= read -r memfile; do
            local rel="${memfile#$CLAUDE_DIR/projects/}"
            local target_dir
            target_dir="$(dirname "$SYNC_DIR/memory/$rel")"
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would copy memory: $rel"
            else
                mkdir -p "$target_dir"
                cp "$memfile" "$SYNC_DIR/memory/$rel"
                ok "memory: $rel"
            fi
        done < <(find "$CLAUDE_DIR/projects" -path "*/memory/*.md" -type f 2>/dev/null)
    fi

    # Generate platform info
    if [[ "$DRY_RUN" != true ]]; then
        local platform="unknown"
        if [[ -d "/data/data/com.termux" ]]; then
            platform="termux"
        elif [[ "$(uname)" == "Darwin" ]]; then
            platform="macos"
        elif [[ -f "/etc/lsb-release" ]] && grep -q "Chrome" /etc/lsb-release 2>/dev/null; then
            platform="chromeos"
        else
            platform="linux"
        fi
        echo "$platform $(date -u +%Y-%m-%dT%H:%M:%SZ) $(hostname)" > "$SYNC_DIR/.last-export"
        ok "Metadata saved (.last-export)"
    fi

    echo ""
    log "Export complete. Commit and push dev-sync to share with other devices."
}

# --- Import functions ---

import_recursive_dirs() {
    local source_dir="$1" old_home="$2"
    shift 2
    local dirs=("$@")

    for d in "${dirs[@]}"; do
        if [[ -d "$source_dir/$d" ]]; then
            local count
            count=$(find "$source_dir/$d" -type f 2>/dev/null | wc -l | tr -d ' ')
            if [[ "$count" -eq 0 ]]; then
                continue
            fi
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would import $d/ ($count files)"
            else
                mkdir -p "$CLAUDE_DIR/$d"
                # Copy files with path adjustment
                while IFS= read -r srcfile; do
                    local rel="${srcfile#$source_dir/$d/}"
                    mkdir -p "$(dirname "$CLAUDE_DIR/$d/$rel")"
                    adjust_paths "$srcfile" "$CLAUDE_DIR/$d/$rel" "$old_home"
                done < <(find "$source_dir/$d" -type f 2>/dev/null)
                ok "$d/ ($count files, paths adjusted)"
            fi
        fi
    done
}

do_import() {
    log "Importing Claude config <- sync repo"
    log "Source: $SYNC_DIR"
    log "Target: $CLAUDE_DIR"
    echo ""

    if [[ ! -d "$SYNC_DIR" ]]; then
        err "sync dir not found at $SYNC_DIR"
        echo "Run 'export' on another device first, then pull this repo."
        exit 1
    fi

    mkdir -p "$CLAUDE_DIR"

    # Show where this config came from
    if [[ -f "$SYNC_DIR/.last-export" ]]; then
        log "Config exported from: $(cat "$SYNC_DIR/.last-export")"
        echo ""
    fi

    local old_home
    old_home="$(get_export_home "$SYNC_DIR")"

    # Copy settings + keybindings
    for f in "${SYNC_FILES[@]}"; do
        if [[ -f "$SYNC_DIR/$f" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would import $f"
            else
                if [[ "$f" == "keybindings.json" ]]; then
                    # Keybindings have no paths to adjust
                    cp "$SYNC_DIR/$f" "$CLAUDE_DIR/$f"
                    ok "$f"
                else
                    adjust_paths "$SYNC_DIR/$f" "$CLAUDE_DIR/$f" "$old_home"
                    if [[ "$old_home" != "$HOME" ]]; then
                        ok "$f (paths adjusted for $HOME)"
                    else
                        ok "$f"
                    fi
                fi
            fi
        fi
    done

    # Copy plugin manifest
    for f in "${SYNC_DIRS[@]}"; do
        if [[ -f "$SYNC_DIR/$f" ]]; then
            local dir
            dir="$(dirname "$CLAUDE_DIR/$f")"
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would import $f"
            else
                mkdir -p "$dir"
                adjust_paths "$SYNC_DIR/$f" "$CLAUDE_DIR/$f" "$old_home"
                ok "$f (paths adjusted)"
            fi
        fi
    done

    # Import skills and rules
    import_recursive_dirs "$SYNC_DIR" "$old_home" "${SYNC_RECURSIVE_DIRS[@]}"

    # Import MCP servers
    import_mcp_servers "$SYNC_DIR/mcp-servers.json"

    # Import memory files
    if [[ -d "$SYNC_DIR/memory" ]]; then
        log "Importing project memory files..."
        while IFS= read -r memfile; do
            local rel="${memfile#$SYNC_DIR/memory/}"
            local target_dir
            target_dir="$(dirname "$CLAUDE_DIR/projects/$rel")"
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would import memory: $rel"
            else
                mkdir -p "$target_dir"
                adjust_paths "$memfile" "$CLAUDE_DIR/projects/$rel" "$old_home"
                ok "memory: $rel"
            fi
        done < <(find "$SYNC_DIR/memory" -name "*.md" -type f 2>/dev/null)
    fi

    # Suggest plugin reinstallation
    if [[ "$NO_PLUGINS" != true ]]; then
        local plugin_manifest="$CLAUDE_DIR/plugins/installed_plugins.json"
        if [[ -f "$plugin_manifest" ]]; then
            local plugin_list
            plugin_list=$(python3 -c "
import json, sys
with open('$plugin_manifest') as f:
    data = json.load(f)
for name in data.get('plugins', {}):
    print(name)
" 2>/dev/null)
            if [[ -n "$plugin_list" ]]; then
                log "Plugins need manual reinstall. Run:"
                while IFS= read -r plugin; do
                    echo "  claude /install-plugin $plugin"
                done <<< "$plugin_list"
            fi
        fi
    fi

    echo ""

    # Install MCP servers from mcp-servers/
    install_mcp_servers

    log "Import complete. Restart Claude Code to apply changes."
}

# --- MCP Server Installation ---

install_mcp_servers() {
    local mcp_dir="$SCRIPT_DIR/../mcp-servers"

    if [[ ! -d "$mcp_dir" ]]; then
        return
    fi

    log "Installing MCP servers..."

    for server_dir in "$mcp_dir"/*/; do
        if [[ -f "$server_dir/install.sh" ]]; then
            local server_name
            server_name=$(basename "$server_dir")
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would install MCP server: $server_name"
            else
                log "Installing $server_name..."
                bash "$server_dir/install.sh" 2>&1 | sed 's/^/  /'
            fi
        fi
    done
}

# --- Team export/import ---

do_team_export() {
    local team_dir="$SYNC_DIR/team"

    log "Exporting shareable team config"
    log "Source: $CLAUDE_DIR"
    log "Target: $team_dir"
    echo ""

    mkdir -p "$team_dir"

    # Export skills and rules
    export_recursive_dirs "$team_dir" "${SYNC_RECURSIVE_DIRS[@]}"

    # Export plugin manifest (with paths sanitized)
    if [[ -f "$CLAUDE_DIR/plugins/installed_plugins.json" ]]; then
        if [[ "$DRY_RUN" == true ]]; then
            ok "(dry-run) would copy plugins/installed_plugins.json (paths sanitized)"
        else
            mkdir -p "$team_dir/plugins"
            sed "s|$HOME|~|g" "$CLAUDE_DIR/plugins/installed_plugins.json" > "$team_dir/plugins/installed_plugins.json"
            ok "plugins/installed_plugins.json"
        fi
    fi

    # Export MCP servers (with secrets redacted)
    export_mcp_servers "$team_dir/mcp-servers.json" true

    # Generate README
    if [[ "$DRY_RUN" != true ]]; then
        cat > "$team_dir/README.md" << 'README'
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
README
        ok "README.md generated"
    fi

    echo ""
    log "Team export complete. Commit and push to share with teammates."
}

do_team_import() {
    local team_dir="$SYNC_DIR/team"

    log "Importing shared team config"
    log "Source: $team_dir"
    log "Target: $CLAUDE_DIR"
    echo ""

    if [[ ! -d "$team_dir" ]]; then
        err "team config dir not found at $team_dir"
        echo "Run 'team-export' first, then pull this repo."
        exit 1
    fi

    mkdir -p "$CLAUDE_DIR"

    local old_home
    old_home="$(get_export_home "$SYNC_DIR")"

    # Import skills and rules only
    import_recursive_dirs "$team_dir" "$old_home" "${SYNC_RECURSIVE_DIRS[@]}"

    # Import MCP servers (merge, skip existing + redacted)
    import_mcp_servers "$team_dir/mcp-servers.json"

    # Show plugin install commands from team manifest
    local team_plugin_manifest="$team_dir/plugins/installed_plugins.json"
    if [[ -f "$team_plugin_manifest" ]]; then
        local plugin_list
        plugin_list=$(python3 -c "
import json, sys
with open('$team_plugin_manifest') as f:
    data = json.load(f)
for name in data.get('plugins', {}):
    print(name)
" 2>/dev/null)
        if [[ -n "$plugin_list" ]]; then
            echo ""
            log "Team plugins available. Install with:"
            while IFS= read -r plugin; do
                echo "  claude /install-plugin $plugin"
            done <<< "$plugin_list"
        fi
    fi

    echo ""
    log "Team import complete. Restart Claude Code to apply changes."
}

# --- Main ---

case "$COMMAND" in
    export)      do_export ;;
    import)      do_import ;;
    team-export) do_team_export ;;
    team-import) do_team_import ;;
    *) echo "Unknown command: $COMMAND"; usage ;;
esac
