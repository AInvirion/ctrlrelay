---
name: code-interest-sniff-test
description: Use when reviewing source code to assess whether it is "interesting" from a copyright or IP perspective — before running formal plagiarism scans or involving legal. Triggers on code review, compliance triage, OSS audit, license risk assessment, or when an engineer asks "does this code matter?"
tools: Bash, Read, Grep, Glob
---

# Code Interest Sniff Test

Structured triage to determine if source code warrants plagiarism/IP verification. Produces a traffic-light verdict (GREEN/YELLOW/RED) with scoring dimensions explaining why.

## How to Run the Assessment

### Single File Mode

When the user points to a specific file (or you're reviewing a file):

1. Read the file with the Read tool
2. Run the SLOC counter: `bash ~/.claude/skills/code-interest-sniff-test/scripts/sloc.sh <file>`
3. Check for license/copyright headers: `head -20 <file>` — look for SPDX, GPL, MIT, Apache, copyright notices
4. Check git provenance: `git log --follow --diff-filter=A --format="%H %s" -- <file>` — was it copied or added from external source?
5. Apply the Pre-Filter, then score all 6 dimensions, then produce the verdict

### Directory / Repo Mode

When the user says "sniff test this repo", "audit this directory", "check all source files":

1. Find all source files, excluding vendored/generated:
   ```bash
   bash ~/.claude/skills/code-interest-sniff-test/scripts/sloc.sh --scan <directory>
   ```
2. Run the pre-filter on each file — auto-GREEN anything that matches
3. For remaining files, read each one and score all 6 dimensions
4. Produce a summary table sorted by verdict (RED first, then YELLOW, then GREEN)
5. Output individual JSON assessments only for RED and YELLOW files

### Diff / PR Mode

When reviewing a PR or diff:

1. Get changed files: `git diff --name-only <base>..HEAD` or `gh pr diff <number> --name-only`
2. For each changed file, read the full file (not just the diff — context matters for scoring)
3. Score and report only the changed files

## Pre-Filter (instant GREEN — skip scoring)

These files are almost never interesting. Auto-classify as GREEN:

- **< 10 SLOC** — too small to carry copyright
- **Generated/auto files** — protobuf stubs, lockfiles, bundled output, `.min.js`, `*.pb.go`, `*_generated.*`
- **Pure config** — JSON/YAML/TOML/INI/`.env.example` with no embedded logic
- **License/legal files** — LICENSE, NOTICE, COPYING, PATENTS
- **Test fixtures / sample data** — static test inputs, mock data, `testdata/`, `fixtures/`
- **Vendored dependencies** — `vendor/`, `node_modules/`, `third_party/`, `external/`
- **Migration files** — Alembic/Django/Rails migrations (auto-generated schema changes)
- **Package manifests** — `package.json`, `Cargo.toml`, `go.mod`, `requirements.txt`, `pyproject.toml` (dependency declarations, not logic)

When pre-filtering in batch mode, still count and report these files as "X files auto-GREEN (pre-filtered)".

## 6 Scoring Dimensions

Read the file and score each dimension as **LOW**, **MEDIUM**, or **HIGH**:

### 1. Size (SLOC, excluding blanks and comments)

Use the SLOC counter script for accurate counts:
```bash
bash ~/.claude/skills/code-interest-sniff-test/scripts/sloc.sh <file>
```

- **LOW:** < 30 SLOC
- **MEDIUM:** 30-150 SLOC
- **HIGH:** > 150 SLOC

### 2. Complexity (control flow depth and structure)

Measure by reading the code and analyzing:
- Maximum nesting depth (count nested if/for/while/match/try blocks)
- Number of branches (if/elif/else/case arms)
- Presence of state machines, coroutines, callbacks, recursive calls

Use as a supporting signal:
```bash
# Rough complexity indicators — count control flow keywords
grep -cE '^\s*(if|elif|else|for|while|try|except|catch|switch|case|match)\b' <file>
```

- **LOW:** Flat/linear — sequential statements, simple conditionals, max nesting 1-2
- **MEDIUM:** Moderate branching, loops with conditions, nesting 2-3 levels, 5-15 control flow keywords
- **HIGH:** Deep nesting (4+), state machines, coroutines, complex error recovery, recursive algorithms, 15+ control flow keywords

### 3. Novelty (how unique or non-obvious the logic is)

Assess by reading the code — no script can measure this, it requires understanding:
- Could a competent developer write this independently in 30 minutes?
- Does it solve a problem in a surprising or non-obvious way?
- Is the approach one you'd find in a textbook, or is it original?

- **LOW:** Standard patterns — CRUD operations, getters/setters, simple validations, string formatting
- **MEDIUM:** Domain-specific but common — date math, pagination, rate limiting, retry logic
- **HIGH:** Novel algorithms, unique approaches to problems, non-obvious optimizations, original data structures

### 4. Specificity (domain knowledge embedded in the code)

- **LOW:** Generic utility code — logging, config loading, HTTP helpers
- **MEDIUM:** Industry-specific logic — financial calculations, geospatial transforms, protocol implementations
- **HIGH:** Proprietary business logic, trade secrets, competitive differentiators, patentable methods

### 5. Algorithmic Density (presence of non-trivial algorithms)

Look for mathematical operations, bit manipulation, custom data structure implementations:
```bash
# Supporting signal — look for algorithmic indicators
grep -cE '(math\.|numpy|scipy|<<|>>|&|\\||\^|~|sqrt|log|sin|cos|matrix|vector|hash|digest|encrypt|decode|compress|decompress)' <file>
```

- **LOW:** No algorithms — data mapping, simple transformations, wiring code
- **MEDIUM:** Known/standard algorithms — sorting, searching, graph traversal, hashing
- **HIGH:** Crypto implementations, compression, ML/inference, signal processing, codec logic, numerical methods

### 6. Coupling Risk (how much organizational context the code carries)

Check imports, type references, and architectural role:
```bash
# Count internal imports (non-stdlib, non-third-party)
grep -cE '^(from|import)\s+[a-z_]+\.' <file>  # Python
grep -cE "^import.*from\s+['\"]\.\.?/" <file>  # JS/TS
```

- **LOW:** Standalone snippet — works in isolation, no domain model dependencies
- **MEDIUM:** Part of a module — references internal types/interfaces, moderate coupling
- **HIGH:** Core architectural component — central to the system, carries design decisions, hard to extract

## Verdict Rules

Apply mechanically — no overrides based on "feel":

```
Any dimension HIGH           -> RED    (scan + legal review recommended)
2+ dimensions MEDIUM         -> YELLOW (worth scanning)
Everything else              -> GREEN  (not interesting)
```

## False Positive Patterns

After scoring, check if the code matches a known false positive. If it does, note it in the output — it doesn't change the verdict, but gives context:

| Pattern | Why it's not interesting |
|---------|------------------------|
| Standard library reimplementations | Everyone writes the same binary search, linked list, LRU cache |
| Boilerplate / scaffolding | Framework-generated CRUD, CLI argument parsing, main() entrypoints |
| API wrappers with no logic | Thin HTTP clients, SDK wrappers, REST resource mappings |
| Common design patterns | Singleton, observer, factory, builder — these are public knowledge |
| Language idioms | Python list comprehensions, Go error handling, Rust match arms |
| Official docs / tutorial code | Copied from language docs, framework getting-started guides |
| Standard protocol implementations | Well-documented RFCs, publicly specified wire formats |
| Error handling boilerplate | Try/catch blocks, error type definitions, logging wrappers |

## When to Escalate Despite GREEN

Override GREEN -> YELLOW if any of these are detected:

```bash
# Check for license headers and copyright notices
head -30 <file> | grep -iE '(copyright|license|spdx|gpl|agpl|lgpl|mozilla|creative commons|all rights reserved)'

# Check for attribution blocks
grep -iE '(NOTICE|ATTRIBUTION|originally from|ported from|based on|derived from|copied from|adapted from)' <file>
```

- Code contains **license headers** from a known copyleft license (GPL, AGPL, LGPL)
- Code contains **copyright notices** from third parties (not the project owner)
- File has a **NOTICE** or **ATTRIBUTION** comment block
- Code matches a **known snippet** from a copyleft project (even if small)
- The code's **git history** shows it was copied from an external source:
  ```bash
  git log --follow --diff-filter=A --format="%s" -- <file> | grep -iE '(copy|port|import|vendor|from|based on)'
  ```

## Output Format

### Single File — JSON

```json
{
  "file": "path/to/file.py",
  "sloc": 87,
  "verdict": "YELLOW",
  "dimensions": {
    "size": "MEDIUM",
    "complexity": "LOW",
    "novelty": "MEDIUM",
    "specificity": "LOW",
    "algorithmic_density": "LOW",
    "coupling_risk": "LOW"
  },
  "reasoning": "One-sentence explanation of the verdict",
  "false_positive_check": "None detected",
  "escalation_override": "None",
  "provenance": "Original — no external origin detected in git history"
}
```

### Batch Mode — Summary Table + JSON Details

First output a summary table:

```
## Sniff Test Report — <directory or repo name>

### Summary
- Files scanned: X
- Pre-filtered (auto-GREEN): Y
- Scored: Z

| Verdict | Count | Files |
|---------|-------|-------|
| RED     | N     | file1.py, file2.go |
| YELLOW  | N     | file3.py, file4.js |
| GREEN   | N     | (not listed individually) |

### RED Files (require scan + review)

<JSON assessment for each RED file>

### YELLOW Files (worth scanning)

<JSON assessment for each YELLOW file>

### Recommendations
- <prioritized list of files to scan first, based on verdict and reasoning>
```

## Execution Steps

When this skill is triggered:

1. **Determine scope** — single file, directory, or PR diff
2. **Collect files** — use Glob to find source files, apply path exclusions
3. **Pre-filter** — auto-GREEN trivially uninteresting files using the SLOC script and path patterns
4. **Read & score** — for each remaining file, read it, run the supporting bash checks, score all 6 dimensions
5. **Apply verdict** — mechanically from the rules
6. **Check false positives** — note any matches
7. **Check escalation overrides** — scan for license headers, copyright notices, git provenance
8. **Report** — produce structured output (JSON for single file, table + JSON for batch)

For batch mode with many files (50+), process in parallel using the Agent tool with multiple subagents if needed.
