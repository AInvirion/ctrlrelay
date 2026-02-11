#!/usr/bin/env bash
set -euo pipefail

# sync-repos.sh — Clone missing repos and pull latest changes for existing ones
# Works on: macOS, ChromeOS (Linux), Termux (Android)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/../repos.manifest"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/Projects}"

# Colors (works in Termux too)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[sync]${NC} $*"; }
ok()   { echo -e "${GREEN}  OK${NC} $*"; }
warn() { echo -e "${YELLOW}  WARN${NC} $*"; }
err()  { echo -e "${RED}  ERR${NC} $*"; }

usage() {
    echo "Usage: $(basename "$0") [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -n, --dry-run     Show what would be done without doing it"
    echo "  -f, --filter STR  Only process repos matching STR (e.g. 'SEMCL')"
    echo "  -j, --jobs N      Parallel clone/pull jobs (default: 4)"
    echo "  -s, --status      Show status of all repos (dirty, ahead, behind)"
    echo "  -h, --help        Show this help"
    exit 0
}

DRY_RUN=false
FILTER=""
JOBS=4
STATUS_ONLY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--dry-run) DRY_RUN=true; shift ;;
        -f|--filter) FILTER="$2"; shift 2 ;;
        -j|--jobs) JOBS="$2"; shift 2 ;;
        -s|--status) STATUS_ONLY=true; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

if [[ ! -f "$MANIFEST" ]]; then
    err "Manifest not found: $MANIFEST"
    exit 1
fi

cloned=0
pulled=0
skipped=0
failed=0

sync_repo() {
    local path="$1" branch="$2" remote="$3"
    local full_path="$PROJECTS_DIR/$path"
    local org_dir
    org_dir="$(dirname "$full_path")"

    if [[ "$STATUS_ONLY" == true ]]; then
        if [[ -d "$full_path/.git" ]]; then
            local status_line
            local dirty="" ahead="" behind="" branch_info
            branch_info="$(git -C "$full_path" rev-parse --abbrev-ref HEAD 2>/dev/null)"

            if [[ -n "$(git -C "$full_path" status --porcelain 2>/dev/null)" ]]; then
                dirty=" ${YELLOW}[dirty]${NC}"
            fi

            local tracking
            tracking="$(git -C "$full_path" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)"
            if [[ -n "$tracking" ]]; then
                local ahead_n behind_n
                ahead_n="$(git -C "$full_path" rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo 0)"
                behind_n="$(git -C "$full_path" rev-list --count 'HEAD..@{upstream}' 2>/dev/null || echo 0)"
                [[ "$ahead_n" -gt 0 ]] && ahead=" ${GREEN}+${ahead_n}${NC}"
                [[ "$behind_n" -gt 0 ]] && behind=" ${RED}-${behind_n}${NC}"
            fi

            echo -e "  ${path}  (${BLUE}${branch_info}${NC})${dirty}${ahead}${behind}"
        else
            echo -e "  ${path}  ${RED}[not cloned]${NC}"
        fi
        return
    fi

    if [[ -d "$full_path/.git" ]]; then
        log "Pulling $path..."
        if [[ "$DRY_RUN" == true ]]; then
            ok "(dry-run) would pull $path"
            return
        fi
        if git -C "$full_path" fetch --quiet 2>/dev/null; then
            local local_branch
            local_branch="$(git -C "$full_path" rev-parse --abbrev-ref HEAD)"
            # Only pull if on a clean branch (no uncommitted changes)
            if [[ -z "$(git -C "$full_path" status --porcelain 2>/dev/null)" ]]; then
                if git -C "$full_path" pull --ff-only --quiet 2>/dev/null; then
                    ok "$path ($local_branch)"
                    ((pulled++)) || true
                else
                    warn "$path — pull failed (diverged?), skipping"
                    ((skipped++)) || true
                fi
            else
                warn "$path — has uncommitted changes, fetch only"
                ((skipped++)) || true
            fi
        else
            err "$path — fetch failed"
            ((failed++)) || true
        fi
    else
        log "Cloning $path..."
        if [[ "$DRY_RUN" == true ]]; then
            ok "(dry-run) would clone $remote -> $full_path"
            return
        fi
        mkdir -p "$org_dir"
        if git clone --quiet --branch "$branch" "$remote" "$full_path" 2>/dev/null; then
            ok "$path (cloned $branch)"
            ((cloned++)) || true
        else
            err "$path — clone failed"
            ((failed++)) || true
        fi
    fi
}

log "Manifest: $MANIFEST"
log "Projects: $PROJECTS_DIR"
[[ -n "$FILTER" ]] && log "Filter: $FILTER"
echo ""

while IFS='|' read -r path branch remote; do
    # Skip comments and empty lines
    [[ "$path" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$path" ]] && continue

    # Trim whitespace
    path="$(echo "$path" | xargs)"
    branch="$(echo "$branch" | xargs)"
    remote="$(echo "$remote" | xargs)"

    # Apply filter
    if [[ -n "$FILTER" ]] && [[ ! "$path" == *"$FILTER"* ]]; then
        continue
    fi

    sync_repo "$path" "$branch" "$remote"
done < "$MANIFEST"

if [[ "$STATUS_ONLY" != true && "$DRY_RUN" != true ]]; then
    echo ""
    log "Done: ${GREEN}${cloned} cloned${NC}, ${BLUE}${pulled} pulled${NC}, ${YELLOW}${skipped} skipped${NC}, ${RED}${failed} failed${NC}"
fi
