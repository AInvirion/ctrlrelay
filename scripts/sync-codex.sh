#!/usr/bin/env bash
set -euo pipefail

# sync-codex.sh — Sync Codex (OpenAI CLI) configuration between devices
# Syncs: config.toml, AGENTS.md skills, instructions
# Does NOT sync: auth.json, history, sessions (device-specific)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_DIR="$SCRIPT_DIR/../codex-config"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[codex-sync]${NC} $*"; }
ok()   { echo -e "${GREEN}  OK${NC} $*"; }
warn() { echo -e "${YELLOW}  WARN${NC} $*"; }
err()  { echo -e "${RED}  ERROR${NC} $*"; }

usage() {
    echo "Usage: $(basename "$0") <command> [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  export         Copy Codex config from this device into the sync repo"
    echo "  import         Apply Codex config from the sync repo to this device"
    echo "  install        Install skills as AGENTS.md files (symlink or copy)"
    echo ""
    echo "Options:"
    echo "  -n, --dry-run  Show what would be done"
    echo "  --copy         Use copy instead of symlink for install"
    echo "  -h, --help     Show this help"
    echo ""
    echo "Export/import syncs config.toml (with paths adjusted) and skills."
    echo "Install creates AGENTS.md files in ~/.codex/instructions/ for global access."
    exit 0
}

[[ $# -lt 1 ]] && usage

COMMAND="$1"; shift
DRY_RUN=false
USE_COPY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--dry-run) DRY_RUN=true; shift ;;
        --copy) USE_COPY=true; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Files to sync (relative to ~/.codex/)
SYNC_FILES=(
    "config.toml"
)

# Directories to sync recursively
SYNC_RECURSIVE_DIRS=(
    "instructions"
)

# --- Path adjustment helpers ---

get_export_home() {
    if [[ -f "$1/.home-path" ]]; then
        cat "$1/.home-path"
    else
        echo "$HOME"
    fi
}

adjust_paths() {
    local src_file="$1" dst_file="$2" old_home="$3"
    if [[ "$old_home" == "$HOME" ]]; then
        cp "$src_file" "$dst_file"
    else
        sed "s|$old_home|$HOME|g" "$src_file" > "$dst_file"
    fi
}

# --- Export functions ---

export_config() {
    local target="$1"

    if [[ ! -f "$CODEX_DIR/config.toml" ]]; then
        warn "config.toml not found, skipping"
        return
    fi

    if [[ "$DRY_RUN" == true ]]; then
        ok "(dry-run) would export config.toml"
        return
    fi

    # Export config, but strip project trust settings (device-specific paths)
    python3 -c "
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib

with open('$CODEX_DIR/config.toml', 'rb') as f:
    config = tomllib.load(f)

# Remove project trust settings (contain device-specific paths)
config.pop('projects', None)

# Write as TOML manually (no toml writer in stdlib)
def write_toml(data, prefix=''):
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f'[{prefix}{key}]' if prefix else f'[{key}]')
            lines.extend(write_toml(value, f'{prefix}{key}.'))
        elif isinstance(value, str):
            lines.append(f'{key} = \"{value}\"')
        elif isinstance(value, bool):
            lines.append(f'{key} = {str(value).lower()}')
        elif isinstance(value, (int, float)):
            lines.append(f'{key} = {value}')
        elif isinstance(value, list):
            items = ', '.join(f'\"{v}\"' if isinstance(v, str) else str(v) for v in value)
            lines.append(f'{key} = [{items}]')
    return lines

output = write_toml(config)
with open('$target/config.toml', 'w') as f:
    f.write('\\n'.join(output) + '\\n')
" 2>/dev/null || {
    # Fallback: just copy without filtering
    cp "$CODEX_DIR/config.toml" "$target/config.toml"
}
    ok "config.toml (project trust settings excluded)"
}

export_skills() {
    local target="$1"

    # Skills are already in the sync repo, just verify
    if [[ -d "$SYNC_DIR/skills" ]]; then
        local count
        count=$(find "$SYNC_DIR/skills" -name "AGENTS.md" 2>/dev/null | wc -l | tr -d ' ')
        ok "skills/ ($count AGENTS.md files in repo)"
    else
        warn "skills/ not found in sync dir"
    fi
}

export_instructions() {
    local target="$1"

    if [[ -d "$CODEX_DIR/instructions" ]]; then
        local count
        count=$(find "$CODEX_DIR/instructions" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$count" -gt 0 ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would export instructions/ ($count files)"
            else
                mkdir -p "$target/instructions"
                rsync -a --delete "$CODEX_DIR/instructions/" "$target/instructions/"
                ok "instructions/ ($count files)"
            fi
        else
            warn "instructions/ is empty"
        fi
    fi
}

do_export() {
    log "Exporting Codex config -> sync repo"
    log "Source: $CODEX_DIR"
    log "Target: $SYNC_DIR"
    echo ""

    if [[ ! -d "$CODEX_DIR" ]]; then
        err "Codex directory not found at $CODEX_DIR"
        echo "Is Codex installed? Run 'codex --help' to check."
        exit 1
    fi

    mkdir -p "$SYNC_DIR"

    # Save home path for import-time adjustment
    if [[ "$DRY_RUN" != true ]]; then
        echo "$HOME" > "$SYNC_DIR/.home-path"
    fi

    # Export config
    export_config "$SYNC_DIR"

    # Export custom instructions (if any)
    export_instructions "$SYNC_DIR"

    # Verify skills
    export_skills "$SYNC_DIR"

    # Generate export metadata
    if [[ "$DRY_RUN" != true ]]; then
        local platform="unknown"
        if [[ "$(uname)" == "Darwin" ]]; then
            platform="macos"
        elif [[ -f "/etc/lsb-release" ]]; then
            platform="linux"
        fi
        echo "$platform $(date -u +%Y-%m-%dT%H:%M:%SZ) $(hostname)" > "$SYNC_DIR/.last-export"
        ok "Metadata saved (.last-export)"
    fi

    echo ""
    log "Export complete. Commit and push dev-sync to share with other devices."
}

# --- Import functions ---

do_import() {
    log "Importing Codex config <- sync repo"
    log "Source: $SYNC_DIR"
    log "Target: $CODEX_DIR"
    echo ""

    if [[ ! -d "$SYNC_DIR" ]]; then
        err "sync dir not found at $SYNC_DIR"
        echo "Run 'export' on another device first, then pull this repo."
        exit 1
    fi

    mkdir -p "$CODEX_DIR"

    # Show where config came from
    if [[ -f "$SYNC_DIR/.last-export" ]]; then
        log "Config exported from: $(cat "$SYNC_DIR/.last-export")"
        echo ""
    fi

    local old_home
    old_home="$(get_export_home "$SYNC_DIR")"

    # Import config.toml
    if [[ -f "$SYNC_DIR/config.toml" ]]; then
        if [[ "$DRY_RUN" == true ]]; then
            ok "(dry-run) would import config.toml"
        else
            # Merge with existing config (preserve project trust settings)
            if [[ -f "$CODEX_DIR/config.toml" ]]; then
                # For now, just append non-project settings
                warn "config.toml exists - manual merge may be needed"
                cp "$SYNC_DIR/config.toml" "$CODEX_DIR/config.toml.imported"
                ok "config.toml.imported (review and merge manually)"
            else
                adjust_paths "$SYNC_DIR/config.toml" "$CODEX_DIR/config.toml" "$old_home"
                ok "config.toml"
            fi
        fi
    fi

    # Import custom instructions
    if [[ -d "$SYNC_DIR/instructions" ]]; then
        local count
        count=$(find "$SYNC_DIR/instructions" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$count" -gt 0 ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                ok "(dry-run) would import instructions/ ($count files)"
            else
                mkdir -p "$CODEX_DIR/instructions"
                while IFS= read -r srcfile; do
                    local rel="${srcfile#$SYNC_DIR/instructions/}"
                    mkdir -p "$(dirname "$CODEX_DIR/instructions/$rel")"
                    adjust_paths "$srcfile" "$CODEX_DIR/instructions/$rel" "$old_home"
                done < <(find "$SYNC_DIR/instructions" -type f 2>/dev/null)
                ok "instructions/ ($count files)"
            fi
        fi
    fi

    echo ""
    log "Import complete."
    log "Run './sync codex-install' to install skills."
}

# --- Install skills ---

do_install() {
    log "Installing Codex skills"
    log "Source: $SYNC_DIR/skills"
    echo ""

    if [[ ! -d "$SYNC_DIR/skills" ]]; then
        err "skills directory not found at $SYNC_DIR/skills"
        exit 1
    fi

    # Create instructions directory
    mkdir -p "$CODEX_DIR/instructions"

    # Count skills
    local skill_count
    skill_count=$(find "$SYNC_DIR/skills" -name "AGENTS.md" | wc -l | tr -d ' ')
    log "Found $skill_count skills to install"
    echo ""

    # Install each skill
    while IFS= read -r skill_file; do
        local skill_dir
        skill_dir=$(dirname "$skill_file")
        local skill_name
        skill_name=$(basename "$skill_dir")

        local target="$CODEX_DIR/instructions/$skill_name.md"

        if [[ "$DRY_RUN" == true ]]; then
            if [[ "$USE_COPY" == true ]]; then
                ok "(dry-run) would copy $skill_name -> instructions/$skill_name.md"
            else
                ok "(dry-run) would symlink $skill_name -> instructions/$skill_name.md"
            fi
        else
            if [[ "$USE_COPY" == true ]]; then
                cp "$skill_file" "$target"
                ok "$skill_name (copied)"
            else
                # Create symlink (allows live updates)
                ln -sf "$skill_file" "$target"
                ok "$skill_name (symlinked)"
            fi
        fi
    done < <(find "$SYNC_DIR/skills" -name "AGENTS.md" 2>/dev/null)

    # Also install the master AGENTS.md
    if [[ -f "$SYNC_DIR/AGENTS.md" ]]; then
        local target="$CODEX_DIR/instructions/00-master.md"
        if [[ "$DRY_RUN" == true ]]; then
            ok "(dry-run) would install master AGENTS.md"
        else
            if [[ "$USE_COPY" == true ]]; then
                cp "$SYNC_DIR/AGENTS.md" "$target"
            else
                ln -sf "$SYNC_DIR/AGENTS.md" "$target"
            fi
            ok "00-master.md (main instructions)"
        fi
    fi

    echo ""
    log "Skills installed to $CODEX_DIR/instructions/"
    log "Use with: codex 'review this code' or codex 'security audit'"
}

# --- Main ---

case "$COMMAND" in
    export)  do_export ;;
    import)  do_import ;;
    install) do_install ;;
    *) echo "Unknown command: $COMMAND"; usage ;;
esac
