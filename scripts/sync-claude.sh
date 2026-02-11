#!/usr/bin/env bash
set -euo pipefail

# sync-claude.sh — Sync Claude Code configuration between devices
# Syncs: settings, plugins list, project memory files
# Does NOT sync: history, todos, debug logs, session data (device-specific)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_DIR="$SCRIPT_DIR/../claude-config"
CLAUDE_DIR="${CLAUDE_HOME:-$HOME/.claude}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[claude-sync]${NC} $*"; }
ok()   { echo -e "${GREEN}  OK${NC} $*"; }
warn() { echo -e "${YELLOW}  WARN${NC} $*"; }

usage() {
    echo "Usage: $(basename "$0") <export|import> [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  export    Copy Claude config from this device into the sync repo"
    echo "  import    Apply Claude config from the sync repo to this device"
    echo ""
    echo "Options:"
    echo "  -n, --dry-run    Show what would be done"
    echo "  --no-plugins     Skip plugin reinstallation on import"
    echo "  -h, --help       Show this help"
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

# Files/dirs to sync (relative to ~/.claude/)
SYNC_FILES=(
    "settings.json"
    "settings.local.json"
)

SYNC_DIRS=(
    "plugins/installed_plugins.json"
)

do_export() {
    log "Exporting Claude config -> sync repo"
    log "Source: $CLAUDE_DIR"
    log "Target: $SYNC_DIR"
    echo ""

    mkdir -p "$SYNC_DIR"

    # Copy settings files
    for f in "${SYNC_FILES[@]}"; do
        if [[ -f "$CLAUDE_DIR/$f" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would copy $f"
            else
                cp "$CLAUDE_DIR/$f" "$SYNC_DIR/$f"
                ok "$f"
            fi
        else
            warn "$f not found, skipping"
        fi
    done

    # Copy plugin manifest
    for f in "${SYNC_DIRS[@]}"; do
        local dir
        dir="$(dirname "$SYNC_DIR/$f")"
        if [[ -f "$CLAUDE_DIR/$f" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would copy $f"
            else
                mkdir -p "$dir"
                cp "$CLAUDE_DIR/$f" "$SYNC_DIR/$f"
                ok "$f"
            fi
        fi
    done

    # Export project memory files
    if [[ -d "$CLAUDE_DIR/projects" ]]; then
        log "Exporting project memory files..."
        mkdir -p "$SYNC_DIR/memory"
        # Find all MEMORY.md and other .md files in project memory dirs
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

do_import() {
    log "Importing Claude config <- sync repo"
    log "Source: $SYNC_DIR"
    log "Target: $CLAUDE_DIR"
    echo ""

    if [[ ! -d "$SYNC_DIR" ]]; then
        echo -e "${RED}Error: sync dir not found at $SYNC_DIR${NC}"
        echo "Run 'export' on another device first, then pull this repo."
        exit 1
    fi

    mkdir -p "$CLAUDE_DIR"

    # Show where this config came from
    if [[ -f "$SYNC_DIR/.last-export" ]]; then
        log "Config exported from: $(cat "$SYNC_DIR/.last-export")"
        echo ""
    fi

    # Copy settings
    for f in "${SYNC_FILES[@]}"; do
        if [[ -f "$SYNC_DIR/$f" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would import $f"
            else
                # settings.local.json needs path adjustment on import
                if [[ "$f" == "settings.local.json" ]]; then
                    # Replace old home paths with current home
                    sed "s|/Users/ovalenzuela|$HOME|g" "$SYNC_DIR/$f" > "$CLAUDE_DIR/$f"
                    ok "$f (paths adjusted for $HOME)"
                else
                    cp "$SYNC_DIR/$f" "$CLAUDE_DIR/$f"
                    ok "$f"
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
                # Adjust paths in installed_plugins.json
                sed "s|/Users/ovalenzuela|$HOME|g" "$SYNC_DIR/$f" > "$CLAUDE_DIR/$f"
                ok "$f (paths adjusted)"
            fi
        fi
    done

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
                # Adjust paths in memory files too
                sed "s|/Users/ovalenzuela|$HOME|g" "$memfile" > "$CLAUDE_DIR/projects/$rel"
                ok "memory: $rel"
            fi
        done < <(find "$SYNC_DIR/memory" -name "*.md" -type f 2>/dev/null)
    fi

    # Reinstall plugins if Claude Code is available
    if [[ "$NO_PLUGINS" != true ]] && command -v claude &>/dev/null; then
        log "Reinstalling plugins..."
        warn "Plugins need manual reinstall. Run:"
        echo "  claude /install-plugin planning-with-files@planning-with-files"
        echo "  claude /install-plugin frontend-design@claude-plugins-official"
    fi

    echo ""
    log "Import complete. Restart Claude Code to apply changes."
}

case "$COMMAND" in
    export) do_export ;;
    import) do_import ;;
    *) echo "Unknown command: $COMMAND"; usage ;;
esac
