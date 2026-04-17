---
name: gh-prdone
description: >
  Use when the user says they merged a PR and deleted the branch, 
  or asks to "sync after merge", "prdone", "pr done", "finish pr", 
  "merged the PR", "clean up after merge", or needs to switch back 
  to main and verify integrated changes.
tools: Bash, Read, Grep, Glob
---

# GitHub PR Done — Post-Merge Cleanup & Verification

Handles the workflow after merging a PR and deleting the feature branch: switch to main, pull latest, and verify the changes integrated correctly.

## Phase 1 — Return to Main Branch

1. Identify the default branch:
   ```bash
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
   if [ -z "$DEFAULT_BRANCH" ]; then
     DEFAULT_BRANCH=$(git remote show origin | grep 'HEAD branch' | awk '{print $NF}')
   fi
   echo "Default branch: $DEFAULT_BRANCH"
   ```

2. Check for uncommitted changes:
   ```bash
   git status --porcelain
   ```
   - If dirty, warn the user and ask how to proceed (stash, discard, or abort)

3. Switch to the default branch:
   ```bash
   git checkout "$DEFAULT_BRANCH"
   ```

## Phase 2 — Pull Latest Changes

1. Fetch and pull:
   ```bash
   git fetch origin
   git pull origin "$DEFAULT_BRANCH"
   ```

2. Prune deleted remote branches:
   ```bash
   git fetch --prune
   ```

3. Clean up local branches that no longer exist on remote:
   ```bash
   # List local branches that are gone from remote
   git branch -vv | grep ': gone]' | awk '{print $1}'
   ```
   - If there are stale branches, ask the user if they want to delete them

## Phase 3 — Verify Integration

1. Show the recent commits to confirm the merge:
   ```bash
   git log --oneline -10
   ```

2. If the user mentioned what changes were in the PR, verify them:
   - Check that expected files were modified: `git show --stat HEAD`
   - Read key files to confirm changes are present
   - Run relevant tests if identifiable

3. Quick health check:
   - Check for any build/lint issues if package.json or similar exists
   - Run `npm test` / `cargo test` / `go test` if appropriate and quick

## Phase 4 — Report

```
## Post-Merge Status

- **Branch**: <default branch>
- **Latest commit**: <short hash> - <commit message>
- **Merged PR**: <PR info if identifiable from commit>
- **Changes verified**: <yes/no with details>
- **Stale branches cleaned**: <list if any>
```

## Notes

- If the user didn't specify what was in the PR, ask or infer from recent merge commits
- For monorepos, focus verification on the affected workspace/package
- If tests exist, run them to confirm nothing broke
