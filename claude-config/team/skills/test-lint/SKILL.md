---
name: test-lint
description: >
  This skill should be used when the user asks to "run tests", "lint", "check code",
  "run quality checks", "test", "check types", "mypy", "ruff", "pytest",
  "run the suite", "check everything", or wants to run linting, type checking,
  and tests for the current project.
tools: Bash, Read, Grep, Glob
---

# Test & Lint — Project Quality Checks

Runs the full quality suite for the current project: linting → type checking → tests. Auto-detects project type and available tools.

## Phase 1 — Detect Project Type

Determine what kind of project this is and which tools are available:

```bash
# Check for Python indicators
ls pyproject.toml setup.py setup.cfg requirements.txt Pipfile 2>/dev/null

# Check for Node indicators
ls package.json tsconfig.json 2>/dev/null

# Check for available tools
which ruff mypy pytest eslint tsc npx 2>/dev/null
```

Also check `pyproject.toml` or `package.json` for configured scripts/tools (e.g., `[tool.ruff]`, `[tool.mypy]`, `"scripts": { "test": ... }`).

## Phase 2 — Run Checks

Run checks **sequentially** in this order. For each step, capture both stdout and stderr.

### Python Projects

**Step 1 — Lint (ruff)**
```bash
ruff check . 2>&1
```
If ruff is not installed, skip and note it.

**Step 2 — Type Check (mypy)**
```bash
mypy . 2>&1
```
If mypy is not installed or not configured in `pyproject.toml`, skip and note it. Respect any mypy config in `pyproject.toml` or `mypy.ini`.

**Step 3 — Tests (pytest)**
```bash
pytest -v --tb=short 2>&1
```
If a specific test config exists in `pyproject.toml` (e.g., `[tool.pytest.ini_options]`), pytest will pick it up automatically.

Add `--cov` only if `pytest-cov` is installed and coverage is configured.

### Node.js Projects

**Step 1 — Lint (eslint)**
```bash
npx eslint . 2>&1
# or if package.json has a lint script:
npm run lint 2>&1
```

**Step 2 — Type Check (tsc)**
```bash
npx tsc --noEmit 2>&1
```
Only if `tsconfig.json` exists.

**Step 3 — Tests**
```bash
npm test 2>&1
```

### Mixed Projects

If both Python and Node indicators exist, run both suites and report separately.

## Phase 3 — Report

Present a concise summary:

```
## Quality Report

### Lint (ruff)
- Status: PASS / FAIL
- Issues: X errors, Y warnings
- [list specific issues if FAIL, max 10]

### Type Check (mypy)
- Status: PASS / FAIL / SKIPPED
- Errors: X
- [list specific errors if FAIL, max 10]

### Tests (pytest)
- Status: PASS / FAIL
- Results: X passed, Y failed, Z skipped
- Coverage: XX% (if available)
- [list failed tests if any]

### Summary
[Overall status — all green, or what needs fixing]
```

## Options

- If the user says "just lint" or "just ruff", only run the linting step.
- If the user says "just tests" or "just pytest", only run the test step.
- If the user says "just types" or "just mypy", only run type checking.
- If the user says "fix lint" or "fix linting", run `ruff check --fix .` instead.
- If the user says "check and fix", run checks first, then auto-fix what's possible with `ruff check --fix .`.

## Notes

- Never modify code during the check phase — only report. Fix only if the user explicitly asks.
- If tests fail, show the relevant failure output to help the user understand what broke.
- If a virtual environment exists (`venv/`, `.venv/`, `env/`), tools should already be available via PATH. Do NOT activate the venv — just run the commands directly.
- Respect any `.ruff.toml`, `ruff.toml`, or `[tool.ruff]` in `pyproject.toml` for ruff config.
