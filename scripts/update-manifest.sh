#!/usr/bin/env bash
set -euo pipefail

# update-manifest.sh — Scan ~/Projects and regenerate repos.manifest
# Run this after adding new repos to keep the manifest in sync

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/../repos.manifest"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/Projects}"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${BLUE}[manifest]${NC} $*"; }
ok()  { echo -e "${GREEN}  OK${NC} $*"; }

log "Scanning $PROJECTS_DIR for git repos..."

# Build new manifest
TMPFILE="$(mktemp)"

cat > "$TMPFILE" <<'HEADER'
# Dev Sync - Repository Manifest
# Format: FOLDER/REPO | BRANCH | GIT_REMOTE
# Lines starting with # are ignored
# To skip a repo, comment it out
# Auto-generated — edit freely, re-run update-manifest.sh to refresh

HEADER

count=0
while IFS= read -r gitdir; do
    repo_path="$(dirname "$gitdir")"
    rel_path="${repo_path#$PROJECTS_DIR/}"

    # Skip dev-sync itself
    [[ "$rel_path" == "dev-sync" ]] && continue

    remote="$(git -C "$repo_path" remote get-url origin 2>/dev/null || echo "NO_REMOTE")"
    default_branch="$(git -C "$repo_path" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main")"

    # Use the default branch from remote, fallback to main
    [[ -z "$default_branch" ]] && default_branch="main"

    echo "$rel_path | $default_branch | $remote" >> "$TMPFILE"
    ((count++)) || true
done < <(find "$PROJECTS_DIR" -maxdepth 3 -name ".git" -type d 2>/dev/null | sort)

# Show diff if manifest exists
if [[ -f "$MANIFEST" ]]; then
    added="$(comm -13 <(grep -v '^#' "$MANIFEST" | awk -F'|' '{print $1}' | xargs -I{} echo {} | sort) <(grep -v '^#' "$TMPFILE" | awk -F'|' '{print $1}' | xargs -I{} echo {} | sort) 2>/dev/null || true)"
    removed="$(comm -23 <(grep -v '^#' "$MANIFEST" | awk -F'|' '{print $1}' | xargs -I{} echo {} | sort) <(grep -v '^#' "$TMPFILE" | awk -F'|' '{print $1}' | xargs -I{} echo {} | sort) 2>/dev/null || true)"

    if [[ -n "$added" ]]; then
        echo -e "${GREEN}New repos:${NC}"
        echo "$added" | while read -r line; do [[ -n "$line" ]] && echo "  + $line"; done
    fi
    if [[ -n "$removed" ]]; then
        echo -e "${YELLOW}Removed repos:${NC}"
        echo "$removed" | while read -r line; do [[ -n "$line" ]] && echo "  - $line"; done
    fi
fi

mv "$TMPFILE" "$MANIFEST"
ok "Manifest updated: $count repos in $MANIFEST"
