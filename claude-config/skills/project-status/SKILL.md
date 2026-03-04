---
name: project-status
description: >
  This skill should be used when the user asks to "check project status",
  "what's dirty", "check all repos", "project status", "repo status",
  "what needs attention", "show me the state of things", "portfolio status",
  or wants a health check across all their projects and deployments.
tools: Bash, Read, Grep, Glob
---

# Project Status — Portfolio Health Check

Scans all repos under `~/Projects/` and reports on git status, open PRs, and DigitalOcean deployment health.

## Phase 1 — Scan Repositories

Run the scanner script:

```bash
bash ~/.claude/skills/project-status/scripts/scan-repos.sh
```

The script walks all git repos under `~/Projects/` and outputs a JSON summary for each.

## Phase 2 — Check Open PRs

For repos that have a GitHub remote, check for open PRs:

```bash
# For each repo with changes or that the user is actively working on
gh pr list --repo <owner/repo> --state open --json number,title,headRefName,updatedAt --limit 5 2>/dev/null
```

Only check repos that are dirty, ahead, or have recent commits (last 7 days) to avoid API rate limits.

## Phase 3 — Check Deployments (Optional)

If `doctl` is available, check DigitalOcean app deployments:

```bash
doctl apps list --output json 2>/dev/null | python3 -c "
import json, sys
apps = json.load(sys.stdin)
for app in apps:
    name = app.get('spec', {}).get('name', 'unknown')
    phase = app.get('last_deployment_active_at', 'N/A')
    active = app.get('active_deployment', {})
    deploy_phase = active.get('phase', 'UNKNOWN') if active else 'NO_DEPLOYMENT'
    print(f'{name}: {deploy_phase}')
"
```

## Phase 4 — Report

Present results grouped by status:

```
## Project Status Report

### Repos Needing Attention
<repos that are dirty, have unpushed commits, or failed deployments>

| Repo | Branch | Dirty | Ahead | Behind | Open PRs |
|------|--------|-------|-------|--------|----------|
| ...  | ...    | ...   | ...   | ...    | ...      |

### Deployments
| App | Status | Last Deploy |
|-----|--------|-------------|
| ... | ...    | ...         |

### Clean Repos
<count> repos are clean and up to date.

### Summary
- X repos need attention
- Y open PRs across all repos
- Z apps deployed successfully
```

## Options

- If the user says "check <org>" (e.g., "check AINVIRION"), only scan that org folder.
- If the user says "just git" or "just repos", skip the deployment check.
- If the user says "just deploys" or "just deployments", skip git status and only check DO apps.
- If the user asks about a specific project, give detailed status for just that one.

## Notes

- Keep output concise — don't list every clean repo individually, just count them.
- Highlight repos that are ahead of remote (unpushed work) as these risk being lost.
- If a repo is dirty with changes older than 7 days, flag it as "stale uncommitted work".
