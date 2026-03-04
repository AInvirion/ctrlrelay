---
name: whats-next
description: >
  This skill should be used when the user asks "what's next", "what should I work on",
  "what's missing", "analyze backlog", "review roadmap", "prioritize work",
  "what needs to be done", "show me the gaps", "audit issues", "roadmap check",
  or wants a gap analysis between documentation, GitHub issues, milestones,
  and actual implementation status.
tools: Bash, Read, Grep, Glob
---

# What's Next — Gap Analysis & Priority Report

Inspects project documentation (CLAUDE.md, README, TODO, PLAN, CHANGELOG), GitHub issues, and milestones to identify what's missing, what's planned but not tracked, and what should be implemented next.

## Phase 1 — Read Project Documentation

Scan the current project for planning and status files:

```bash
# Find all relevant doc files
ls CLAUDE.md README.md TODO.md PLAN.md CHANGELOG.md ROADMAP.md \
   SESSION_STATUS.md NEXT_SESSION.md docs/*.md 2>/dev/null
```

Read each file and extract:
- **Planned features** — anything described as "planned", "TODO", "future", "next", or in a roadmap section
- **Known issues** — anything described as "known issue", "limitation", "bug", "broken"
- **Version/phase info** — current version, phase, milestone names mentioned
- **Acceptance criteria** — unchecked checkboxes `- [ ]` in any doc

Compile a list of **doc-mentioned items** — things the docs say should exist or need work.

## Phase 2 — Fetch GitHub Issues & Milestones

1. Get the repo:
   ```bash
   REMOTE_URL=$(git remote get-url origin 2>/dev/null)
   REPO=$(echo "$REMOTE_URL" | sed -E 's#(https://github\.com/|git@github\.com:)##' | sed 's/\.git$//')
   ```

2. Fetch all open issues with details:
   ```bash
   gh issue list --repo "$REPO" --state open --json number,title,labels,milestone,assignees,createdAt,updatedAt --limit 100
   ```

3. Fetch milestones with progress:
   ```bash
   gh api "repos/${REPO}/milestones?state=open&sort=due_on" --jq '.[] | {
     title: .title,
     due: .due_on,
     open: .open_issues,
     closed: .closed_issues,
     description: .description
   }'
   ```

4. Fetch recently closed issues (last 30 days) for momentum context:
   ```bash
   gh issue list --repo "$REPO" --state closed --json number,title,closedAt --limit 20
   ```

## Phase 3 — Cross-Reference & Gap Analysis

Compare documentation plans against GitHub issues to find:

### A. Gaps — Planned but Not Tracked
Items mentioned in docs (CLAUDE.md TODOs, PLAN.md features, ROADMAP items) that have **no corresponding GitHub issue**. These are untracked work items.

### B. Stale Issues — Tracked but Outdated
Open issues that:
- Haven't been updated in 30+ days
- Reference features/code that no longer exists
- Are duplicates of other issues

### C. Milestone Health
For each open milestone:
- Progress: X/Y issues closed (Z%)
- Overdue: is the due date past?
- Unassigned issues: issues in the milestone with no assignee
- Missing issues: things the milestone description mentions but aren't tracked

### D. Untracked Work
Look for:
- `TODO` and `FIXME` comments in source code:
  ```bash
  grep -rn "TODO\|FIXME\|HACK\|XXX" --include="*.py" --include="*.js" --include="*.ts" --include="*.tsx" . 2>/dev/null | grep -v node_modules | grep -v __pycache__ | head -30
  ```
- These may represent issues that should be tracked in GitHub.

## Phase 4 — Prioritize

Rank items by priority using these signals:
1. **Critical** — bugs, security issues, broken features
2. **High** — items in the current/overdue milestone, blockers for other work
3. **Medium** — planned features, enhancements, items in the next milestone
4. **Low** — nice-to-haves, tech debt, cosmetic issues

## Phase 5 — Report

```
## What's Next — <Project Name>

### Current State
- **Version**: <from CLAUDE.md or CHANGELOG>
- **Open issues**: X (Y in current milestone)
- **Milestones**: <list with progress %>

### Gaps (In Docs, Not in Issues)
| # | Item | Source | Suggested Priority |
|---|------|--------|--------------------|
| 1 | ...  | CLAUDE.md L42 | High |
| 2 | ...  | PLAN.md TODO | Medium |

### Milestone Status
#### <Milestone Name> — X/Y complete (Z%) [due: date]
- [ ] #<issue> — <title>
- [x] #<issue> — <title> (closed)
- Missing: <items mentioned in milestone desc but not tracked>

### Stale Issues (30+ days inactive)
| # | Issue | Title | Last Updated |
|---|-------|-------|--------------|
| 1 | #X    | ...   | YYYY-MM-DD   |

### Code TODOs Not in Issues
- `file.py:42` — TODO: <description>
- `app.js:15` — FIXME: <description>

### Recommended Next Steps
1. <highest priority item with reasoning>
2. <second priority>
3. <third priority>

### Quick Wins (< 1 hour)
- <small items that can be knocked out quickly>
```

## Options

- If the user says "check <org>" or "check all projects", run across multiple repos in that org folder.
- If the user says "just milestones", focus only on milestone health.
- If the user says "just gaps", only compare docs vs issues.
- If the user asks about a specific milestone, deep-dive into that one.

## Notes

- Never modify any files — this is a read-only analysis.
- If there are no GitHub issues or milestones, focus on documentation-driven analysis.
- If CLAUDE.md has a "roadmap" or "phases" section, treat it as the authoritative plan.
- Cross-reference CHANGELOG.md to understand what's already been delivered.
- Keep recommendations actionable — each "next step" should be something concrete.
