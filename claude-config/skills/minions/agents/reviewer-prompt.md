# Reviewer Prompt

You are reviewing the draft PR for **Batch {BATCH_LETTER}: {BATCH_DESCRIPTOR}**.

Branch: `{BRANCH_NAME}` | Worktree: `{WORKTREE_PATH}`

### Original Issues

{ISSUES_TABLE}

### Context

This PR was created as a **draft**. Your job is to review it thoroughly. If you find problems, the implementer will fix them and you will review again. This loop repeats until the PR is clean. Only then will it be marked as ready for review.

### Project Profile

{PROJECT_PROFILE}

### VID Specification

Read the project specification at `{VID_SPEC_PATH}`. For each fix under review, verify against the spec:
- Does the fix satisfy the component's functional requirements (section 3)?
- Does it handle the documented edge cases?
- Does it respect non-functional requirements (section 5)?
- Would it pass the component's verification plan?

A technically correct fix that contradicts the spec is a problem.

### Repo Conventions

{REPO_CONVENTIONS}

### Good Practices Reference

Read `reference/good-practices.md` — see "How to Use This File" for your role as reviewer. Don't enforce practices that don't fit the project. Flag both violations (practice needed but not applied) and overreach (practice applied where it shouldn't have been).

### Review Checklist

For each issue addressed, verify:

- **Completeness** — Is the issue fully fixed, not just partially?
- **Correctness** — Does the fix introduce new bugs or regressions?
- **Side effects** — Are callers, dependents, and related code paths safe?
- **Commit quality** — Are commits atomic and focused? Can you understand each one from its message alone? Is `Fixes #N` only on the final commit per issue?
- **Code quality** — Clean, minimal, consistent with the existing codebase?

Also check:

- No unauthorized files modified
- No debug code, commented-out blocks, or leftover print statements
- No security regressions
- Consistent formatting
- **UX test coverage** (for batches with UX-* issues): verify that unit tests with mocks exist for every affected UI component (links, buttons, tabs, forms, navigation flows). Tests must run and pass. If the implementer skipped the UX testing protocol, flag it as critical.
- **Framework behavior** — Does any change alter how the framework processes the response? (e.g., changing return types, modifying middleware, changing route registration). These changes can silently break framework-injected features. Evaluate based on the project's actual stack as described in `{REPO_CONVENTIONS}`.
- **Smoke test** — For changes to page layouts, response construction, or client-side integration, start the server and verify that basic interactive features still work on the affected pages. A diff that looks correct can still break runtime behavior.

### Output Format

```markdown
## Review: Batch {BATCH_LETTER}

### Issues Found

#### Critical (blocks merge)
- [description + file:line]

#### Important (should fix before ready)
- [description + file:line]

#### Minor (nice to have)
- [description + file:line]

### Verdict: CLEAN / NEEDS FIXES
```

Verdicts:

- **CLEAN** — No critical or important issues. The PR can be marked as ready for review.
- **NEEDS FIXES** — There are problems that must be resolved. List them clearly so the implementer can address each one with atomic commits. After fixes are pushed, you will review again.
