#!/usr/bin/env bash
# fetch-dashboard.sh — Fetch PRs, security alerts, and assigned issues across all repos
#
# Usage: fetch-dashboard.sh <manifest-path>
# Output: JSON objects to stdout, progress to stderr
#
# Compatible with bash 3.2+ (macOS default)

set -euo pipefail

MANIFEST="${1:-$HOME/Projects/dev-sync/repos.manifest}"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

# Parse manifest and extract owner/repo pairs (one per line to stdout)
parse_manifest() {
    while IFS='|' read -r folder branch remote || [[ -n "$folder" ]]; do
        # Skip comments and empty lines
        [[ "$folder" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$folder" ]] && continue

        # Trim whitespace
        remote=$(echo "$remote" | xargs)

        # Extract owner/repo from git@github.com:Owner/Repo.git
        if [[ "$remote" =~ git@github\.com:([^/]+)/([^.]+)\.git ]]; then
            echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
        fi
    done < "$MANIFEST"
}

# Fetch open PRs for a repo
fetch_prs() {
    local repo="$1"
    local outfile="$TEMP_DIR/pr_${repo//\//_}.json"

    gh pr list --repo "$repo" --state open \
        --json number,title,author,headRefName,createdAt,updatedAt \
        --limit 100 2>/dev/null | \
    jq --arg repo "$repo" '[.[] | {repo: $repo, number, title, author: .author.login, branch: .headRefName, created: .createdAt}]' \
        > "$outfile" 2>/dev/null || echo "[]" > "$outfile"
}

# Fetch assigned issues for a repo
fetch_issues() {
    local repo="$1"
    local outfile="$TEMP_DIR/issue_${repo//\//_}.json"

    gh issue list --repo "$repo" --assignee @me --state open \
        --json number,title,labels,createdAt \
        --limit 50 2>/dev/null | \
    jq --arg repo "$repo" '[.[] | {repo: $repo, number, title, labels: [.labels[].name], created: .createdAt}]' \
        > "$outfile" 2>/dev/null || echo "[]" > "$outfile"
}

# Fetch Dependabot alerts for a repo
fetch_dependabot() {
    local repo="$1"
    local outfile="$TEMP_DIR/dependabot_${repo//\//_}.json"

    gh api "repos/$repo/dependabot/alerts" --jq '[.[] | select(.state == "open") | {
        repo: "'"$repo"'",
        type: "dependabot",
        severity: .security_advisory.severity,
        package: .security_vulnerability.package.name,
        advisory: .security_advisory.cve_id,
        summary: .security_advisory.summary
    }]' 2>/dev/null > "$outfile" || echo "[]" > "$outfile"
}

# Fetch code scanning alerts for a repo
fetch_code_scanning() {
    local repo="$1"
    local outfile="$TEMP_DIR/codescan_${repo//\//_}.json"

    gh api "repos/$repo/code-scanning/alerts" --jq '[.[] | select(.state == "open") | {
        repo: "'"$repo"'",
        type: "code-scanning",
        severity: .rule.severity,
        rule: .rule.id,
        description: .rule.description,
        path: .most_recent_instance.location.path
    }]' 2>/dev/null > "$outfile" || echo "[]" > "$outfile"
}

# Fetch secret scanning alerts for a repo (requires appropriate permissions)
fetch_secret_scanning() {
    local repo="$1"
    local outfile="$TEMP_DIR/secret_${repo//\//_}.json"

    gh api "repos/$repo/secret-scanning/alerts" --jq '[.[] | select(.state == "open") | {
        repo: "'"$repo"'",
        type: "secret-scanning",
        severity: "CRITICAL",
        secret_type: .secret_type_display_name,
        created: .created_at
    }]' 2>/dev/null > "$outfile" || echo "[]" > "$outfile"
}

# Main execution
main() {
    echo "Parsing repos.manifest..." >&2

    # Read repos into array (bash 3.2 compatible)
    REPOS=()
    while IFS= read -r repo; do
        REPOS+=("$repo")
    done < <(parse_manifest)

    local total=${#REPOS[@]}
    echo "Found $total repositories" >&2

    # Launch all fetches in parallel (with batching to avoid rate limits)
    local batch_size=10
    local batch_count=0
    local pr_count=0
    local security_count=0
    local issue_count=0

    echo "Fetching PRs..." >&2
    for repo in "${REPOS[@]}"; do
        fetch_prs "$repo" &
        batch_count=$((batch_count + 1))

        # Wait for batch to complete before starting next
        if (( batch_count >= batch_size )); then
            wait
            batch_count=0
        fi
    done
    wait

    # Count PRs
    for f in "$TEMP_DIR"/pr_*.json; do
        [[ -f "$f" ]] && pr_count=$((pr_count + $(jq 'length' "$f" 2>/dev/null || echo 0)))
    done
    echo "Fetching PRs... done ($pr_count found)" >&2

    echo "Fetching security alerts..." >&2
    batch_count=0
    for repo in "${REPOS[@]}"; do
        fetch_dependabot "$repo" &
        fetch_code_scanning "$repo" &
        fetch_secret_scanning "$repo" &
        batch_count=$((batch_count + 3))

        if (( batch_count >= batch_size * 3 )); then
            wait
            batch_count=0
        fi
    done
    wait

    # Count security alerts
    for f in "$TEMP_DIR"/dependabot_*.json "$TEMP_DIR"/codescan_*.json "$TEMP_DIR"/secret_*.json; do
        [[ -f "$f" ]] && security_count=$((security_count + $(jq 'length' "$f" 2>/dev/null || echo 0)))
    done
    echo "Fetching security alerts... done ($security_count found)" >&2

    echo "Fetching assigned issues..." >&2
    batch_count=0
    for repo in "${REPOS[@]}"; do
        fetch_issues "$repo" &
        batch_count=$((batch_count + 1))

        if (( batch_count >= batch_size )); then
            wait
            batch_count=0
        fi
    done
    wait

    # Count issues
    for f in "$TEMP_DIR"/issue_*.json; do
        [[ -f "$f" ]] && issue_count=$((issue_count + $(jq 'length' "$f" 2>/dev/null || echo 0)))
    done
    echo "Fetching assigned issues... done ($issue_count found)" >&2

    # Combine all results into single JSON output
    echo "---JSON_OUTPUT_START---"

    # Combine PRs
    echo '{"prs":'
    jq -s 'add | sort_by(.created) | reverse' "$TEMP_DIR"/pr_*.json 2>/dev/null || echo "[]"

    # Combine security alerts
    echo ',"security":'
    jq -s 'add | sort_by(.severity) | reverse' \
        "$TEMP_DIR"/dependabot_*.json \
        "$TEMP_DIR"/codescan_*.json \
        "$TEMP_DIR"/secret_*.json 2>/dev/null || echo "[]"

    # Combine issues
    echo ',"issues":'
    jq -s 'add | sort_by(.created) | reverse' "$TEMP_DIR"/issue_*.json 2>/dev/null || echo "[]"

    echo '}'
    echo "---JSON_OUTPUT_END---"
}

main
