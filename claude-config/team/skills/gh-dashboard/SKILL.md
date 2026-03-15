---
name: gh-dashboard
description: >
  This skill should be used when the user asks to "check github", "gh dashboard",
  "review my repos", "what needs attention", "security alerts", "open prs",
  "check PRs", "github status", "show me PRs", "any security issues", or wants
  a unified view of PRs, security alerts, and assigned issues across all
  managed repositories from repos.manifest.
tools: Bash, Read
---

# GitHub Dashboard — Unified Repository Report

Queries all repos from `repos.manifest` and presents a dashboard showing open PRs, security alerts, and assigned issues.

## Phase 1 — Locate Manifest

Find the repos.manifest file:

```bash
MANIFEST="$HOME/Projects/dev-sync/repos.manifest"
if [[ ! -f "$MANIFEST" ]]; then
    echo "Error: repos.manifest not found at $MANIFEST"
    exit 1
fi
wc -l < "$MANIFEST" | xargs echo "Repos to check:"
```

## Phase 2 — Fetch Dashboard Data

Run the fetch script to query all repos in parallel:

```bash
bash ~/.claude/skills/gh-dashboard/scripts/fetch-dashboard.sh "$HOME/Projects/dev-sync/repos.manifest"
```

The script outputs progress to stderr and JSON data to stdout between markers.

## Phase 3 — Format Report

Parse the JSON output and format as markdown tables.

### PRs Table Format

```
| Repo | PR | Title | Author | Age |
|------|------|-------|--------|-----|
```

For each PR:
- Repo: `owner/repo` (link-friendly)
- PR: `#number`
- Title: First 50 chars
- Author: `@login`
- Age: Human-readable (e.g., "2d", "1w")

### Security Alerts Table Format

```
| Repo | Type | Severity | Package/Rule | Advisory |
|------|------|----------|--------------|----------|
```

Sort by severity: CRITICAL > HIGH > MEDIUM > LOW

### Issues Table Format

```
| Repo | Issue | Title | Labels | Age |
|------|-------|-------|--------|-----|
```

## Phase 4 — Present Dashboard

Output the final dashboard:

```markdown
## GitHub Dashboard

---

### Open Pull Requests (N)

| Repo | PR | Title | Author | Age |
|------|------|-------|--------|-----|
| ... |

### Security Alerts (N)

| Repo | Type | Severity | Package/Rule | Advisory |
|------|------|----------|--------------|----------|
| ... |

### Assigned Issues (N)

| Repo | Issue | Title | Labels | Age |
|------|-------|-------|--------|-----|
| ... |

---

### Summary
- N open PRs across M repos
- N security alerts (X CRITICAL, Y HIGH, Z MEDIUM)
- N issues assigned to you
```

## Options

- If the user says "just PRs" or "only PRs", skip security and issues sections.
- If the user says "just security" or "security only", skip PRs and issues.
- If the user says "just issues" or "my issues", skip PRs and security.
- If the user specifies an org (e.g., "check SemClone"), filter to repos matching that org.

## Error Handling

- **403/404 on security APIs**: Some repos may not have security features enabled. Skip silently and don't show errors for missing alerts.
- **Rate limits**: The script batches requests. If you hit rate limits, wait and retry.
- **Empty results**: If a section has 0 items, show "None" instead of an empty table.

## Notes

- PRs are sorted newest first
- Security alerts are sorted by severity (most critical first)
- Issues are sorted by creation date (newest first)
- The script uses `gh` CLI which must be authenticated
