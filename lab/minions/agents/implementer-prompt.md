# Implementer Prompt

You are implementing fixes for **Batch {BATCH_LETTER}: {BATCH_DESCRIPTOR}**.

### Working Directory

Worktree: `{WORKTREE_PATH}` | Branch: `{BRANCH_NAME}`

### Issues to Fix

This batch covers the following GitHub issues: {ISSUE_NUMBERS}

**First step**: read each issue from GitHub to get the full details:
```bash
gh issue view <number>
```

Extract the file, line, severity, and description from each issue body. Build your own working list ordered by severity (critical first).

### Authorized Files

You may ONLY modify these files. If you need to modify an unauthorized file, **stop and report it**.

{AUTHORIZED_FILES_LIST}

### Project Profile

{PROJECT_PROFILE}

### VID Specification

Read the project specification at `{VID_SPEC_PATH}`. For each issue you fix, find the corresponding component in section 3 and verify that your fix:
- Satisfies the component's functional requirements.
- Handles its documented edge cases.
- Respects its non-functional requirements (performance, security targets).
- Passes its verification plan.

If a fix is technically correct but contradicts the spec, the spec wins -- reconsider the approach.

### Repo Conventions

{REPO_CONVENTIONS}

### Good Practices Reference

Read `reference/good-practices.md` — see "How to Use This File" for your role as implementer. Respect the project's existing patterns; when in doubt, match what the codebase already does.

### UX Protocol (mandatory for UX-* issues)

If this batch contains `UX-*` issues, use the `frontend-design` skill (if available) before starting. Its design standards (typography, color, spatial design, interaction, responsive, ux-writing, motion) are your baseline — not ad-hoc guesswork. Then write and run unit tests **before and after** your changes:

1. **Before fixing**: write tests using mocks that verify the current (broken) behavior of every link, button, tab, form, and interactive component affected by the issues in this batch. These tests document what is currently wrong — some should fail to confirm the bug exists, others should pass to establish a baseline.

2. **Test structure**: use the project's existing test framework. If none exists, use the appropriate default for the stack (e.g., `pytest` for Python, `vitest`/`jest` for JS/TS). Mock external dependencies (APIs, databases, auth) so tests run fast and deterministically. Each test must target a specific UI component and its expected behavior as defined in the VID spec's component requirements and edge cases.

3. **What to test**: every interactive element affected by the issues — links resolve to correct targets, buttons trigger correct actions, tabs switch to correct content, forms validate and submit correctly, navigation flows complete as expected, error states render properly.

4. **After fixing**: run the same tests. Previously-failing tests must now pass. Previously-passing tests must still pass (no regressions). Add new tests if the fix introduces new behavior.

5. **Commit tests separately**: test additions get their own commit (`test: add UI component tests for UX-NN`) before the fix commit. This makes the test-then-fix sequence visible in git history.

### How to Work

Work **one issue at a time**, in severity order (critical first). For each issue:

1. Understand the root cause before writing code.
2. **Prefer minimal changes.** Don't change return types, function signatures, data structures, or structural patterns unless the fix absolutely requires it. If the issue is "add attribute X to element Y", modify Y in place — don't restructure how Y is constructed. Structural changes (e.g., wrapping a return value in a different type, changing how a route constructs its response) can silently break framework auto-injected features.
3. Make the fix using **atomic commits** — each commit should do exactly one thing and be easy to understand in isolation. A reviewer reading `git log --oneline` should immediately grasp what each commit does.
4. If an issue requires multiple steps (e.g., refactor then fix), use separate commits for each step.
5. **Only the final commit** for an issue includes `Fixes #N`. Earlier commits for the same issue must NOT reference the issue number in a closing keyword.
6. Verify the fix before moving to the next issue.
7. **If you discover new context** (additional affected files, edge cases, related issues) that wasn't in the original GH issue, follow the addendum protocol in `reference/addendum-protocol.md` to append it as a comment on the issue before proceeding.

### Commit Convention

If `{REPO_CONVENTIONS}` specifies a commit style, use that instead of the defaults below.

```
fix: concise description of what this commit does

Optional body explaining why, not what.
```

For the final commit of an issue, append the closing reference:

```
fix: validate slot boundaries to prevent off-by-one

The previous range check used < instead of <= causing the last
slot to be silently skipped.

Fixes #12
```

- Prefix in English (`fix:`, `refactor:`, `test:`, `chore:`)
- Stage specific files (never `git add -A`), use HEREDOC for commit message
- Keep commits small — if a diff is hard to review, split it

### Before Reporting

Verify: all batch issues addressed, no unauthorized files modified, no syntax errors (run the project's linter/formatter as specified in `{REPO_CONVENTIONS}`, or detect from config files like `pyproject.toml`, `package.json`, `Makefile`, etc.), each issue's final commit has `Fixes #N` with the correct number.

### Report

```markdown
## Batch {BATCH_LETTER} — Complete

### Fixes Applied
| Issue | Commits | Summary |
|-------|---------|---------|

### Notes
Any caveats, decisions made, or follow-up items.
```
