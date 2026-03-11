---
name: codex-review-loop
description: Orchestrate a code review loop with Codex. Use after implementing a feature to get it reviewed, fix issues, and verify fixes until Codex approves. Triggers on "review with codex", "codex review loop", "QA this", "get codex to review".
tools: mcp__codex-reviewer__codex_review, mcp__codex-reviewer__codex_security_review, mcp__codex-reviewer__codex_find_duplicates, mcp__codex-reviewer__codex_find_dead_code, mcp__codex-reviewer__codex_verify_fixes, mcp__codex-reviewer__codex_test_coverage, mcp__codex-reviewer__codex_performance_review
---

# Codex Review Loop

This skill orchestrates an automated code review cycle between you (Claude) and Codex. You implement, Codex reviews, you fix, Codex verifies, repeat until clean.

## When to Use

- After implementing a feature
- After fixing a bug
- Before creating a PR
- When user says "QA this", "review with codex", "get codex to check"

## The Loop

```
┌─────────────────────────────────────────────────────────┐
│ 1. PREPARE                                              │
│    - Identify files to review                           │
│    - Summarize what was implemented                     │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 2. REVIEW (call Codex tools)                            │
│    - codex_review (general)                             │
│    - codex_security_review                              │
│    - codex_find_duplicates                              │
│    - codex_find_dead_code                               │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 3. PARSE RESULTS                                        │
│    - Extract BLOCKING issues (must fix)                 │
│    - Extract CONCERNS (should fix)                      │
│    - Note SUGGESTIONS (optional)                        │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 4. FIX ISSUES                                           │
│    - Address all BLOCKING issues                        │
│    - Address CONCERNS if reasonable                     │
│    - Document what was fixed                            │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 5. VERIFY                                               │
│    - Call codex_verify_fixes with original issues       │
│    - Check if all BLOCKING resolved                     │
└────────────────────────┬────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
    ┌─────────────────┐   ┌─────────────────┐
    │ Issues remain   │   │ All clear       │
    │ → Go to step 4  │   │ → Done!         │
    └─────────────────┘   └─────────────────┘
```

## Execution Steps

### Step 1: Identify Scope

Ask the user or determine from context:
- Which files/directories to review?
- What was implemented? (context helps Codex)
- Any specific concerns to focus on?

### Step 2: Run Initial Reviews

Call multiple review tools in parallel for efficiency:

```
codex_review(path: "src/feature/")
codex_security_review(path: "src/feature/")
codex_find_duplicates(path: "src/feature/")
codex_find_dead_code(path: "src/feature/")
```

### Step 3: Consolidate Findings

Parse all responses and create a unified issue list:

```markdown
## Review Summary

### BLOCKING (must fix before merge)
1. [security] SQL injection in user_handler.py:45
2. [correctness] Missing null check in process_data()

### CONCERNS (should address)
1. [duplication] Similar logic in handlers A, B, C
2. [performance] O(n²) loop in batch_process()

### SUGGESTIONS (nice to have)
1. [naming] Consider renaming 'do_thing' to 'process_payment'

### PASSED
- No dead code found
- Test coverage adequate
```

### Step 4: Fix Issues

For each BLOCKING issue:
1. Read the relevant code
2. Understand the problem
3. Apply the fix
4. Note what was changed

For CONCERNS:
- Fix if straightforward
- Note if deferred (explain why)

### Step 5: Verify Fixes

Call verify with the original issues:

```
codex_verify_fixes(
  path: "src/feature/",
  original_issues: "1. SQL injection in user_handler.py:45\n2. Missing null check..."
)
```

### Step 6: Loop or Complete

Parse verify response:
- If "NOT FIXED" items exist → fix them and verify again
- If all "FIXED" → report success to user

## Output Format

Report progress to the user:

```markdown
## Codex Review Loop - Iteration 1

### Codex Found:
- 2 BLOCKING issues
- 3 CONCERNS
- 1 SUGGESTION

### Fixing BLOCKING Issues...

**Issue 1**: SQL injection in user_handler.py:45
- Problem: User input passed directly to query
- Fix: Added parameterized query
- Status: FIXED

**Issue 2**: Missing null check in process_data()
- Problem: data.items accessed without null check
- Fix: Added guard clause
- Status: FIXED

### Verification...

Calling Codex to verify fixes...

**Result**: All BLOCKING issues resolved!

### Remaining CONCERNS (deferred):
- Duplication in handlers - will address in refactor ticket

### Review Complete
```

## Handling Disagreements

If you believe Codex's finding is incorrect:

1. Re-read the code carefully - Codex might be right
2. If still disagree, explain your reasoning to the user
3. Let the user decide whether to fix or skip

## Review Types by Situation

| Situation | Reviews to Run |
|-----------|----------------|
| New feature | review, security, duplicates |
| Bug fix | review, verify original bug fixed |
| Refactoring | duplicates, dead_code, review |
| Security-sensitive | security (with threat model), review |
| Performance work | performance, review |
| Pre-PR | all reviews |

## Limits

- Max 3 iterations before asking user for guidance
- If same issue persists after 2 fix attempts, flag to user
- Don't spend more than 5 minutes on a single CONCERN
