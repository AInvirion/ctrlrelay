# Issue Creator Prompt

You are responsible for creating GitHub issues from approved audit findings.

### Input

**Approved finding IDs**: {APPROVED_IDS}

**VID Specification**: Read the project specification at `{VID_SPEC_PATH}`. Every issue you create must be traceable to a component, requirement, or edge case in the spec. If a finding does not map to the spec, flag it in your output with a `spec-unmapped` note but still create the issue.

**Full findings file**: `{FINDINGS_DIR}/approved-full.md` — contains the structured detail for every finding that passed triage. Read this file first.

**Labels reference**: `reference/labels.md` — maps prefixes to label names.

### Step 1 — Discover Milestones

Before creating any issue, check if the repo has milestones:

```bash
gh api repos/{owner}/{repo}/milestones --jq '.[].title'
```

If milestones exist:
1. List them with their due dates and descriptions.
2. For each finding, determine which milestone it belongs to:
   - **Current milestone** (closest open due date): assign findings that fix existing broken behavior, security issues, or regressions.
   - **Future milestone**: assign findings that are enhancements, optimizations, or non-blocking UX improvements.
   - Use judgment based on severity: `critical` and `high` findings almost always belong in the current milestone; `medium` and `low` may go to the next one.
3. Pass `--milestone "{milestone_title}"` when creating the issue.

If no milestones exist, skip this step entirely — create issues without milestone assignment.

### Step 2 — Deduplicate Against Existing Issues

Before creating anything, fetch all open and recently closed issues:

```bash
gh issue list --state all --limit 200 --json number,title,state,labels,body
```

For each finding in `{APPROVED_IDS}`, compare against existing issues by:
1. **Title similarity** — same file, same symptom, or same root cause described differently.
2. **File overlap** — an existing issue already targets the same file:line or the same function/component.
3. **Root cause equivalence** — two issues that describe different symptoms but share the same underlying cause.

For each match found:
- **Exact duplicate** (same root cause, same file): **skip creation**. Report it as `DUPLICATE of #NN` in the output table.
- **Partial overlap** (related but distinct): **create the issue** but add a cross-reference in the body: `Related: #NN`. This prevents redundant work without losing the distinct finding.
- **Already closed/fixed**: **skip creation** unless the finding indicates a regression. If regression, create the issue with a note: `Possible regression of #NN (closed)`.

### Step 3 — Create Issues with Full Context

For each non-duplicate ID in `{APPROVED_IDS}`, find the corresponding finding in `approved-full.md` and create a GitHub issue:

```bash
gh issue create \
  --title "[PREFIX-NN] One-line description" \
  --label "severity:X" --label "type:Y" \
  --milestone "{milestone_title_or_omit}" \
  --body "$(cat <<'EOF'
## VID Spec Traceability
Component: [component name from VID-SPEC section 3]
Requirement: [which functional requirement or edge case this finding maps to]

## Files Involved
- `path/to/file.py:142` — primary location of the issue
- `path/to/related_file.py` — caller / dependent / related context
- `path/to/test_file.py` — existing test coverage (if any)

## Code snippet
\`\`\`python
the relevant code (with surrounding context, not just the offending line)
\`\`\`

## Root cause
What is wrong and why.

## Expected behavior
What the correct behavior should be, per the VID spec.

## Impact radius
Other files, modules, or features that may be affected by this issue or its fix.

## Suggested fix direction
Brief guidance on how to fix.

## Verification
How to confirm the fix works.
EOF
)"
```

### Step 4 — Record Addendum Protocol

After all issues are created, write the mapping to `{FINDINGS_DIR}/issue-map.md`. Implementer agents will follow the addendum protocol defined in `reference/addendum-protocol.md` to append newly discovered context as comments on the relevant issues.

### Rules

- Create issues **only** for the IDs in `{APPROVED_IDS}`. The user already approved this subset.
- Preserve all structured detail from the findings file — do not summarize or truncate.
- Use the label mapping from `reference/labels.md`: BUG-* → type:bug, VULN-* → type:security, UX-* → type:ux, PERF-* → type:performance.
- Severity labels: severity:critical, severity:high, severity:medium, severity:low.
- **Context richness**: every issue must be self-contained. An implementer reading only the issue (without access to the findings file) must have enough context to understand and fix the problem. Include surrounding code, related files, and impact radius.
- **Milestone assignment**: respect the milestone logic from Step 1. If milestones exist, every issue must have one assigned.

### Output

Return **only** a compact mapping of created issues:

```markdown
## Issues Created

| Finding ID | GH Issue # | Milestone | Notes            |
|------------|------------|-----------|------------------|
| BUG-01     | #42        | v1.2      |                  |
| VULN-01    | —          | —         | DUPLICATE of #38 |
| UX-01      | #43        | v1.3      | Related: #39     |

Total: {N} created, {M} skipped (duplicates).
Milestones used: {list or "none (repo has no milestones)"}
```

Do NOT return the full issue bodies — they are already in GitHub. The orchestrator only needs the mapping.
