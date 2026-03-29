#!/usr/bin/env bash
# scan-repos.sh — Scan all git repos under ~/Projects/ and report status.
#
# Usage: scan-repos.sh [org-filter]
# Example: scan-repos.sh AINVIRION  (only scan AINVIRION/)

set -euo pipefail

BASE_DIR="$HOME/Projects"
FILTER="${1:-}"

DIRTY=()
AHEAD=()
BEHIND=()
CLEAN=0

# Find all git repos (max 3 levels deep to match ORG/REPO and ORG/SUBFOLDER/REPO)
while IFS= read -r gitdir; do
  REPO_DIR=$(dirname "$gitdir")
  REPO_NAME="${REPO_DIR#$BASE_DIR/}"

  # Apply org filter if specified
  if [[ -n "$FILTER" && "$REPO_NAME" != "$FILTER"/* ]]; then
    continue
  fi

  cd "$REPO_DIR"

  BRANCH=$(git branch --show-current 2>/dev/null || echo "detached")

  # Check dirty status
  DIRTY_FILES=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')

  # Check ahead/behind (silently fetch would be too slow, use cached)
  AHEAD_COUNT=0
  BEHIND_COUNT=0
  TRACKING=$(git rev-parse --abbrev-ref "@{upstream}" 2>/dev/null || echo "")
  if [[ -n "$TRACKING" ]]; then
    AHEAD_COUNT=$(git rev-list "@{upstream}..HEAD" --count 2>/dev/null || echo 0)
    BEHIND_COUNT=$(git rev-list "HEAD..@{upstream}" --count 2>/dev/null || echo 0)
  fi

  # Classify
  if [[ "$DIRTY_FILES" -gt 0 ]]; then
    DIRTY+=("$REPO_NAME|$BRANCH|${DIRTY_FILES} files|+${AHEAD_COUNT}|-${BEHIND_COUNT}")
  elif [[ "$AHEAD_COUNT" -gt 0 ]]; then
    AHEAD+=("$REPO_NAME|$BRANCH|clean|+${AHEAD_COUNT}|-${BEHIND_COUNT}")
  elif [[ "$BEHIND_COUNT" -gt 0 ]]; then
    BEHIND+=("$REPO_NAME|$BRANCH|clean|+${AHEAD_COUNT}|-${BEHIND_COUNT}")
  else
    CLEAN=$((CLEAN + 1))
  fi

done < <(find "$BASE_DIR" -maxdepth 4 -name ".git" -type d 2>/dev/null | sort)

# Output results
echo "=== DIRTY REPOS (uncommitted changes) ==="
if [[ ${#DIRTY[@]} -eq 0 ]]; then
  echo "None"
else
  printf "%-40s %-20s %-15s %-8s %-8s\n" "REPO" "BRANCH" "DIRTY" "AHEAD" "BEHIND"
  for entry in "${DIRTY[@]}"; do
    IFS='|' read -r repo branch dirty ahead behind <<< "$entry"
    printf "%-40s %-20s %-15s %-8s %-8s\n" "$repo" "$branch" "$dirty" "$ahead" "$behind"
  done
fi

echo ""
echo "=== AHEAD OF REMOTE (unpushed work) ==="
if [[ ${#AHEAD[@]} -eq 0 ]]; then
  echo "None"
else
  printf "%-40s %-20s %-15s %-8s %-8s\n" "REPO" "BRANCH" "DIRTY" "AHEAD" "BEHIND"
  for entry in "${AHEAD[@]}"; do
    IFS='|' read -r repo branch dirty ahead behind <<< "$entry"
    printf "%-40s %-20s %-15s %-8s %-8s\n" "$repo" "$branch" "$dirty" "$ahead" "$behind"
  done
fi

echo ""
echo "=== BEHIND REMOTE ==="
if [[ ${#BEHIND[@]} -eq 0 ]]; then
  echo "None"
else
  printf "%-40s %-20s %-15s %-8s %-8s\n" "REPO" "BRANCH" "DIRTY" "AHEAD" "BEHIND"
  for entry in "${BEHIND[@]}"; do
    IFS='|' read -r repo branch dirty ahead behind <<< "$entry"
    printf "%-40s %-20s %-15s %-8s %-8s\n" "$repo" "$branch" "$dirty" "$ahead" "$behind"
  done
fi

echo ""
echo "=== SUMMARY ==="
echo "Dirty: ${#DIRTY[@]} | Ahead: ${#AHEAD[@]} | Behind: ${#BEHIND[@]} | Clean: ${CLEAN}"
