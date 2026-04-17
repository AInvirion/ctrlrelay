# dev-sync Phase 1: Checkpoint Protocol + Skill Audit Tool

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the checkpoint protocol for skill-orchestrator communication and a skill audit tool to verify orchestrator readiness.

**Architecture:** The checkpoint module provides helper functions (`done`, `blocked`, `failed`) that skills call to report their final state. State is written atomically to `$DEV_SYNC_STATE_FILE`. The skill audit tool scans SKILL.md files and their referenced code for orchestrator compliance patterns.

**Tech Stack:** Python 3.12, Pydantic, pytest, Rich

**Phase Gate:** `dev-sync skills audit` produces compliance report for existing skills.

---

## File Structure

```
src/dev_sync/
├── __init__.py              # Add checkpoint re-export
├── cli.py                   # Add skills subcommand group
└── core/
    ├── __init__.py          # Add checkpoint exports
    ├── checkpoint.py        # NEW: Checkpoint protocol helpers
    └── audit.py             # NEW: Skill audit logic
tests/
├── test_checkpoint.py       # NEW: Checkpoint tests
└── test_audit.py            # NEW: Audit tests
```

---

### Task 1: Create checkpoint Pydantic models

**Files:**
- Create: `src/dev_sync/core/checkpoint.py`
- Create: `tests/test_checkpoint.py`

- [ ] **Step 1: Write failing test for CheckpointState model**

Create `tests/test_checkpoint.py`:
```python
"""Tests for checkpoint protocol."""

from datetime import datetime, timezone

import pytest

from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus


class TestCheckpointState:
    def test_done_state_valid(self) -> None:
        """DONE state requires summary."""
        state = CheckpointState(
            status=CheckpointStatus.DONE,
            session_id="sess-123",
            summary="Merged 3 PRs",
        )
        assert state.status == CheckpointStatus.DONE
        assert state.summary == "Merged 3 PRs"
        assert state.version == "1"

    def test_blocked_state_requires_question(self) -> None:
        """BLOCKED_NEEDS_INPUT state requires question."""
        state = CheckpointState(
            status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
            session_id="sess-123",
            question="Which version?",
            question_context={"options": ["2.4.1", "2.5.0"]},
        )
        assert state.question == "Which version?"

    def test_failed_state_requires_error(self) -> None:
        """FAILED state requires error message."""
        state = CheckpointState(
            status=CheckpointStatus.FAILED,
            session_id="sess-123",
            error="gh CLI returned 404",
            recoverable=False,
        )
        assert state.error == "gh CLI returned 404"
        assert state.recoverable is False

    def test_timestamp_auto_generated(self) -> None:
        """Timestamp should be auto-generated if not provided."""
        state = CheckpointState(
            status=CheckpointStatus.DONE,
            session_id="sess-123",
            summary="Done",
        )
        assert state.timestamp is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/test_checkpoint.py -v
```
Expected: FAIL with "No module named 'dev_sync.core.checkpoint'"

- [ ] **Step 3: Write the checkpoint models**

Create `src/dev_sync/core/checkpoint.py`:
```python
"""Checkpoint protocol for skill-orchestrator communication."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CheckpointStatus(str, Enum):
    """Status values for checkpoint state."""

    DONE = "DONE"
    BLOCKED_NEEDS_INPUT = "BLOCKED_NEEDS_INPUT"
    FAILED = "FAILED"


class CheckpointState(BaseModel):
    """State written by skills to communicate with orchestrator."""

    version: str = "1"
    status: CheckpointStatus
    session_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    summary: str | None = None

    question: str | None = None
    question_context: dict[str, Any] | None = None

    error: str | None = None
    recoverable: bool = True

    outputs: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_checkpoint.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/checkpoint.py tests/test_checkpoint.py
git commit -m "feat: add checkpoint Pydantic models"
```

---

### Task 2: Add checkpoint helper functions

**Files:**
- Modify: `src/dev_sync/core/checkpoint.py`
- Modify: `tests/test_checkpoint.py`

- [ ] **Step 1: Write failing tests for helper functions**

Add to `tests/test_checkpoint.py`:
```python
import json
import os
from pathlib import Path


class TestCheckpointHelpers:
    def test_done_writes_state_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """done() should write DONE state to state file."""
        from dev_sync.core.checkpoint import done

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        done(summary="Completed task", outputs={"pr_url": "https://..."})

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["status"] == "DONE"
        assert data["summary"] == "Completed task"
        assert data["outputs"]["pr_url"] == "https://..."

    def test_blocked_writes_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """blocked() should write BLOCKED_NEEDS_INPUT state."""
        from dev_sync.core.checkpoint import blocked

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        blocked(
            question="Which version?",
            context={"options": ["2.4.1", "2.5.0"]},
        )

        data = json.loads(state_file.read_text())
        assert data["status"] == "BLOCKED_NEEDS_INPUT"
        assert data["question"] == "Which version?"

    def test_failed_writes_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """failed() should write FAILED state."""
        from dev_sync.core.checkpoint import failed

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        failed(error="Connection timeout", recoverable=True)

        data = json.loads(state_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"] == "Connection timeout"
        assert data["recoverable"] is True

    def test_atomic_write_uses_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checkpoint should write to .tmp then rename for atomicity."""
        from dev_sync.core.checkpoint import done

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        done(summary="Test")

        # Temp file should not exist after completion
        assert not (tmp_path / "state.json.tmp").exists()
        # Final file should exist
        assert state_file.exists()

    def test_missing_env_raises_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should raise if DEV_SYNC_STATE_FILE not set."""
        from dev_sync.core.checkpoint import CheckpointError, done

        monkeypatch.delenv("DEV_SYNC_STATE_FILE", raising=False)
        monkeypatch.delenv("DEV_SYNC_SESSION_ID", raising=False)

        with pytest.raises(CheckpointError, match="DEV_SYNC_STATE_FILE"):
            done(summary="Test")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_checkpoint.py::TestCheckpointHelpers -v
```
Expected: FAIL with "cannot import name 'done'"

- [ ] **Step 3: Implement helper functions**

Add to `src/dev_sync/core/checkpoint.py`:
```python
import json
import os
from pathlib import Path


class CheckpointError(Exception):
    """Raised when checkpoint operations fail."""


def _get_state_file() -> Path:
    """Get state file path from environment."""
    path = os.environ.get("DEV_SYNC_STATE_FILE")
    if not path:
        raise CheckpointError("DEV_SYNC_STATE_FILE environment variable not set")
    return Path(path)


def _get_session_id() -> str:
    """Get session ID from environment."""
    session_id = os.environ.get("DEV_SYNC_SESSION_ID")
    if not session_id:
        raise CheckpointError("DEV_SYNC_SESSION_ID environment variable not set")
    return session_id


def _write_checkpoint(state: CheckpointState) -> None:
    """Write checkpoint state atomically."""
    state_file = _get_state_file()
    temp_file = state_file.with_suffix(".json.tmp")

    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file first
    temp_file.write_text(state.model_dump_json(indent=2))

    # Atomic rename
    temp_file.rename(state_file)


def done(summary: str, outputs: dict[str, Any] | None = None) -> None:
    """Report successful completion.

    Args:
        summary: One-line human-readable result.
        outputs: Optional structured outputs (pr_url, merged_prs, etc).
    """
    state = CheckpointState(
        status=CheckpointStatus.DONE,
        session_id=_get_session_id(),
        summary=summary,
        outputs=outputs or {},
    )
    _write_checkpoint(state)


def blocked(question: str, context: dict[str, Any] | None = None) -> None:
    """Report blocked on human input.

    Args:
        question: Question to ask the human.
        context: Additional context (repo, pr, options, etc).
    """
    state = CheckpointState(
        status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
        session_id=_get_session_id(),
        question=question,
        question_context=context,
    )
    _write_checkpoint(state)


def failed(error: str, recoverable: bool = True) -> None:
    """Report failure.

    Args:
        error: Error message describing what failed.
        recoverable: Whether the operation can be retried.
    """
    state = CheckpointState(
        status=CheckpointStatus.FAILED,
        session_id=_get_session_id(),
        error=error,
        recoverable=recoverable,
    )
    _write_checkpoint(state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_checkpoint.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/checkpoint.py tests/test_checkpoint.py
git commit -m "feat: add checkpoint helper functions (done, blocked, failed)"
```

---

### Task 3: Add read_checkpoint for orchestrator

**Files:**
- Modify: `src/dev_sync/core/checkpoint.py`
- Modify: `tests/test_checkpoint.py`

- [ ] **Step 1: Write failing tests for read_checkpoint**

Add to `tests/test_checkpoint.py`:
```python
class TestReadCheckpoint:
    def test_read_valid_checkpoint(self, tmp_path: Path) -> None:
        """Should parse valid checkpoint file."""
        from dev_sync.core.checkpoint import CheckpointStatus, read_checkpoint

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "sess-123",
            "timestamp": "2026-04-17T12:00:00Z",
            "summary": "Completed",
            "outputs": {"pr": 42},
        }))

        state = read_checkpoint(state_file)
        assert state.status == CheckpointStatus.DONE
        assert state.summary == "Completed"
        assert state.outputs["pr"] == 42

    def test_read_deletes_file_after(self, tmp_path: Path) -> None:
        """Should delete checkpoint file after reading."""
        from dev_sync.core.checkpoint import read_checkpoint

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "sess-123",
            "timestamp": "2026-04-17T12:00:00Z",
            "summary": "Done",
        }))

        read_checkpoint(state_file, delete_after=True)
        assert not state_file.exists()

    def test_read_missing_file_raises(self, tmp_path: Path) -> None:
        """Should raise if file doesn't exist."""
        from dev_sync.core.checkpoint import CheckpointError, read_checkpoint

        with pytest.raises(CheckpointError, match="not found"):
            read_checkpoint(tmp_path / "missing.json")

    def test_read_invalid_json_raises(self, tmp_path: Path) -> None:
        """Should raise FAILED status for invalid JSON (truncation)."""
        from dev_sync.core.checkpoint import CheckpointError, read_checkpoint

        state_file = tmp_path / "state.json"
        state_file.write_text('{"status": "DONE", "session_id": "x", "truncated')

        with pytest.raises(CheckpointError, match="parse"):
            read_checkpoint(state_file)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_checkpoint.py::TestReadCheckpoint -v
```
Expected: FAIL with "cannot import name 'read_checkpoint'"

- [ ] **Step 3: Implement read_checkpoint**

Add to `src/dev_sync/core/checkpoint.py`:
```python
def read_checkpoint(path: Path, delete_after: bool = False) -> CheckpointState:
    """Read and parse a checkpoint file.

    Used by the orchestrator to read skill results.

    Args:
        path: Path to the checkpoint file.
        delete_after: If True, delete the file after reading.

    Returns:
        Parsed CheckpointState.

    Raises:
        CheckpointError: If file not found or invalid.
    """
    if not path.exists():
        raise CheckpointError(f"Checkpoint file not found: {path}")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise CheckpointError(f"Failed to parse checkpoint file: {e}") from e

    try:
        state = CheckpointState.model_validate(data)
    except Exception as e:
        raise CheckpointError(f"Invalid checkpoint data: {e}") from e

    if delete_after:
        path.unlink()

    return state
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_checkpoint.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/checkpoint.py tests/test_checkpoint.py
git commit -m "feat: add read_checkpoint for orchestrator"
```

---

### Task 4: Export checkpoint from package root

**Files:**
- Modify: `src/dev_sync/__init__.py`
- Modify: `src/dev_sync/core/__init__.py`

- [ ] **Step 1: Update core exports**

Edit `src/dev_sync/core/__init__.py`:
```python
"""Core functionality for dev-sync orchestrator."""

from dev_sync.core.checkpoint import (
    CheckpointError,
    CheckpointState,
    CheckpointStatus,
    blocked,
    done,
    failed,
    read_checkpoint,
)
from dev_sync.core.config import (
    Config,
    ConfigError,
    RepoConfig,
    load_config,
)
from dev_sync.core.state import StateDB

__all__ = [
    "CheckpointError",
    "CheckpointState",
    "CheckpointStatus",
    "Config",
    "ConfigError",
    "RepoConfig",
    "StateDB",
    "blocked",
    "done",
    "failed",
    "load_config",
    "read_checkpoint",
]
```

- [ ] **Step 2: Update package root exports**

Edit `src/dev_sync/__init__.py`:
```python
"""dev-sync: Local-first orchestrator for Claude Code."""

from dev_sync.core import checkpoint

__version__ = "0.1.0"

# Public API
__all__ = ["__version__", "checkpoint"]
```

- [ ] **Step 3: Verify imports work**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && python -c "from dev_sync import checkpoint; print(checkpoint.done)"
```
Expected: `<function done at ...>`

- [ ] **Step 4: Commit**

```bash
git add src/dev_sync/__init__.py src/dev_sync/core/__init__.py
git commit -m "feat: export checkpoint module from package root"
```

---

### Task 5: Create skill audit models

**Files:**
- Create: `src/dev_sync/core/audit.py`
- Create: `tests/test_audit.py`

- [ ] **Step 1: Write failing test for audit models**

Create `tests/test_audit.py`:
```python
"""Tests for skill audit functionality."""

from pathlib import Path

import pytest

from dev_sync.core.audit import AuditCheck, AuditResult, SkillAudit


class TestAuditModels:
    def test_audit_check_values(self) -> None:
        """AuditCheck should have all required check types."""
        assert AuditCheck.CHECKPOINT.value == "checkpoint"
        assert AuditCheck.HEADLESS.value == "headless"
        assert AuditCheck.CONTEXT_PATH.value == "context_path"
        assert AuditCheck.ATTRIBUTION.value == "attribution"

    def test_skill_audit_passed(self) -> None:
        """SkillAudit should calculate passed status."""
        audit = SkillAudit(
            name="test-skill",
            path=Path("/skills/test"),
            results={
                AuditCheck.CHECKPOINT: AuditResult(passed=True),
                AuditCheck.HEADLESS: AuditResult(passed=True),
                AuditCheck.CONTEXT_PATH: AuditResult(passed=True),
                AuditCheck.ATTRIBUTION: AuditResult(passed=True),
            },
        )
        assert audit.passed is True
        assert audit.status == "READY"

    def test_skill_audit_failed(self) -> None:
        """SkillAudit should report NOT READY if any check fails."""
        audit = SkillAudit(
            name="test-skill",
            path=Path("/skills/test"),
            results={
                AuditCheck.CHECKPOINT: AuditResult(passed=False, reason="No checkpoint calls"),
                AuditCheck.HEADLESS: AuditResult(passed=True),
                AuditCheck.CONTEXT_PATH: AuditResult(passed=True),
                AuditCheck.ATTRIBUTION: AuditResult(passed=True),
            },
        )
        assert audit.passed is False
        assert audit.status == "NOT READY"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_audit.py -v
```
Expected: FAIL with "No module named 'dev_sync.core.audit'"

- [ ] **Step 3: Implement audit models**

Create `src/dev_sync/core/audit.py`:
```python
"""Skill audit functionality for orchestrator readiness checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AuditCheck(str, Enum):
    """Types of orchestrator readiness checks."""

    CHECKPOINT = "checkpoint"
    HEADLESS = "headless"
    CONTEXT_PATH = "context_path"
    ATTRIBUTION = "attribution"


@dataclass
class AuditResult:
    """Result of a single audit check."""

    passed: bool
    reason: str = ""
    auto_fixable: bool = False


@dataclass
class SkillAudit:
    """Audit results for a single skill."""

    name: str
    path: Path
    results: dict[AuditCheck, AuditResult] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True if all checks passed."""
        return all(r.passed for r in self.results.values())

    @property
    def status(self) -> str:
        """Human-readable status."""
        return "READY" if self.passed else "NOT READY"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_audit.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/audit.py tests/test_audit.py
git commit -m "feat: add skill audit models"
```

---

### Task 6: Implement skill discovery

**Files:**
- Modify: `src/dev_sync/core/audit.py`
- Modify: `tests/test_audit.py`

- [ ] **Step 1: Write failing test for skill discovery**

Add to `tests/test_audit.py`:
```python
class TestSkillDiscovery:
    def test_discover_skills(self, tmp_path: Path) -> None:
        """Should find all SKILL.md files in directory."""
        from dev_sync.core.audit import discover_skills

        # Create mock skills
        skill1 = tmp_path / "skill-one" / "SKILL.md"
        skill1.parent.mkdir()
        skill1.write_text("---\nname: skill-one\n---\n# Skill One")

        skill2 = tmp_path / "skill-two" / "SKILL.md"
        skill2.parent.mkdir()
        skill2.write_text("---\nname: skill-two\n---\n# Skill Two")

        skills = discover_skills(tmp_path)
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"skill-one", "skill-two"}

    def test_discover_skills_empty_dir(self, tmp_path: Path) -> None:
        """Should return empty list if no skills found."""
        from dev_sync.core.audit import discover_skills

        skills = discover_skills(tmp_path)
        assert skills == []

    def test_discover_skills_parses_name(self, tmp_path: Path) -> None:
        """Should parse skill name from YAML frontmatter."""
        from dev_sync.core.audit import discover_skills

        skill = tmp_path / "my-skill" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: custom-name\ndescription: Test\n---\n# Content")

        skills = discover_skills(tmp_path)
        assert skills[0].name == "custom-name"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_audit.py::TestSkillDiscovery -v
```
Expected: FAIL with "cannot import name 'discover_skills'"

- [ ] **Step 3: Implement discover_skills**

Add to `src/dev_sync/core/audit.py`:
```python
import re
import yaml


@dataclass
class SkillInfo:
    """Basic skill information from SKILL.md."""

    name: str
    path: Path
    content: str
    frontmatter: dict


def discover_skills(skills_dir: Path) -> list[SkillInfo]:
    """Discover all skills in a directory.

    Args:
        skills_dir: Path to skills directory.

    Returns:
        List of SkillInfo for each skill found.
    """
    skills = []

    if not skills_dir.exists():
        return skills

    for skill_md in skills_dir.glob("*/SKILL.md"):
        content = skill_md.read_text()

        # Parse YAML frontmatter
        frontmatter = {}
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if match:
            try:
                frontmatter = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError:
                pass

        name = frontmatter.get("name", skill_md.parent.name)
        skills.append(SkillInfo(
            name=name,
            path=skill_md.parent,
            content=content,
            frontmatter=frontmatter,
        ))

    return sorted(skills, key=lambda s: s.name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_audit.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/audit.py tests/test_audit.py
git commit -m "feat: add skill discovery from SKILL.md files"
```

---

### Task 7: Implement audit checks

**Files:**
- Modify: `src/dev_sync/core/audit.py`
- Modify: `tests/test_audit.py`

- [ ] **Step 1: Write failing tests for audit checks**

Add to `tests/test_audit.py`:
```python
class TestAuditChecks:
    def test_check_checkpoint_passes_with_import(self, tmp_path: Path) -> None:
        """Should pass if skill imports checkpoint module."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\n```python\nfrom dev_sync import checkpoint\ncheckpoint.done()\n```",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.CHECKPOINT)
        assert result.passed is True

    def test_check_checkpoint_fails_without(self, tmp_path: Path) -> None:
        """Should fail if skill has no checkpoint references."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nDo some work.",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.CHECKPOINT)
        assert result.passed is False

    def test_check_headless_passes_without_input(self, tmp_path: Path) -> None:
        """Should pass if skill has no interactive prompts."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nRun commands.",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.HEADLESS)
        assert result.passed is True

    def test_check_headless_fails_with_input(self, tmp_path: Path) -> None:
        """Should fail if skill uses input()."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\n```python\nuser_input = input('Enter value:')\n```",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.HEADLESS)
        assert result.passed is False

    def test_check_headless_fails_with_playwright(self, tmp_path: Path) -> None:
        """Should fail if skill uses playwright MCP without fallback."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nUse mcp__playwright__navigate to browse.",
            frontmatter={"tools": "mcp__playwright__navigate"},
        )
        result = run_check(skill, AuditCheck.HEADLESS)
        assert result.passed is False

    def test_check_attribution_passes_clean(self, tmp_path: Path) -> None:
        """Should pass if no Claude/Anthropic in output."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nDo work and report results.",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.ATTRIBUTION)
        assert result.passed is True

    def test_check_attribution_fails_with_claude(self, tmp_path: Path) -> None:
        """Should fail if output mentions Claude."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content='# Skill\n\nPrint "Generated by Claude"',
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.ATTRIBUTION)
        assert result.passed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_audit.py::TestAuditChecks -v
```
Expected: FAIL with "cannot import name 'run_check'"

- [ ] **Step 3: Implement run_check**

Add to `src/dev_sync/core/audit.py`:
```python
# Patterns for headless check
INTERACTIVE_PATTERNS = [
    r"\binput\s*\(",
    r"\bread\s+-p\b",
    r"Confirm\s*\(",
    r"typer\.confirm\s*\(",
]

BROWSER_ONLY_TOOLS = [
    "mcp__playwright__",
    "mcp__chrome_devtools__",
]

# Patterns for checkpoint check
CHECKPOINT_PATTERNS = [
    r"from\s+dev_sync\s+import\s+checkpoint",
    r"from\s+dev_sync\.core\.checkpoint\s+import",
    r"checkpoint\.(done|blocked|failed)\s*\(",
    r"DEV_SYNC_STATE_FILE",
]

# Attribution patterns to avoid in output
ATTRIBUTION_PATTERNS = [
    r"\bClaude\b",
    r"\bAnthropic\b",
    r"Generated by AI",
    r"AI Assistant",
]


def run_check(skill: SkillInfo, check: AuditCheck) -> AuditResult:
    """Run a single audit check on a skill.

    Args:
        skill: Skill information.
        check: Type of check to run.

    Returns:
        AuditResult with pass/fail and reason.
    """
    content = skill.content
    tools = skill.frontmatter.get("tools", "")

    if check == AuditCheck.CHECKPOINT:
        for pattern in CHECKPOINT_PATTERNS:
            if re.search(pattern, content):
                return AuditResult(passed=True)
        return AuditResult(
            passed=False,
            reason="No checkpoint protocol usage found",
        )

    if check == AuditCheck.HEADLESS:
        # Check for interactive prompts
        for pattern in INTERACTIVE_PATTERNS:
            if re.search(pattern, content):
                return AuditResult(
                    passed=False,
                    reason=f"Interactive prompt pattern found: {pattern}",
                )
        # Check for browser-only tools
        for tool in BROWSER_ONLY_TOOLS:
            if tool in content or tool in tools:
                # Check for fallback mention
                if "fallback" not in content.lower() and "cli" not in content.lower():
                    return AuditResult(
                        passed=False,
                        reason=f"Browser-only tool without fallback: {tool}",
                    )
        return AuditResult(passed=True)

    if check == AuditCheck.CONTEXT_PATH:
        # Check for hardcoded paths vs REPO_CONTEXT_PATH
        if "REPO_CONTEXT_PATH" in content or "$REPO_CONTEXT_PATH" in content:
            return AuditResult(passed=True)
        # If skill mentions context at all, it should use the env var
        if "context" in content.lower() and "/" in content:
            return AuditResult(
                passed=False,
                reason="May use hardcoded context path instead of $REPO_CONTEXT_PATH",
                auto_fixable=True,
            )
        return AuditResult(passed=True)

    if check == AuditCheck.ATTRIBUTION:
        for pattern in ATTRIBUTION_PATTERNS:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return AuditResult(
                    passed=False,
                    reason=f"Attribution pattern found: {match.group()}",
                    auto_fixable=True,
                )
        return AuditResult(passed=True)

    return AuditResult(passed=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_audit.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/audit.py tests/test_audit.py
git commit -m "feat: implement skill audit checks"
```

---

### Task 8: Implement audit_skill and audit_all

**Files:**
- Modify: `src/dev_sync/core/audit.py`
- Modify: `tests/test_audit.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_audit.py`:
```python
class TestAuditFunctions:
    def test_audit_skill(self, tmp_path: Path) -> None:
        """audit_skill should run all checks on a skill."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, audit_skill

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\n```python\nfrom dev_sync import checkpoint\ncheckpoint.done()\n```",
            frontmatter={},
        )

        result = audit_skill(skill)
        assert result.name == "test"
        assert AuditCheck.CHECKPOINT in result.results
        assert AuditCheck.HEADLESS in result.results

    def test_audit_all(self, tmp_path: Path) -> None:
        """audit_all should audit all skills in directory."""
        from dev_sync.core.audit import audit_all

        # Create skills
        skill1 = tmp_path / "skill-one" / "SKILL.md"
        skill1.parent.mkdir()
        skill1.write_text("---\nname: skill-one\n---\n# Ready\n\nfrom dev_sync import checkpoint")

        skill2 = tmp_path / "skill-two" / "SKILL.md"
        skill2.parent.mkdir()
        skill2.write_text("---\nname: skill-two\n---\n# Not ready")

        results = audit_all(tmp_path)
        assert len(results) == 2

        by_name = {r.name: r for r in results}
        assert by_name["skill-one"].results[AuditCheck.CHECKPOINT].passed is True
        assert by_name["skill-two"].results[AuditCheck.CHECKPOINT].passed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_audit.py::TestAuditFunctions -v
```
Expected: FAIL with "cannot import name 'audit_skill'"

- [ ] **Step 3: Implement audit functions**

Add to `src/dev_sync/core/audit.py`:
```python
def audit_skill(skill: SkillInfo) -> SkillAudit:
    """Run all audit checks on a skill.

    Args:
        skill: Skill information.

    Returns:
        SkillAudit with all check results.
    """
    results = {}
    for check in AuditCheck:
        results[check] = run_check(skill, check)

    return SkillAudit(
        name=skill.name,
        path=skill.path,
        results=results,
    )


def audit_all(skills_dir: Path) -> list[SkillAudit]:
    """Audit all skills in a directory.

    Args:
        skills_dir: Path to skills directory.

    Returns:
        List of SkillAudit results.
    """
    skills = discover_skills(skills_dir)
    return [audit_skill(skill) for skill in skills]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_audit.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/audit.py tests/test_audit.py
git commit -m "feat: add audit_skill and audit_all functions"
```

---

### Task 9: Add format_report for markdown output

**Files:**
- Modify: `src/dev_sync/core/audit.py`
- Modify: `tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_audit.py`:
```python
class TestAuditReport:
    def test_format_report(self) -> None:
        """format_report should generate markdown table."""
        from dev_sync.core.audit import (
            AuditCheck,
            AuditResult,
            SkillAudit,
            format_report,
        )

        audits = [
            SkillAudit(
                name="skill-one",
                path=Path("/skills/one"),
                results={
                    AuditCheck.CHECKPOINT: AuditResult(passed=True),
                    AuditCheck.HEADLESS: AuditResult(passed=True),
                    AuditCheck.CONTEXT_PATH: AuditResult(passed=True),
                    AuditCheck.ATTRIBUTION: AuditResult(passed=True),
                },
            ),
            SkillAudit(
                name="skill-two",
                path=Path("/skills/two"),
                results={
                    AuditCheck.CHECKPOINT: AuditResult(passed=False, reason="Missing"),
                    AuditCheck.HEADLESS: AuditResult(passed=True),
                    AuditCheck.CONTEXT_PATH: AuditResult(passed=False, reason="Hardcoded"),
                    AuditCheck.ATTRIBUTION: AuditResult(passed=True),
                },
            ),
        ]

        report = format_report(audits)
        assert "## Skill Audit Report" in report
        assert "skill-one" in report
        assert "skill-two" in report
        assert "READY" in report
        assert "NOT READY" in report
        assert "| Skill |" in report
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_audit.py::TestAuditReport -v
```
Expected: FAIL with "cannot import name 'format_report'"

- [ ] **Step 3: Implement format_report**

Add to `src/dev_sync/core/audit.py`:
```python
def format_report(audits: list[SkillAudit]) -> str:
    """Format audit results as markdown report.

    Args:
        audits: List of skill audit results.

    Returns:
        Markdown formatted report.
    """
    lines = [
        "## Skill Audit Report",
        "",
        "| Skill | Checkpoint | Headless | Context | Attribution | Status |",
        "|-------|------------|----------|---------|-------------|--------|",
    ]

    for audit in audits:
        def icon(check: AuditCheck) -> str:
            result = audit.results.get(check)
            if result is None:
                return "➖"
            return "✅" if result.passed else "❌"

        lines.append(
            f"| {audit.name} "
            f"| {icon(AuditCheck.CHECKPOINT)} "
            f"| {icon(AuditCheck.HEADLESS)} "
            f"| {icon(AuditCheck.CONTEXT_PATH)} "
            f"| {icon(AuditCheck.ATTRIBUTION)} "
            f"| {audit.status} |"
        )

    # Summary
    ready = sum(1 for a in audits if a.passed)
    total = len(audits)
    lines.extend([
        "",
        f"**Summary:** {ready}/{total} skills ready for orchestrator",
    ])

    # Details for failed checks
    failed_audits = [a for a in audits if not a.passed]
    if failed_audits:
        lines.extend(["", "### Issues", ""])
        for audit in failed_audits:
            lines.append(f"**{audit.name}:**")
            for check, result in audit.results.items():
                if not result.passed:
                    fixable = " (auto-fixable)" if result.auto_fixable else ""
                    lines.append(f"- {check.value}: {result.reason}{fixable}")
            lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_audit.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/core/audit.py tests/test_audit.py
git commit -m "feat: add markdown report formatting for skill audit"
```

---

### Task 10: Add skills CLI subcommand

**Files:**
- Modify: `src/dev_sync/cli.py`

- [ ] **Step 1: Add skills subcommand group and audit command**

Add to `src/dev_sync/cli.py` after the config_app section:
```python
# Skills subcommand group
skills_app = typer.Typer(help="Skill management commands.")
app.add_typer(skills_app, name="skills")


@skills_app.command("audit")
def skills_audit(
    skills_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to skills directory (default: from config)",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Audit skills for orchestrator readiness."""
    from dev_sync.core.audit import audit_all, format_report

    # Get skills path from config if not provided
    if skills_path is None:
        try:
            config = load_config(config_path)
            skills_dir = config.paths.skills
        except ConfigError as e:
            console.print(f"[red]Error loading config:[/red] {e}")
            console.print("Use --path to specify skills directory directly.")
            raise typer.Exit(1)
    else:
        skills_dir = Path(skills_path).expanduser()

    if not skills_dir.exists():
        console.print(f"[red]Skills directory not found:[/red] {skills_dir}")
        raise typer.Exit(1)

    console.print(f"Auditing skills in: {skills_dir}\n")

    audits = audit_all(skills_dir)

    if not audits:
        console.print("[yellow]No skills found.[/yellow]")
        return

    report = format_report(audits)
    console.print(report)

    # Exit with error if any skills not ready
    if not all(a.passed for a in audits):
        raise typer.Exit(1)


@skills_app.command("list")
def skills_list(
    skills_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to skills directory (default: from config)",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """List available skills."""
    from dev_sync.core.audit import discover_skills

    if skills_path is None:
        try:
            config = load_config(config_path)
            skills_dir = config.paths.skills
        except ConfigError as e:
            console.print(f"[red]Error loading config:[/red] {e}")
            raise typer.Exit(1)
    else:
        skills_dir = Path(skills_path).expanduser()

    skills = discover_skills(skills_dir)

    if not skills:
        console.print("[yellow]No skills found.[/yellow]")
        return

    table = Table(title="Available Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Path", style="dim")

    for skill in skills:
        table.add_row(skill.name, str(skill.path))

    console.print(table)
```

- [ ] **Step 2: Test CLI help**

Run:
```bash
dev-sync skills --help
```
Expected: Shows audit and list subcommands

- [ ] **Step 3: Test skills audit**

Run:
```bash
dev-sync skills audit --path claude-config/skills
```
Expected: Shows audit report with skill statuses

- [ ] **Step 4: Test skills list**

Run:
```bash
dev-sync skills list --path claude-config/skills
```
Expected: Shows table of skills

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/cli.py
git commit -m "feat: add skills audit and list CLI commands"
```

---

### Task 11: Run full test suite and verify phase gate

**Files:** None (verification only)

- [ ] **Step 1: Run all tests**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/ -v --cov=dev_sync
```
Expected: All tests pass

- [ ] **Step 2: Run linter**

Run:
```bash
ruff check src/ tests/
```
Expected: No errors

- [ ] **Step 3: Verify phase gate - audit produces report**

Run:
```bash
dev-sync skills audit --path claude-config/skills
```
Expected: Markdown report showing skill compliance statuses

- [ ] **Step 4: Commit final state**

```bash
git add -A
git status
# If any uncommitted changes:
git commit -m "chore: phase 1 complete - checkpoint protocol and skill audit"
```

---

## Phase Gate Verification

**Phase 1 is complete when:**

1. ✅ `from dev_sync import checkpoint` works
2. ✅ `checkpoint.done()`, `checkpoint.blocked()`, `checkpoint.failed()` write state files
3. ✅ `dev-sync skills audit` produces compliance report
4. ✅ All tests pass
5. ✅ No linter errors

**Next:** Phase 2 - Telegram Bridge
