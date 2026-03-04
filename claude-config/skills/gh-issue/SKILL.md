---
name: gh-issue
description: >
  This skill should be used when the user asks to "create an issue",
  "add an issue", "open an issue", "file a bug", "add a feature request",
  "track this", "log this issue", "gh issue", "add to backlog",
  or wants to create a GitHub issue with proper labels, milestones,
  and project assignment.
tools: Bash, Read, Grep, Glob
---

# GitHub Issue — Create & Organize

Creates a GitHub issue with labels, milestone assignment, and project board placement. Ensures issues are properly categorized and tracked.

## Phase 1 — Gather Context

1. Get the current repo:
   ```bash
   REMOTE_URL=$(git remote get-url origin 2>/dev/null)
   REPO=$(echo "$REMOTE_URL" | sed -E 's#(https://github\.com/|git@github\.com:)##' | sed 's/\.git$//')
   ```

2. Fetch available labels:
   ```bash
   gh label list --repo "$REPO" --json name,description --limit 50
   ```

3. Fetch available milestones:
   ```bash
   gh api "repos/${REPO}/milestones?state=open" --jq '.[] | "\(.number) \(.title) (due: \(.due_on // "no date"))"'
   ```

4. Fetch project boards (GitHub Projects v2):
   ```bash
   gh project list --owner "$(echo $REPO | cut -d/ -f1)" --format json --limit 10 2>/dev/null
   ```

5. Read CLAUDE.md if it exists — look for issue conventions, labels taxonomy, or milestone naming patterns.

## Phase 2 — Classify the Issue

Based on what the user described, determine:

- **Type**: bug, feature, enhancement, chore, docs, refactor, test
- **Priority**: critical, high, medium, low (if labels exist for this)
- **Component**: which part of the system is affected (from project structure)
- **Milestone**: which milestone it belongs to (match by name/theme)

### Label Mapping

Map the issue type to existing repo labels. Common patterns:
- `bug` → "bug" label
- `feature` → "enhancement" or "feature" label
- `docs` → "documentation" label
- `chore` → "chore" or "maintenance" label

If appropriate labels don't exist, suggest creating them but don't block on it.

### Milestone Matching

Match the issue to a milestone based on:
1. User explicitly names a milestone → use it
2. Issue relates to current sprint/version → find the active milestone
3. Issue is a future enhancement → find the next milestone or backlog
4. No clear match → ask the user which milestone, showing the list

## Phase 3 — Draft the Issue

Generate the issue content:

**Title**: Clear, concise, imperative form (e.g., "Add rate limiting to API endpoints")
- Keep under 80 characters
- Start with a verb: Add, Fix, Update, Remove, Implement, Refactor

**Body** template:
```markdown
## Description
<clear description of the issue>

## Context
<why this is needed, what problem it solves>

## Acceptance Criteria
- [ ] <specific, testable criteria>

## Additional Notes
<any relevant links, screenshots, or technical details>
```

For **bugs**, use this template instead:
```markdown
## Bug Description
<what's happening vs what should happen>

## Steps to Reproduce
1. <step>
2. <step>

## Expected Behavior
<what should happen>

## Actual Behavior
<what actually happens>

## Environment
- App: <app name>
- Branch: <branch>
- Deployment: <production/staging>
```

## Phase 4 — Confirm with User

Before creating, show the user:
- Title
- Body (abbreviated)
- Labels to apply
- Milestone to assign
- Project board (if applicable)

Ask for confirmation. Let them adjust anything.

## Phase 5 — Create & Assign

```bash
# Create the issue
gh issue create \
  --repo "$REPO" \
  --title "<title>" \
  --body "$(cat <<'EOF'
<body>
EOF
)" \
  --label "<label1>,<label2>" \
  --milestone "<milestone title>"
```

If a project board was identified, add the issue to it:
```bash
# Get the issue number from creation output
ISSUE_NUMBER=<number>

# Add to project board (GitHub Projects v2)
ITEM_ID=$(gh project item-add <PROJECT_NUMBER> --owner "<owner>" --url "https://github.com/${REPO}/issues/${ISSUE_NUMBER}" --format json --jq '.id' 2>/dev/null)
```

If the issue should be assigned to someone:
```bash
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --add-assignee "<username>"
```

## Phase 6 — Report

```
## Issue Created

- **URL**: <issue url>
- **Number**: #<number>
- **Title**: <title>
- **Labels**: <labels>
- **Milestone**: <milestone>
- **Project**: <project board, if assigned>
- **Assignee**: <assignee, if set>
```

## Batch Mode

If the user provides multiple issues at once (e.g., a list), create them all sequentially and report a summary table:

```
| # | Title | Labels | Milestone |
|---|-------|--------|-----------|
| 1 | ...   | ...    | ...       |
```

## Notes

- Always confirm before creating — never auto-create without showing the user what will be created.
- If the repo has no milestones, skip milestone assignment and mention it.
- If the repo has no labels, create the issue without labels and suggest setting up a label taxonomy.
- For repos in SEMCL.ONE org, check if there's a shared labeling convention across repos.
- If the user says "track this" during a conversation about a bug or feature, infer the issue details from the conversation context.
