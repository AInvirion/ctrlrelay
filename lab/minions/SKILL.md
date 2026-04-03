---
name: minions
description: >
  Use when needing to audit a codebase for bugs, security or UX issues and fix
  them in bulk via parallel batched worktrees. Launches 3 auditor agents, triages
  findings into GH issues, batches by file, and dispatches parallel implementers.
instructions: >
  Invoke this skill when the user asks to audit, review, or fix bulk issues across
  a codebase. Auto-resumes from handoff file if a previous planning session exists;
  otherwise runs planning (Phases 0-4), writes handoff, and stops. Use "minions:
  restart" to force a fresh planning run. Requires: gh CLI authenticated, git
  worktree support.
---

# Minions — Parallel Audit & Fix Workflow

Announce: "Invoking the **minions** skill to run a parallel audit → triage → batch fix workflow."

## Scope

By default, audit the **entire codebase**. However, if the user invokes the skill with specific context (e.g., a set of files, a module, a feature area, or a particular problem), **focus the audit on that scope only**. Pass the scope constraint to every auditor agent so they don't waste time on unrelated code.

## Agent Dispatch Strategy

Each phase dispatches subagents with different cost/quality profiles. Use the right model for the job:

| Agent role     | Model   | Reasoning effort | Why                                                                 |
|----------------|---------|------------------|---------------------------------------------------------------------|
| Auditor        | opus    | high             | Audit quality is the foundation — false negatives here propagate through the entire pipeline. |
| Triager        | sonnet  | medium           | Dedup and severity calibration on structured data. Doesn't need deep reasoning. |
| Issue Creator  | sonnet  | medium           | Mechanical task: read findings file, create GH issues. No judgment calls. |
| Implementer    | sonnet  | medium           | Fixes are scoped and constrained by authorized files. Sonnet is fast and capable enough for focused edits. |
| Reviewer       | opus    | high             | The reviewer is the last line of defense before code hits main. Thoroughness matters more than speed. |

These are defaults. If the orchestrator's own model is sonnet, use sonnet for all roles — don't dispatch agents on a more expensive model than the orchestrator.

For **token efficiency**: the orchestrator should never hold detailed findings in its context. Auditors write to disk; the triager reads from disk and returns only a compact table; the issue creator reads from disk and returns only issue numbers. Implementers receive issue numbers and read details from GitHub on demand. Reviewers only see their batch's PR diff. The orchestrator's context stays lean throughout the entire pipeline.

## Phases

### Entry Points

**Before anything else**, check if `.minions-handoff.json` exists at the repo root.

- **If it exists**: show a summary (batch count, issue count, creation date) and resume at Phase 5.
- **If it does not exist**: start planning from Phase 0.

Override: `"minions: restart"` — deletes the handoff file and forces planning from Phase 0.

### Phase 0 — Setup, profile project & discover repo conventions

**Step 1 — Project profile.** Before anything else, build a `{PROJECT_PROFILE}` by reading:
- `README.md` (or `README.rst`, `README.txt`) — what the project does, who it's for
- Manifest files (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `composer.json`, `Gemfile`, etc.) — stack, dependencies, project size signals
- Top-level directory structure (`ls -1`) — architecture signals (monolith, monorepo, microservice, single script, etc.)

Synthesize into `{PROJECT_PROFILE}`:
```
Project type: [web app / API / CLI / library / monorepo / script]
Domain: [e-commerce, healthcare, fintech, internal tool, ...]
Stack: [languages + frameworks]
Architecture: [monolith / microservice / single-file]
Size: [small (<10 files) / medium (10-100) / large (100+)]
Has tests: [yes/no]
Has CI: [yes/no]
```
This drives which good practices apply and how auditors calibrate severity.

**Step 2 — Repo conventions.** Check for convention files: `CONTRIBUTING.md`, `CONTRIB.md`, `CONTRIBUTION.md`, `.github/CONTRIBUTING.md`, and `CLAUDE.md`. If any exist, read them and extract rules about commit style, branch naming, PR format, code style, linter/formatter commands, and any other conventions. **These conventions take priority over the defaults in this skill** — adapt commit prefixes, branch naming, PR titles, and label schemes to match the repo's own rules.

**Step 3 — Detect tooling.** Identify the project's linter/formatter tools from config files and manifest (e.g., `ruff` for Python, `eslint`/`prettier` for JS, `go vet` for Go). Record these for use in implementer and reviewer prompts.

**Step 4 — Create GH labels** using commands from `reference/labels.md` (idempotent with `--force`). If the repo already has its own label scheme, use that instead.

**Step 5 — Locate or generate VID-SPEC.md.** Check if `VID-SPEC.md` exists at the repo root. This file is the project specification produced by the `Verified Intent Development` (VID) skill.

- **If `VID-SPEC.md` exists**: record `{VID_SPEC_PATH}` (the file path) for all agents. Agents read the spec directly -- they don't receive a summary.
- **If `VID-SPEC.md` does not exist but `SPEC.md` does**: use `SPEC.md` as fallback, but warn the user it was not produced by VID and may lack risk scores, verification plans, and component-level specifications.
- **If neither exists**: **stop and run the VID skill first** (or ask the user to run it). The VID spec is a hard dependency -- without it, auditors have no objective baseline to judge findings against, triagers can't calibrate severity, and implementers can't validate that their fixes serve the project's actual purpose. Proceeding without a spec produces noise.

From the VID-SPEC, extract and pass to all agents:
- **Purpose + Success Criteria** (sections 1.1, 1.4) → what the project does and how "done" is defined.
- **Component Specifications** (section 3) → risk scores, functional requirements, edge cases, verification plans per component. Auditors use these to calibrate findings; implementers use them to validate fixes.
- **Architecture Overview** (section 2) → how components interact. Critical for assessing impact radius.
- **Non-Functional Requirements** (section 5) → performance, security, scalability targets.

The VID-SPEC is the north star: every finding, issue, and fix must be traceable to it. Findings that don't map to a component, requirement, or edge case in the spec get deprioritized or flagged.

**Step 6 — Compile context.** Bundle all discovered conventions into `{REPO_CONVENTIONS}` and pass `{PROJECT_PROFILE}`, `{VID_SPEC_PATH}`, and `{REPO_CONVENTIONS}` to every subsequent agent. If no convention files were found, pass: "No repo-specific conventions found. Use this skill's defaults."

### Phase 1 — Audit (parallel)

Create a temporary findings directory: `{FINDINGS_DIR}` (e.g., `.minions-findings/`).

Launch **3 agents** with `agents/auditor-prompt.md` and `reference/good-practices.md`, each with a different perspective. Pass `{PROJECT_PROFILE}`, `{VID_SPEC_PATH}`, and `{REPO_CONVENTIONS}` to each. **Auditors write findings to disk** (`{FINDINGS_DIR}/logic.md`, `{FINDINGS_DIR}/security.md`, `{FINDINGS_DIR}/ux.md`) and return only a one-line summary to the orchestrator. This keeps the orchestrator's context clean.

Perspectives:
- **Logic** — Bugs, incorrect logic, race conditions, edge cases, performance bottlenecks → `BUG-*` / `PERF-*`
- **Security** — Injection, auth bypass, data exposure, OWASP top 10 → `VULN-*`
- **UX** — Broken flows, accessibility, inconsistent UI, missing states → `UX-*`. This auditor must use the `frontend-design` skill (if available) to ground its criteria in production-grade design standards.

**Orchestrator receives**: 3 one-liners only.

### Phase 2 — Triage & deduplicate (delegated)

Dispatch **triager** with `agents/triager-prompt.md`. The triager reads all finding files from `{FINDINGS_DIR}`, deduplicates, calibrates severity using `{PROJECT_PROFILE}`, and:
1. Writes the full deduplicated findings to `{FINDINGS_DIR}/approved-full.md`
2. Returns a **compact summary table** (ID, severity, file, one-liner) to the orchestrator

**Present the compact table to the user for approval.** The user marks which findings to fix. Record the approved IDs.

**Orchestrator receives**: compact summary table only.

### Phase 3 — Create GH issues (delegated)

Dispatch **issue creator** with `agents/issue-creator-prompt.md`. Pass `{APPROVED_IDS}`, `{VID_SPEC_PATH}`, and `{REPO_CONVENTIONS}`. The issue creator:
1. Checks for repo milestones and determines assignment per finding.
2. Deduplicates against existing open/closed issues (by title, file overlap, and root cause). Skips exact duplicates; cross-references partial overlaps; flags possible regressions of closed issues.
3. Reads `{FINDINGS_DIR}/approved-full.md` for full detail.
4. Creates self-contained GitHub issues with full context (files involved, code snippets with surrounding context, impact radius, intent alignment).
5. Returns a mapping table (Finding ID → GH issue # → Milestone → Notes) including skipped duplicates.

After the issue creator finishes, **delete `{FINDINGS_DIR}`** — all detail now lives in GitHub.

**Orchestrator receives**: mapping table only (Finding ID → GH issue # → Milestone). From this point forward, GitHub is the sole source of truth. Implementers follow `reference/addendum-protocol.md` to append newly discovered context.

### Phase 4 — Batch by file

**Step 1 — Query issues.** `gh issue list --state open --label "type:bug,type:security,type:ux,type:performance" --json number,title,labels,body`. Parse each issue's file references from its body.

**Step 2 — Auto-calculate batch count and subagent allocation.** The orchestrator computes the optimal configuration based on the issue set:

```
Total issues: N
Unique files touched: F
File clusters (connected components by co-occurrence in issues): C

Batch count = min(C, 8)
  - If C <= 3: use C batches (small scope, no need to split further)
  - If 3 < C <= 8: use C batches (natural grouping)
  - If C > 8: merge smallest clusters until batch count = 8

Subagents per phase:
  - Auditors: 3 (fixed — one per perspective)
  - Triager: 1 (fixed)
  - Issue creator: 1 (fixed)
  - Implementers: {batch_count} (one per batch, parallel)
  - Reviewers: {batch_count} (one per batch, parallel)

Total subagents = 5 + (2 × batch_count)
```

Present the calculated configuration to the user alongside the batch table:
```
Calculated: {batch_count} batches, {total_subagents} subagents total
Estimated parallel rounds: auditors (1) + triage (1) + issues (1) + implement+review ({review_rounds}) + merge ({batch_count} sequential)
```

**Step 3 — Group into batches** (A, B, C...). Rules: **no file in more than one batch**; if an issue touches multiple files, all go to same batch. Present batch table and subagent calculation to user for approval.

### Phase 4.5 — Context Handoff (clean slate for execution)

The planning phases (0-4) accumulate audit findings, triage tables, user approvals, and batch calculations in the orchestrator's context. Before execution begins, **flush this context to disk** so the implementation phase starts clean.

**Step 1 — Write handoff file.** Create `.minions-handoff.json` at the repo root with all state needed for Phases 5-9:

```json
{
  "project_profile": "{PROJECT_PROFILE}",
  "vid_spec_path": "VID-SPEC.md",
  "repo_conventions": "{REPO_CONVENTIONS}",
  "linter_tools": ["ruff", "eslint", ...],
  "batches": {
    "A": {
      "descriptor": "auth-module",
      "issue_numbers": [42, 43],
      "authorized_files": ["src/auth.py", "src/auth_utils.py"],
      "has_ux_issues": false
    },
    "B": {
      "descriptor": "ui-navigation",
      "issue_numbers": [44, 45, 46],
      "authorized_files": ["templates/nav.html", "static/nav.js"],
      "has_ux_issues": true
    }
  },
  "batch_count": 2,
  "total_subagents": 9,
  "issue_map": {"BUG-01": 42, "VULN-01": 43, "UX-01": 44},
  "milestones_used": ["v1.2"],
  "main_branch": "main",
  "created_at": "2026-04-02T12:00:00Z"
}
```

**Step 2 — Instruct the user to start a new session.** Present:

```
Planning complete. State saved to .minions-handoff.json.
Start a new conversation and invoke "minions" — it will auto-resume from the handoff.
```

When the next session starts and detects the handoff file (see Entry Points), it validates the file (all batch definitions present, issue numbers resolve via `gh issue view`, main branch is clean), loads context, and proceeds directly to Phase 5. If the file is invalid, abort with a clear error.

### Phase 5 — Implement (parallel batches, iterative per issue)
Per batch: create worktree, dispatch implementer with `agents/implementer-prompt.md` and `reference/good-practices.md`. Pass the **list of issue numbers** for the batch (not the full issue content). Branch: `fix/batch-{letter}-{descriptor}`. All batches run in parallel.

For batches containing `UX-*` issues, the implementer must:
1. Use the `frontend-design` skill (if available) before starting work.
2. Follow the **UX Testing Protocol**: write unit tests with mocks for every affected UI component (links, buttons, tabs, forms, navigation) _before_ fixing, run them to confirm the bug, fix, then run again to confirm the fix and absence of regressions. See `agents/implementer-prompt.md` for the full protocol.

Within each batch the implementer works **one issue at a time**:
1. Read the issue details from GitHub (`gh issue view N`).
2. Fix the issue with **focused, atomic commits** — each commit does one thing and is easy to read.
3. An issue may require several commits; **only the final commit** includes `Fixes #N` to auto-close it.
4. Iterate until the issue is fully resolved before moving to the next one.

### Phase 6 — Draft PR + review loop
Per completed batch:
1. **Push branch and create a draft PR** (`gh pr create --draft`). PR title: `fix(batch-{letter}): {descriptor}`. Body lists all issues addressed with their `Fixes #N` references.
2. Dispatch reviewer with `agents/reviewer-prompt.md` against the draft PR.
3. **Iterate**: if the reviewer finds problems, the implementer fixes them with atomic commits in the same branch, then the reviewer runs again. Repeat until no issues are found. **Max 3 review rounds** — if the loop hasn't converged after 3 iterations, stop and escalate to the user with a summary of remaining issues.
4. When the review passes cleanly, **mark the PR as ready for review** (`gh pr ready`).

All batches go through this loop in parallel.

### Phase 7 — Merge (sequential)
Per approved batch, **sequentially**: merge PR → pull master before next batch. Never merge in parallel — it causes conflicts.

### Phase 8 — Post-merge verification
After all batches are merged, verify on the main branch. Use `{PROJECT_PROFILE}` to decide **what** to verify:

- **Always**: run the project's linter/formatter (if detected in Phase 0 Step 3).
- **If tests exist**: run the full test suite.
- **Web app / site**: start the server and verify that affected pages load and respond correctly.
- **API**: hit key endpoints and confirm expected status codes / response shapes.
- **CLI tool**: run representative commands and check output.
- **Library**: verify public exports resolve and basic usage examples work.

If any check fails with errors introduced by the merged fixes, create a follow-up fix branch, resolve the issues, and go through the draft PR + review loop (Phase 6) before re-merging. If the project has no tests, no linter, and no runnable entry point, skip this phase.

### Phase 9 — Cleanup
Remove all worktrees and local branches. Delete `.minions-handoff.json`. Verify all issues are closed.

## Guardrails

- Never create issues without user review (Phase 2 triage is mandatory)
- No file overlap between batches — causes merge conflicts; re-batch if detected
- Always merge sequentially — parallel merge causes conflicts
- Commits must be atomic and focused — one logical change per commit, easy to read in `git log`
- **Readability is non-negotiable** — "code is written once but must be read a thousand times." All code produced or modified must prioritize human readability: clear names, explicit logic, structure that reveals intent. See the Cardinal Rule in `reference/good-practices.md`.
- Only the **final commit** for an issue includes `Fixes #N` — earlier commits for the same issue must not
- PRs start as **draft**; only mark ready for review after the review loop finds zero issues
- Max 8 batches; consolidate small ones
- If >30 findings, prioritize critical/high only first
- If reviewer returns NEEDS FIXES, fix in-place in the same branch (don't create new worktree)
- If merge conflict in Phase 7, resolve in current worktree and re-review
