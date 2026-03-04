---
name: session-handoff
description: >
  This skill should be used when the user asks to "wrap up", "end session",
  "session handoff", "handoff", "save progress", "what did we do",
  "summarize session", "session summary", "closing time", or wants to
  capture the current session state for continuity in the next session.
tools: Bash, Read, Grep, Glob, Write, Edit
---

# Session Handoff — Capture & Continue

Summarizes the current session's work, updates status files, and ensures nothing is left in a broken state.

## Phase 1 — Gather Session Context

1. Check for uncommitted work:
   ```bash
   git status --porcelain 2>/dev/null
   ```

2. Get recent commits from this session (last few hours):
   ```bash
   git log --oneline --since="8 hours ago" --no-merges 2>/dev/null
   ```

3. Check for unpushed commits:
   ```bash
   git log origin/$(git branch --show-current)..HEAD --oneline 2>/dev/null
   ```

4. Check current branch:
   ```bash
   git branch --show-current
   ```

5. Check if there are any open PRs for the current branch:
   ```bash
   gh pr list --head "$(git branch --show-current)" --state open --json url,title 2>/dev/null
   ```

## Phase 2 — Assess State

Identify:
- **Completed work**: commits made, PRs created/merged, issues closed
- **In-progress work**: uncommitted changes, open PRs, unfinished features
- **Blockers**: failing tests, deployment issues, open questions
- **Next steps**: what should be done next based on the work trajectory

## Phase 3 — Update Status Files

### SESSION_STATUS.md

Create or update `SESSION_STATUS.md` in the project root:

```markdown
# Session Status

**Date**: <today's date>
**Branch**: <current branch>
**Last commit**: <hash> <message>

## What was done
- <bullet points of completed work>

## Current state
- <uncommitted changes, if any>
- <unpushed commits, if any>
- <open PRs, if any>
- <test status: passing/failing>

## Blockers
- <any blockers or open questions, or "None">
```

### NEXT_SESSION.md

Create or update `NEXT_SESSION.md` in the project root:

```markdown
# Next Session

**Priority**: <what to tackle first>

## TODO
- [ ] <next steps, ordered by priority>

## Context
- <any context needed to resume work efficiently>
- <key decisions made this session>
- <links to relevant PRs/issues>

## Quick Start
\`\`\`bash
<commands to get back into the flow, e.g.>
git checkout <branch>
<run tests, start dev server, etc.>
\`\`\`
```

## Phase 4 — Safety Checks

1. **Uncommitted work**: If there are uncommitted changes, ask the user if they want to commit before wrapping up.
2. **Unpushed commits**: If there are unpushed commits, ask the user if they want to push.
3. **Failing tests**: If tests were recently run and failed, note this prominently in NEXT_SESSION.md.

## Phase 5 — Report

```
## Session Summary

### Completed
- <what got done>

### Left in Progress
- <what's still open>

### Files Updated
- SESSION_STATUS.md — current state captured
- NEXT_SESSION.md — next steps documented

### Reminders
- <uncommitted changes warning, if applicable>
- <unpushed commits warning, if applicable>
- <anything else the user should know>
```

## Notes

- Read the existing SESSION_STATUS.md and NEXT_SESSION.md before overwriting — preserve any manually added notes.
- If CLAUDE.md exists, reference it for project context but do NOT modify it.
- Keep summaries concise — focus on actionable items, not exhaustive logs.
- If the conversation context contains enough information about what was done, use it. Otherwise, rely on git history.
