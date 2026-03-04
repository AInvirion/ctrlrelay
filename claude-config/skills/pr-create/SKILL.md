---
name: pr-create
description: >
  This skill should be used when the user asks to "create a PR", "open a PR",
  "make a pull request", "submit PR", "PR this", "open pull request",
  "create merge request", or wants to create a GitHub pull request
  following project conventions.
tools: Bash, Read, Grep, Glob
---

# PR Create — GitHub Pull Request

Creates a GitHub pull request following project conventions: validates branch, generates title/body from commits, checks CI, and opens the PR.

## Prerequisites

- `gh` CLI authenticated
- On a feature/bugfix/hotfix branch (not `main` or `master`)
- Changes pushed to remote

## Phase 1 — Validate Branch & State

1. Get current branch:
   ```bash
   BRANCH=$(git branch --show-current)
   ```

2. Abort if on `main` or `master` — tell the user to create a feature branch first.

3. Check branch naming convention. Warn (don't block) if it doesn't match:
   - `feature/*` — new features
   - `bugfix/*` — bug fixes
   - `hotfix/*` — production hotfixes
   - `refactor/*`, `chore/*`, `docs/*` — also acceptable

4. Check for uncommitted changes:
   ```bash
   git status --porcelain
   ```
   If there are uncommitted changes, ask the user if they want to commit first.

5. Ensure branch is pushed and up to date:
   ```bash
   git push origin HEAD 2>&1
   ```

## Phase 2 — Determine Base Branch

1. Default base is `main`. If `main` doesn't exist, use `master`.
   ```bash
   git rev-parse --verify origin/main 2>/dev/null && echo "main" || echo "master"
   ```

2. If the user specifies a different base, use that instead.

## Phase 3 — Generate PR Content

1. Get all commits on this branch since diverging from base:
   ```bash
   BASE_BRANCH=main  # or master
   git log origin/${BASE_BRANCH}..HEAD --oneline --no-merges
   ```

2. Get the full diff stat:
   ```bash
   git diff origin/${BASE_BRANCH}..HEAD --stat
   ```

3. Generate PR title:
   - If single commit: use the commit message as title
   - If multiple commits: derive from the branch name (e.g., `feature/add-auth` → "Add auth")
   - Keep under 70 characters

4. Generate PR body using this template:
   ```markdown
   ## Summary
   - <bullet points summarizing the changes based on commits and diff>

   ## Changes
   <list of commits>

   ## Test plan
   - [ ] <testing checklist items based on what changed>
   ```

5. If commits reference GitHub issues (e.g., `#123`, `fixes #45`), include them in the body and they'll auto-link.

## Phase 4 — Check CI Status

Before creating the PR, check if there's a running CI workflow:
```bash
gh run list --branch "$(git branch --show-current)" --limit 1 --json status,conclusion,name 2>/dev/null
```

If CI is running, note it in the output. If CI failed, warn the user but don't block.

## Phase 5 — Create PR

```bash
gh pr create \
  --title "<generated title>" \
  --body "$(cat <<'EOF'
<generated body>
EOF
)" \
  --base "<base branch>"
```

If the user specified reviewers, add `--reviewer <user>`.
If the user specified labels, add `--label <label>`.

## Phase 6 — Report

```
## PR Created

- **URL**: <pr url>
- **Title**: <title>
- **Base**: <base branch> ← <current branch>
- **Commits**: <count>
- **Files changed**: <count>
- **CI**: <running/passed/failed/none>
```

## Options

- If the user provides a PR title, use it instead of generating one.
- If the user says "draft PR" or "draft", add `--draft` flag.
- If the user says "PR to <branch>", use that as the base branch.
- If CLAUDE.md specifies PR conventions or templates, follow those instead.

## Notes

- Always show the user the generated title and body before creating the PR, and ask for confirmation.
- Never force-push or rebase as part of PR creation.
- If a PR already exists for this branch, show its URL instead of creating a duplicate.
