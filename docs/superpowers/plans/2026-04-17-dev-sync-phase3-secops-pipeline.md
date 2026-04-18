---
title: Phase 3 — Secops Pipeline
layout: default
parent: Plans
nav_order: 4
---

# Phase 3: Secops Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the core execution infrastructure (dispatcher, worktree, github CLI wrapper) and secops pipeline that runs daily across configured repos, with dashboard event logging.

**Architecture:** The dispatcher spawns `claude -p` subprocesses with environment variables for checkpoint protocol. Worktree manager creates isolated git worktrees for each session. GitHub wrapper provides typed access to `gh` CLI. Secops pipeline orchestrates the full flow: acquire locks, create worktree, symlink context, run claude, parse results, push events.

**Tech Stack:** Python 3.12, asyncio subprocess, git worktree, gh CLI, httpx for dashboard client, pydantic for models

---

## File Structure

```
src/dev_sync/
├── core/
│   ├── dispatcher.py      # NEW: claude -p subprocess manager
│   ├── worktree.py        # NEW: git worktree management
│   └── github.py          # NEW: gh CLI wrapper
├── pipelines/
│   ├── __init__.py        # NEW: pipeline exports
│   ├── base.py            # NEW: Pipeline protocol
│   └── secops.py          # NEW: secops pipeline implementation
└── dashboard/
    ├── __init__.py        # NEW: dashboard exports
    └── client.py          # NEW: event push with offline queue

tests/
├── test_dispatcher.py     # NEW
├── test_worktree.py       # NEW
├── test_github.py         # NEW
├── test_secops_pipeline.py # NEW
└── test_dashboard_client.py # NEW
```

---

### Task 1: GitHub CLI Wrapper

**Files:**
- Create: `src/dev_sync/core/github.py`
- Test: `tests/test_github.py`

- [ ] **Step 1: Write failing test for list_prs**

```python
# tests/test_github.py
"""Tests for GitHub CLI wrapper."""

import json
from unittest.mock import AsyncMock, patch

import pytest


class TestGitHubCLI:
    @pytest.mark.asyncio
    async def test_list_prs_returns_parsed_json(self) -> None:
        """Should parse gh pr list JSON output."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 1, "title": "Bump requests", "author": {"login": "dependabot[bot]"}},
            {"number": 2, "title": "Fix bug", "author": {"login": "user"}},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            prs = await gh.list_prs("owner/repo", state="open")

            assert len(prs) == 2
            assert prs[0]["number"] == 1
            mock_run.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_github.py::TestGitHubCLI::test_list_prs_returns_parsed_json -v`
Expected: FAIL with "No module named 'dev_sync.core.github'"

- [ ] **Step 3: Write minimal implementation**

```python
# src/dev_sync/core/github.py
"""GitHub CLI (gh) wrapper for dev-sync."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


class GitHubError(Exception):
    """Raised when gh CLI operations fail."""


@dataclass
class GitHubCLI:
    """Async wrapper around the gh CLI."""

    gh_binary: str = "gh"
    timeout: int = 60

    async def _run_gh(self, *args: str) -> str:
        """Run gh command and return stdout."""
        cmd = [self.gh_binary, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.timeout
        )

        if proc.returncode != 0:
            raise GitHubError(f"gh failed: {stderr.decode().strip()}")

        return stdout.decode()

    async def list_prs(
        self,
        repo: str,
        state: str = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List pull requests for a repository."""
        output = await self._run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", state,
            "--limit", str(limit),
            "--json", "number,title,author,labels,headRefName,mergeable,reviewDecision",
        )
        return json.loads(output) if output.strip() else []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_github.py::TestGitHubCLI::test_list_prs_returns_parsed_json -v`
Expected: PASS

- [ ] **Step 5: Write failing test for list_security_alerts**

```python
    @pytest.mark.asyncio
    async def test_list_security_alerts(self) -> None:
        """Should fetch Dependabot alerts."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 1, "state": "open", "dependency": {"package": {"name": "lodash"}}},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            alerts = await gh.list_security_alerts("owner/repo")

            assert len(alerts) == 1
            assert alerts[0]["dependency"]["package"]["name"] == "lodash"
```

- [ ] **Step 6: Implement list_security_alerts**

Add to `src/dev_sync/core/github.py`:

```python
    async def list_security_alerts(
        self,
        repo: str,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        """List Dependabot security alerts."""
        output = await self._run_gh(
            "api",
            f"/repos/{repo}/dependabot/alerts",
            "--jq", f'[.[] | select(.state == "{state}")]',
        )
        return json.loads(output) if output.strip() else []
```

- [ ] **Step 7: Write failing test for merge_pr**

```python
    @pytest.mark.asyncio
    async def test_merge_pr(self) -> None:
        """Should merge PR with squash."""
        from dev_sync.core.github import GitHubCLI

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.merge_pr("owner/repo", 42, method="squash")

            mock_run.assert_called_once()
            args = mock_run.call_args[0]
            assert "merge" in args
            assert "--squash" in args
```

- [ ] **Step 8: Implement merge_pr**

Add to `src/dev_sync/core/github.py`:

```python
    async def merge_pr(
        self,
        repo: str,
        pr_number: int,
        method: str = "squash",
    ) -> None:
        """Merge a pull request."""
        merge_flag = f"--{method}"
        await self._run_gh(
            "pr", "merge",
            str(pr_number),
            "--repo", repo,
            merge_flag,
            "--delete-branch",
        )
```

- [ ] **Step 9: Write failing test for get_pr_checks**

```python
    @pytest.mark.asyncio
    async def test_get_pr_checks(self) -> None:
        """Should get PR check status."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"name": "tests", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

            assert len(checks) == 2
            assert all(c["conclusion"] == "success" for c in checks)
```

- [ ] **Step 10: Implement get_pr_checks**

Add to `src/dev_sync/core/github.py`:

```python
    async def get_pr_checks(
        self,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        """Get status checks for a PR."""
        output = await self._run_gh(
            "pr", "checks",
            str(pr_number),
            "--repo", repo,
            "--json", "name,status,conclusion",
        )
        return json.loads(output) if output.strip() else []

    def all_checks_passed(self, checks: list[dict[str, Any]]) -> bool:
        """Check if all PR checks passed."""
        if not checks:
            return False
        return all(
            c.get("status") == "completed" and c.get("conclusion") == "success"
            for c in checks
        )
```

- [ ] **Step 11: Run all tests**

Run: `pytest tests/test_github.py -v`
Expected: All tests PASS

- [ ] **Step 12: Commit**

```bash
git add src/dev_sync/core/github.py tests/test_github.py
git commit -m "$(cat <<'EOF'
feat(core): add GitHub CLI wrapper

Async wrapper around gh CLI for PR listing, merging, security alerts,
and check status. Used by secops pipeline for automated triage.
EOF
)"
```

---

### Task 2: Git Worktree Manager

**Files:**
- Create: `src/dev_sync/core/worktree.py`
- Test: `tests/test_worktree.py`

- [ ] **Step 1: Write failing test for create_worktree**

```python
# tests/test_worktree.py
"""Tests for git worktree management."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestWorktreeManager:
    @pytest.mark.asyncio
    async def test_create_worktree(self, tmp_path: Path) -> None:
        """Should create worktree with correct paths."""
        from dev_sync.core.worktree import WorktreeManager

        worktrees_dir = tmp_path / "worktrees"
        bare_repos_dir = tmp_path / "repos"

        manager = WorktreeManager(
            worktrees_dir=worktrees_dir,
            bare_repos_dir=bare_repos_dir,
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            worktree_path = await manager.create_worktree(
                repo="owner/repo",
                session_id="sess-123",
                branch="main",
            )

            assert worktree_path.parent == worktrees_dir
            assert "repo" in str(worktree_path)
            assert "sess-123" in str(worktree_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worktree.py::TestWorktreeManager::test_create_worktree -v`
Expected: FAIL with "No module named 'dev_sync.core.worktree'"

- [ ] **Step 3: Write minimal implementation**

```python
# src/dev_sync/core/worktree.py
"""Git worktree management for isolated sessions."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(Exception):
    """Raised when worktree operations fail."""


@dataclass
class WorktreeManager:
    """Manages git worktrees for session isolation."""

    worktrees_dir: Path
    bare_repos_dir: Path
    timeout: int = 120

    def __post_init__(self) -> None:
        self.worktrees_dir = Path(self.worktrees_dir)
        self.bare_repos_dir = Path(self.bare_repos_dir)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.bare_repos_dir.mkdir(parents=True, exist_ok=True)

    async def _run_git(self, *args: str, cwd: Path | None = None) -> str:
        """Run git command and return stdout."""
        cmd = ["git", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.timeout
        )

        if proc.returncode != 0:
            raise WorktreeError(f"git failed: {stderr.decode().strip()}")

        return stdout.decode()

    def _get_bare_repo_path(self, repo: str) -> Path:
        """Get path to bare repo clone."""
        repo_name = repo.replace("/", "-")
        return self.bare_repos_dir / f"{repo_name}.git"

    def _get_worktree_path(self, repo: str, session_id: str) -> Path:
        """Get path for a worktree."""
        repo_name = repo.replace("/", "-")
        return self.worktrees_dir / f"{repo_name}-{session_id}"

    async def create_worktree(
        self,
        repo: str,
        session_id: str,
        branch: str = "main",
    ) -> Path:
        """Create a worktree for a session."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        await self._run_git(
            "worktree", "add",
            str(worktree_path),
            branch,
            cwd=bare_path,
        )

        return worktree_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worktree.py::TestWorktreeManager::test_create_worktree -v`
Expected: PASS

- [ ] **Step 5: Write failing test for ensure_bare_repo**

```python
    @pytest.mark.asyncio
    async def test_ensure_bare_repo_clones_if_missing(self, tmp_path: Path) -> None:
        """Should clone bare repo if it doesn't exist."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            await manager.ensure_bare_repo("owner/repo")

            # Should have called git clone --bare
            calls = [str(c) for c in mock_git.call_args_list]
            assert any("clone" in str(c) and "--bare" in str(c) for c in calls)
```

- [ ] **Step 6: Implement ensure_bare_repo**

Add to `src/dev_sync/core/worktree.py`:

```python
    async def ensure_bare_repo(self, repo: str) -> Path:
        """Ensure bare repo exists, cloning if needed."""
        bare_path = self._get_bare_repo_path(repo)

        if bare_path.exists():
            await self._run_git("fetch", "--all", cwd=bare_path)
        else:
            await self._run_git(
                "clone", "--bare",
                f"https://github.com/{repo}.git",
                str(bare_path),
            )

        return bare_path
```

- [ ] **Step 7: Write failing test for remove_worktree**

```python
    @pytest.mark.asyncio
    async def test_remove_worktree(self, tmp_path: Path) -> None:
        """Should remove worktree and prune."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        worktree_path = manager._get_worktree_path("owner/repo", "sess-123")
        worktree_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            await manager.remove_worktree("owner/repo", "sess-123")

            assert not worktree_path.exists()
            calls = [str(c) for c in mock_git.call_args_list]
            assert any("worktree" in str(c) and "prune" in str(c) for c in calls)
```

- [ ] **Step 8: Implement remove_worktree**

Add to `src/dev_sync/core/worktree.py`:

```python
    async def remove_worktree(self, repo: str, session_id: str) -> None:
        """Remove a worktree and clean up."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        if bare_path.exists():
            await self._run_git("worktree", "prune", cwd=bare_path)
```

- [ ] **Step 9: Write failing test for symlink_context**

```python
    @pytest.mark.asyncio
    async def test_symlink_context(self, tmp_path: Path) -> None:
        """Should symlink CLAUDE.md into worktree."""
        from dev_sync.core.worktree import WorktreeManager

        contexts_dir = tmp_path / "contexts"
        context_file = contexts_dir / "owner-repo" / "CLAUDE.md"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("# Context")

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        manager.symlink_context(
            worktree_path=worktree_path,
            context_path=context_file,
        )

        link = worktree_path / "CLAUDE.md"
        assert link.is_symlink()
        assert link.resolve() == context_file.resolve()
```

- [ ] **Step 10: Implement symlink_context**

Add to `src/dev_sync/core/worktree.py`:

```python
    def symlink_context(
        self,
        worktree_path: Path,
        context_path: Path,
    ) -> None:
        """Symlink CLAUDE.md into worktree."""
        target = worktree_path / "CLAUDE.md"

        if target.exists() or target.is_symlink():
            target.unlink()

        target.symlink_to(context_path.resolve())

        exclude_file = worktree_path / ".git" / "info" / "exclude"
        if exclude_file.exists():
            content = exclude_file.read_text()
            if "CLAUDE.md" not in content:
                exclude_file.write_text(content.rstrip() + "\nCLAUDE.md\n")

    def remove_context_symlink(self, worktree_path: Path) -> None:
        """Remove CLAUDE.md symlink before git operations."""
        target = worktree_path / "CLAUDE.md"
        if target.is_symlink():
            target.unlink()
```

- [ ] **Step 11: Run all tests**

Run: `pytest tests/test_worktree.py -v`
Expected: All tests PASS

- [ ] **Step 12: Commit**

```bash
git add src/dev_sync/core/worktree.py tests/test_worktree.py
git commit -m "$(cat <<'EOF'
feat(core): add git worktree manager

Manages isolated worktrees for session execution. Handles bare repo
cloning, worktree creation/removal, and CLAUDE.md context symlinking.
EOF
)"
```

---

### Task 3: Claude Dispatcher

**Files:**
- Create: `src/dev_sync/core/dispatcher.py`
- Test: `tests/test_dispatcher.py`

- [ ] **Step 1: Write failing test for spawn_session**

```python
# tests/test_dispatcher.py
"""Tests for Claude dispatcher."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestClaudeDispatcher:
    @pytest.mark.asyncio
    async def test_spawn_session_sets_env_vars(self, tmp_path: Path) -> None:
        """Should set DEV_SYNC env vars for checkpoint protocol."""
        from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            state_file = tmp_path / "state.json"
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": "test-123",
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "Test completed",
            }))

            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test prompt",
                working_dir=tmp_path,
                state_file=state_file,
            )

            call_kwargs = mock_exec.call_args.kwargs
            env = call_kwargs.get("env", {})
            assert "DEV_SYNC_SESSION_ID" in env
            assert "DEV_SYNC_STATE_FILE" in env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dispatcher.py::TestClaudeDispatcher::test_spawn_session_sets_env_vars -v`
Expected: FAIL with "No module named 'dev_sync.core.dispatcher'"

- [ ] **Step 3: Write minimal implementation**

```python
# src/dev_sync/core/dispatcher.py
"""Claude subprocess dispatcher for dev-sync."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint


@dataclass
class SessionResult:
    """Result of a Claude session."""

    session_id: str
    exit_code: int
    state: CheckpointState | None
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.state is not None and self.state.status == CheckpointStatus.DONE

    @property
    def blocked(self) -> bool:
        return (
            self.state is not None
            and self.state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT
        )

    @property
    def failed(self) -> bool:
        return self.state is None or self.state.status == CheckpointStatus.FAILED


@dataclass
class ClaudeDispatcher:
    """Spawns and manages Claude subprocess sessions."""

    claude_binary: str = "claude"
    default_timeout: int = 1800
    extra_env: dict[str, str] = field(default_factory=dict)

    async def spawn_session(
        self,
        session_id: str,
        prompt: str,
        working_dir: Path,
        state_file: Path,
        timeout: int | None = None,
        resume_session_id: str | None = None,
    ) -> SessionResult:
        """Spawn a Claude session and wait for completion."""
        timeout = timeout or self.default_timeout

        env = os.environ.copy()
        env.update(self.extra_env)
        env["DEV_SYNC_SESSION_ID"] = session_id
        env["DEV_SYNC_STATE_FILE"] = str(state_file)

        cmd = [self.claude_binary, "-p", prompt, "--output-format", "json"]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=working_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SessionResult(
                session_id=session_id,
                exit_code=-1,
                state=None,
                stderr="Session timed out",
            )

        state = None
        if state_file.exists():
            try:
                state = read_checkpoint(state_file, delete_after=True)
            except Exception:
                pass

        return SessionResult(
            session_id=session_id,
            exit_code=proc.returncode or 0,
            state=state,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dispatcher.py::TestClaudeDispatcher::test_spawn_session_sets_env_vars -v`
Expected: PASS

- [ ] **Step 5: Write failing test for timeout handling**

```python
    @pytest.mark.asyncio
    async def test_spawn_session_handles_timeout(self, tmp_path: Path) -> None:
        """Should kill process on timeout."""
        from dev_sync.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude", default_timeout=1)

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                timeout=1,
            )

            assert result.exit_code == -1
            assert "timed out" in result.stderr
            mock_proc.kill.assert_called_once()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_dispatcher.py::TestClaudeDispatcher::test_spawn_session_handles_timeout -v`
Expected: PASS (already implemented)

- [ ] **Step 7: Write failing test for parsing checkpoint state**

```python
    @pytest.mark.asyncio
    async def test_spawn_session_parses_done_state(self, tmp_path: Path) -> None:
        """Should parse DONE checkpoint state."""
        from dev_sync.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b'{"result": "ok"}', b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "test-123",
            "timestamp": "2026-04-17T12:00:00Z",
            "summary": "Merged 3 PRs",
            "outputs": {"merged_prs": [1, 2, 3]},
        }))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test",
                working_dir=tmp_path,
                state_file=state_file,
            )

            assert result.success
            assert result.state is not None
            assert result.state.summary == "Merged 3 PRs"
            assert result.state.outputs["merged_prs"] == [1, 2, 3]
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_dispatcher.py::TestClaudeDispatcher::test_spawn_session_parses_done_state -v`
Expected: PASS (already implemented)

- [ ] **Step 9: Write failing test for blocked state**

```python
    @pytest.mark.asyncio
    async def test_spawn_session_parses_blocked_state(self, tmp_path: Path) -> None:
        """Should parse BLOCKED_NEEDS_INPUT checkpoint state."""
        from dev_sync.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "BLOCKED_NEEDS_INPUT",
            "session_id": "test-123",
            "timestamp": "2026-04-17T12:00:00Z",
            "question": "Pin to 2.4.1 or bump to 2.5.0?",
            "question_context": {"pr": 42},
        }))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test",
                working_dir=tmp_path,
                state_file=state_file,
            )

            assert result.blocked
            assert result.state is not None
            assert "2.4.1" in result.state.question
```

- [ ] **Step 10: Run all tests**

Run: `pytest tests/test_dispatcher.py -v`
Expected: All tests PASS

- [ ] **Step 11: Commit**

```bash
git add src/dev_sync/core/dispatcher.py tests/test_dispatcher.py
git commit -m "$(cat <<'EOF'
feat(core): add Claude subprocess dispatcher

Spawns claude -p with checkpoint protocol env vars. Handles timeout,
parses checkpoint state file, returns structured SessionResult.
EOF
)"
```

---

### Task 4: Dashboard Client with Offline Queue

**Files:**
- Create: `src/dev_sync/dashboard/__init__.py`
- Create: `src/dev_sync/dashboard/client.py`
- Test: `tests/test_dashboard_client.py`

- [ ] **Step 1: Create dashboard package init**

```python
# src/dev_sync/dashboard/__init__.py
"""Dashboard client for dev-sync."""

from dev_sync.dashboard.client import DashboardClient, EventPayload, HeartbeatPayload

__all__ = ["DashboardClient", "EventPayload", "HeartbeatPayload"]
```

- [ ] **Step 2: Write failing test for push_event**

```python
# tests/test_dashboard_client.py
"""Tests for dashboard client."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestDashboardClient:
    @pytest.mark.asyncio
    async def test_push_event_sends_to_server(self) -> None:
        """Should POST event to dashboard server."""
        from dev_sync.dashboard.client import DashboardClient, EventPayload

        client = DashboardClient(
            url="https://dashboard.example.com",
            auth_token="test-token",
            node_id="test-node",
        )

        event = EventPayload(
            level="info",
            pipeline="secops",
            repo="owner/repo",
            message="Merged 3 PRs",
        )

        mock_response = AsyncMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
            await client.push_event(event)

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/event" in call_args[0][0]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_dashboard_client.py::TestDashboardClient::test_push_event_sends_to_server -v`
Expected: FAIL with "No module named 'dev_sync.dashboard'"

- [ ] **Step 4: Write minimal implementation**

```python
# src/dev_sync/dashboard/client.py
"""Dashboard client for event push and heartbeat."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel


class HeartbeatPayload(BaseModel):
    """Payload for heartbeat endpoint."""

    node_id: str
    timestamp: str = ""
    version: str = "0.1.0"
    uptime_seconds: int = 0
    platform: str = ""
    active_sessions: list[dict[str, Any]] = []
    last_github_poll: str | None = None
    last_github_poll_status: str = "ok"
    repos_configured: int = 0
    repos_active: int = 0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class EventPayload(BaseModel):
    """Payload for event endpoint."""

    level: str  # info, warning, error
    pipeline: str  # secops, dev
    repo: str
    message: str
    session_id: str | None = None
    timestamp: str = ""
    details: dict[str, Any] = {}

    def model_post_init(self, __context: Any) -> None:
        if not self.timestamp:
            object.__setattr__(
                self, "timestamp", datetime.now(timezone.utc).isoformat()
            )


@dataclass
class DashboardClient:
    """Client for dashboard API with offline queue."""

    url: str
    auth_token: str
    node_id: str
    queue_dir: Path | None = None
    timeout: int = 30
    max_retries: int = 3
    _queue: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.queue_dir:
            self.queue_dir = Path(self.queue_dir)
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            self._load_queue()

    def _load_queue(self) -> None:
        """Load queued events from disk."""
        if not self.queue_dir:
            return
        queue_file = self.queue_dir / "event_queue.json"
        if queue_file.exists():
            try:
                self._queue = json.loads(queue_file.read_text())
            except json.JSONDecodeError:
                self._queue = []

    def _save_queue(self) -> None:
        """Save queued events to disk."""
        if not self.queue_dir:
            return
        queue_file = self.queue_dir / "event_queue.json"
        queue_file.write_text(json.dumps(self._queue))

    def _queue_event(self, event: EventPayload) -> None:
        """Add event to offline queue."""
        self._queue.append(event.model_dump())
        self._save_queue()

    async def push_event(self, event: EventPayload) -> bool:
        """Push event to dashboard, queue on failure."""
        payload = event.model_dump()
        payload["node_id"] = self.node_id

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url}/event",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
                response.raise_for_status()
                return True
        except (httpx.HTTPError, httpx.TimeoutException):
            self._queue_event(event)
            return False

    async def heartbeat(self, payload: HeartbeatPayload) -> bool:
        """Send heartbeat to dashboard."""
        data = payload.model_dump()
        data["node_id"] = self.node_id

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url}/heartbeat",
                    json=data,
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
                response.raise_for_status()
                return True
        except (httpx.HTTPError, httpx.TimeoutException):
            return False

    async def drain_queue(self) -> int:
        """Attempt to send queued events. Returns count of successfully sent."""
        if not self._queue:
            return 0

        sent = 0
        remaining = []

        for event_data in self._queue:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    event_data["node_id"] = self.node_id
                    response = await client.post(
                        f"{self.url}/event",
                        json=event_data,
                        headers={"Authorization": f"Bearer {self.auth_token}"},
                    )
                    response.raise_for_status()
                    sent += 1
            except (httpx.HTTPError, httpx.TimeoutException):
                remaining.append(event_data)

        self._queue = remaining
        self._save_queue()
        return sent

    @property
    def queue_size(self) -> int:
        """Number of events in offline queue."""
        return len(self._queue)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_dashboard_client.py::TestDashboardClient::test_push_event_sends_to_server -v`
Expected: PASS

- [ ] **Step 6: Write failing test for offline queue**

```python
    @pytest.mark.asyncio
    async def test_push_event_queues_on_failure(self, tmp_path: Path) -> None:
        """Should queue event when server unreachable."""
        from dev_sync.dashboard.client import DashboardClient, EventPayload

        client = DashboardClient(
            url="https://dashboard.example.com",
            auth_token="test-token",
            node_id="test-node",
            queue_dir=tmp_path,
        )

        event = EventPayload(
            level="info",
            pipeline="secops",
            repo="owner/repo",
            message="Test event",
        )

        with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
            result = await client.push_event(event)

            assert result is False
            assert client.queue_size == 1

            queue_file = tmp_path / "event_queue.json"
            assert queue_file.exists()
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_dashboard_client.py::TestDashboardClient::test_push_event_queues_on_failure -v`
Expected: PASS

- [ ] **Step 8: Write failing test for drain_queue**

```python
    @pytest.mark.asyncio
    async def test_drain_queue_sends_queued_events(self, tmp_path: Path) -> None:
        """Should send queued events when connection restored."""
        from dev_sync.dashboard.client import DashboardClient, EventPayload

        queue_file = tmp_path / "event_queue.json"
        queue_file.write_text(json.dumps([
            {"level": "info", "pipeline": "secops", "repo": "r1", "message": "m1", "timestamp": "t1", "details": {}},
            {"level": "info", "pipeline": "secops", "repo": "r2", "message": "m2", "timestamp": "t2", "details": {}},
        ]))

        client = DashboardClient(
            url="https://dashboard.example.com",
            auth_token="test-token",
            node_id="test-node",
            queue_dir=tmp_path,
        )

        assert client.queue_size == 2

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            sent = await client.drain_queue()

            assert sent == 2
            assert client.queue_size == 0
```

- [ ] **Step 9: Run all tests**

Run: `pytest tests/test_dashboard_client.py -v`
Expected: All tests PASS

- [ ] **Step 10: Commit**

```bash
git add src/dev_sync/dashboard/__init__.py src/dev_sync/dashboard/client.py tests/test_dashboard_client.py
git commit -m "$(cat <<'EOF'
feat(dashboard): add client with offline event queue

Dashboard client pushes events and heartbeats. Failed events are queued
to disk and drained when connection is restored.
EOF
)"
```

---

### Task 5: Pipeline Base Protocol

**Files:**
- Create: `src/dev_sync/pipelines/__init__.py`
- Create: `src/dev_sync/pipelines/base.py`
- Test: `tests/test_pipeline_base.py`

- [ ] **Step 1: Create pipelines package init**

```python
# src/dev_sync/pipelines/__init__.py
"""Pipeline implementations for dev-sync."""

from dev_sync.pipelines.base import Pipeline, PipelineContext, PipelineResult

__all__ = ["Pipeline", "PipelineContext", "PipelineResult"]
```

- [ ] **Step 2: Write failing test for Pipeline protocol**

```python
# tests/test_pipeline_base.py
"""Tests for pipeline base protocol."""

from pathlib import Path

import pytest


class TestPipelineProtocol:
    def test_pipeline_context_has_required_fields(self) -> None:
        """PipelineContext should have all required fields."""
        from dev_sync.pipelines.base import PipelineContext

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=Path("/tmp/worktree"),
            context_path=Path("/tmp/context/CLAUDE.md"),
            state_file=Path("/tmp/state.json"),
        )

        assert ctx.session_id == "sess-123"
        assert ctx.repo == "owner/repo"

    def test_pipeline_result_has_required_fields(self) -> None:
        """PipelineResult should capture execution outcome."""
        from dev_sync.pipelines.base import PipelineResult

        result = PipelineResult(
            success=True,
            session_id="sess-123",
            summary="Completed successfully",
        )

        assert result.success
        assert result.summary == "Completed successfully"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_pipeline_base.py -v`
Expected: FAIL with "No module named 'dev_sync.pipelines'"

- [ ] **Step 4: Write implementation**

```python
# src/dev_sync/pipelines/base.py
"""Base protocol and types for pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class PipelineContext:
    """Context for a pipeline execution."""

    session_id: str
    repo: str
    worktree_path: Path
    context_path: Path
    state_file: Path
    issue_number: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Result of a pipeline execution."""

    success: bool
    session_id: str
    summary: str
    blocked: bool = False
    question: str | None = None
    error: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Pipeline(Protocol):
    """Protocol for pipeline implementations."""

    name: str

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Execute the pipeline."""
        ...

    async def resume(
        self, ctx: PipelineContext, answer: str
    ) -> PipelineResult:
        """Resume a blocked pipeline with user answer."""
        ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_pipeline_base.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/pipelines/__init__.py src/dev_sync/pipelines/base.py tests/test_pipeline_base.py
git commit -m "$(cat <<'EOF'
feat(pipelines): add base protocol and types

Pipeline protocol defines run/resume interface. PipelineContext and
PipelineResult provide structured input/output for pipeline execution.
EOF
)"
```

---

### Task 6: Secops Pipeline Implementation

**Files:**
- Create: `src/dev_sync/pipelines/secops.py`
- Test: `tests/test_secops_pipeline.py`

- [ ] **Step 1: Write failing test for secops pipeline initialization**

```python
# tests/test_secops_pipeline.py
"""Tests for secops pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSecopsPipeline:
    def test_secops_pipeline_has_name(self) -> None:
        """SecopsPipeline should have name attribute."""
        from dev_sync.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=MagicMock(),
            state_db=MagicMock(),
            transport=MagicMock(),
        )

        assert pipeline.name == "secops"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_secops_pipeline_has_name -v`
Expected: FAIL with "No module named 'dev_sync.pipelines.secops'"

- [ ] **Step 3: Write minimal implementation**

```python
# src/dev_sync/pipelines/secops.py
"""Secops pipeline for security triage across repos."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_sync.core.checkpoint import CheckpointStatus
from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult
from dev_sync.core.github import GitHubCLI
from dev_sync.core.state import StateDB
from dev_sync.core.worktree import WorktreeManager
from dev_sync.dashboard.client import DashboardClient, EventPayload
from dev_sync.pipelines.base import Pipeline, PipelineContext, PipelineResult
from dev_sync.transports.base import Transport


@dataclass
class SecopsPipeline:
    """Security operations pipeline for daily triage."""

    dispatcher: ClaudeDispatcher
    github: GitHubCLI
    worktree: WorktreeManager
    dashboard: DashboardClient | None
    state_db: StateDB
    transport: Transport | None

    name: str = "secops"

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Run secops on a single repo."""
        prompt = self._build_prompt(ctx.repo)

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
        )

        return self._session_to_result(result)

    async def resume(self, ctx: PipelineContext, answer: str) -> PipelineResult:
        """Resume blocked secops with user answer."""
        prompt = f"User answered: {answer}\n\nContinue from where you left off."

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
            resume_session_id=ctx.session_id,
        )

        return self._session_to_result(result)

    def _build_prompt(self, repo: str) -> str:
        """Build the secops prompt."""
        return f"""Execute security operations for repository {repo}.

1. Run /gh-dashboard to get current security status
2. For each Dependabot alert or security PR:
   - Review the severity and impact
   - If patch/minor with green CI, auto-merge
   - If major or unclear, use checkpoint.blocked() to ask for guidance
3. Summarize actions taken

Use checkpoint.done() when complete with summary of actions.
Use checkpoint.blocked() if you need human input.
Use checkpoint.failed() if something goes wrong."""

    def _session_to_result(self, result: SessionResult) -> PipelineResult:
        """Convert SessionResult to PipelineResult."""
        if result.state is None:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="No checkpoint state returned",
                error=result.stderr or "Unknown error",
            )

        if result.state.status == CheckpointStatus.DONE:
            return PipelineResult(
                success=True,
                session_id=result.session_id,
                summary=result.state.summary or "Completed",
                outputs=result.state.outputs,
            )

        if result.state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="Blocked on user input",
                blocked=True,
                question=result.state.question,
            )

        return PipelineResult(
            success=False,
            session_id=result.session_id,
            summary="Failed",
            error=result.state.error,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_secops_pipeline_has_name -v`
Expected: PASS

- [ ] **Step 5: Write failing test for run method**

```python
    @pytest.mark.asyncio
    async def test_run_dispatches_claude_session(self, tmp_path: Path) -> None:
        """Should dispatch Claude with secops prompt."""
        from dev_sync.core.checkpoint import CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.secops import SecopsPipeline

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Merged 2 PRs"
        mock_state.outputs = {"merged_prs": [1, 2]}

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        pipeline = SecopsPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
        )

        result = await pipeline.run(ctx)

        assert result.success
        assert result.summary == "Merged 2 PRs"
        mock_dispatcher.spawn_session.assert_called_once()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_run_dispatches_claude_session -v`
Expected: PASS

- [ ] **Step 7: Write failing test for blocked handling**

```python
    @pytest.mark.asyncio
    async def test_run_returns_blocked_when_needs_input(self, tmp_path: Path) -> None:
        """Should return blocked result when Claude needs input."""
        from dev_sync.core.checkpoint import CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.secops import SecopsPipeline

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        mock_state.question = "Should I merge major version bump?"
        mock_state.summary = None
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        pipeline = SecopsPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
        )

        result = await pipeline.run(ctx)

        assert not result.success
        assert result.blocked
        assert "major version" in result.question
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_run_returns_blocked_when_needs_input -v`
Expected: PASS

- [ ] **Step 9: Run all tests**

Run: `pytest tests/test_secops_pipeline.py -v`
Expected: All tests PASS

- [ ] **Step 10: Commit**

```bash
git add src/dev_sync/pipelines/secops.py tests/test_secops_pipeline.py
git commit -m "$(cat <<'EOF'
feat(pipelines): add secops pipeline

Secops pipeline runs security triage across repos. Dispatches Claude
with gh-dashboard/gh-secops skills, handles blocked/done/failed states.
EOF
)"
```

---

### Task 7: Secops Orchestration (run_secops_all)

**Files:**
- Modify: `src/dev_sync/pipelines/secops.py`
- Test: `tests/test_secops_pipeline.py`

- [ ] **Step 1: Write failing test for run_all**

```python
    @pytest.mark.asyncio
    async def test_run_all_processes_multiple_repos(self, tmp_path: Path) -> None:
        """Should run secops on all configured repos."""
        from dev_sync.core.checkpoint import CheckpointStatus
        from dev_sync.core.config import RepoConfig
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.secops import SecopsPipeline, run_secops_all

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Done"
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True

        repos = [
            MagicMock(name="owner/repo1", local_path=tmp_path / "repo1"),
            MagicMock(name="owner/repo2", local_path=tmp_path / "repo2"),
        ]

        results = await run_secops_all(
            repos=repos,
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(results) == 2
        assert all(r.success for r in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_run_all_processes_multiple_repos -v`
Expected: FAIL with "cannot import name 'run_secops_all'"

- [ ] **Step 3: Implement run_secops_all**

Add to `src/dev_sync/pipelines/secops.py`:

```python
import time
import uuid

async def run_secops_all(
    repos: list[Any],
    dispatcher: ClaudeDispatcher,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
) -> list[PipelineResult]:
    """Run secops pipeline on all configured repos."""
    results = []

    pipeline = SecopsPipeline(
        dispatcher=dispatcher,
        github=github,
        worktree=worktree,
        dashboard=dashboard,
        state_db=state_db,
        transport=transport,
    )

    for repo_config in repos:
        repo = repo_config.name
        session_id = f"secops-{repo.replace('/', '-')}-{uuid.uuid4().hex[:8]}"

        if not state_db.acquire_lock(repo, session_id):
            results.append(PipelineResult(
                success=False,
                session_id=session_id,
                summary=f"Could not acquire lock for {repo}",
                error="Repository locked by another session",
            ))
            continue

        try:
            await worktree.ensure_bare_repo(repo)
            worktree_path = await worktree.create_worktree(repo, session_id)

            context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
            if context_path.exists():
                worktree.symlink_context(worktree_path, context_path)

            state_file = worktree_path / ".dev-sync" / "state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)

            ctx = PipelineContext(
                session_id=session_id,
                repo=repo,
                worktree_path=worktree_path,
                context_path=context_path,
                state_file=state_file,
            )

            state_db.execute(
                """INSERT INTO sessions (id, pipeline, repo, worktree_path, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, "secops", repo, str(worktree_path), "running", int(time.time())),
            )
            state_db.commit()

            result = await pipeline.run(ctx)
            results.append(result)

            status = "done" if result.success else ("blocked" if result.blocked else "failed")
            state_db.execute(
                "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
                (status, result.summary, int(time.time()), session_id),
            )
            state_db.commit()

            if dashboard and result.success:
                await dashboard.push_event(EventPayload(
                    level="info",
                    pipeline="secops",
                    repo=repo,
                    message=result.summary,
                    session_id=session_id,
                ))

            worktree.remove_context_symlink(worktree_path)
            await worktree.remove_worktree(repo, session_id)

        except Exception as e:
            results.append(PipelineResult(
                success=False,
                session_id=session_id,
                summary=f"Error processing {repo}",
                error=str(e),
            ))

        finally:
            state_db.release_lock(repo, session_id)

    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_run_all_processes_multiple_repos -v`
Expected: PASS

- [ ] **Step 5: Write failing test for lock handling**

```python
    @pytest.mark.asyncio
    async def test_run_all_skips_locked_repos(self, tmp_path: Path) -> None:
        """Should skip repos that are already locked."""
        from dev_sync.pipelines.secops import run_secops_all

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = False

        repos = [MagicMock(name="owner/locked-repo")]

        results = await run_secops_all(
            repos=repos,
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path,
        )

        assert len(results) == 1
        assert not results[0].success
        assert "locked" in results[0].error.lower()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_secops_pipeline.py::TestSecopsPipeline::test_run_all_skips_locked_repos -v`
Expected: PASS

- [ ] **Step 7: Update pipelines __init__.py**

```python
# src/dev_sync/pipelines/__init__.py
"""Pipeline implementations for dev-sync."""

from dev_sync.pipelines.base import Pipeline, PipelineContext, PipelineResult
from dev_sync.pipelines.secops import SecopsPipeline, run_secops_all

__all__ = [
    "Pipeline",
    "PipelineContext",
    "PipelineResult",
    "SecopsPipeline",
    "run_secops_all",
]
```

- [ ] **Step 8: Run all tests**

Run: `pytest tests/test_secops_pipeline.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add src/dev_sync/pipelines/secops.py src/dev_sync/pipelines/__init__.py tests/test_secops_pipeline.py
git commit -m "$(cat <<'EOF'
feat(pipelines): add run_secops_all orchestration

Orchestrates secops across multiple repos: acquires locks, creates
worktrees, symlinks context, runs pipeline, pushes events, cleans up.
EOF
)"
```

---

### Task 8: CLI Command for Secops

**Files:**
- Modify: `src/dev_sync/cli.py`
- Test: `tests/test_cli.py` (add secops tests)

- [ ] **Step 1: Write failing test for secops run command**

```python
# Add to tests/test_cli.py or create tests/test_cli_secops.py
"""Tests for secops CLI commands."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

runner = CliRunner()


class TestSecopsCLI:
    def test_run_secops_requires_config(self) -> None:
        """Should fail without valid config."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["run", "secops", "--config", "/nonexistent.yaml"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "error" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_secops.py::TestSecopsCLI::test_run_secops_requires_config -v`
Expected: FAIL with "No such command 'run'"

- [ ] **Step 3: Add run command group to CLI**

Add to `src/dev_sync/cli.py` after the bridge_app section:

```python
# Run subcommand group
run_app = typer.Typer(help="Pipeline execution commands.")
app.add_typer(run_app, name="run")


@run_app.command("secops")
def run_secops(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
    repo: str = typer.Option(
        None,
        "--repo",
        "-r",
        help="Run on specific repo only",
    ),
) -> None:
    """Run secops pipeline on configured repos."""
    import asyncio

    from dev_sync.core.dispatcher import ClaudeDispatcher
    from dev_sync.core.github import GitHubCLI
    from dev_sync.core.state import StateDB
    from dev_sync.core.worktree import WorktreeManager
    from dev_sync.dashboard.client import DashboardClient
    from dev_sync.pipelines.secops import run_secops_all

    path = Path(config_path)

    try:
        config = load_config(path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    repos = config.repos
    if repo:
        repos = [r for r in repos if r.name == repo]
        if not repos:
            console.print(f"[red]Repo not found:[/red] {repo}")
            raise typer.Exit(1)

    if not repos:
        console.print("[yellow]No repos configured.[/yellow]")
        return

    db = StateDB(config.paths.state_db)
    dispatcher = ClaudeDispatcher(
        claude_binary=config.claude.binary,
        default_timeout=config.claude.default_timeout_seconds,
    )
    github = GitHubCLI()
    worktree = WorktreeManager(
        worktrees_dir=config.paths.worktrees,
        bare_repos_dir=config.paths.bare_repos,
    )

    dashboard = None
    if config.dashboard.enabled and config.dashboard.url:
        import os
        token = os.environ.get(config.dashboard.auth_token_env, "")
        if token:
            dashboard = DashboardClient(
                url=config.dashboard.url,
                auth_token=token,
                node_id=config.node_id,
                queue_dir=config.paths.state_db.parent / "event_queue",
            )

    console.print(f"Running secops on {len(repos)} repo(s)...")

    async def _run():
        return await run_secops_all(
            repos=repos,
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=db,
            transport=None,
            contexts_dir=config.paths.contexts,
        )

    try:
        results = asyncio.run(_run())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        db.close()

    success_count = sum(1 for r in results if r.success)
    console.print(f"\n[bold]Results:[/bold] {success_count}/{len(results)} succeeded")

    for result in results:
        status = "[green]OK[/green]" if result.success else "[red]FAIL[/red]"
        console.print(f"  {status} {result.summary}")

    if not all(r.success for r in results):
        raise typer.Exit(1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_secops.py::TestSecopsCLI::test_run_secops_requires_config -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/cli.py tests/test_cli_secops.py
git commit -m "$(cat <<'EOF'
feat(cli): add run secops command

CLI command to run secops pipeline on configured repos. Supports
--repo flag for single-repo runs. Reports success/failure summary.
EOF
)"
```

---

### Task 9: Integration Test

**Files:**
- Create: `tests/test_secops_integration.py`

- [ ] **Step 1: Write integration test with mocked subprocess**

```python
# tests/test_secops_integration.py
"""Integration tests for secops pipeline."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSecopsIntegration:
    @pytest.mark.asyncio
    async def test_full_secops_flow_with_mocked_claude(self, tmp_path: Path) -> None:
        """Should run complete secops flow with mocked Claude subprocess."""
        from dev_sync.core.dispatcher import ClaudeDispatcher
        from dev_sync.core.github import GitHubCLI
        from dev_sync.core.state import StateDB
        from dev_sync.core.worktree import WorktreeManager
        from dev_sync.pipelines.secops import run_secops_all

        db_path = tmp_path / "state.db"
        db = StateDB(db_path)

        worktrees_dir = tmp_path / "worktrees"
        bare_repos_dir = tmp_path / "repos"
        contexts_dir = tmp_path / "contexts"

        context_dir = contexts_dir / "owner-repo"
        context_dir.mkdir(parents=True)
        (context_dir / "CLAUDE.md").write_text("# Test context")

        dispatcher = ClaudeDispatcher(claude_binary="claude")
        github = GitHubCLI()
        worktree = WorktreeManager(
            worktrees_dir=worktrees_dir,
            bare_repos_dir=bare_repos_dir,
        )

        repo_config = MagicMock()
        repo_config.name = "owner/repo"
        repo_config.local_path = tmp_path / "repo"

        async def mock_spawn_session(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "Merged 2 dependabot PRs",
                "outputs": {"merged_prs": [101, 102]},
            }))

            from dev_sync.core.dispatcher import SessionResult
            from dev_sync.core.checkpoint import read_checkpoint
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                state=read_checkpoint(state_file),
            )

        async def mock_create_worktree(repo, session_id, **kwargs):
            wt_path = worktrees_dir / f"{repo.replace('/', '-')}-{session_id}"
            wt_path.mkdir(parents=True)
            (wt_path / ".git" / "info").mkdir(parents=True)
            (wt_path / ".git" / "info" / "exclude").write_text("")
            return wt_path

        with patch.object(dispatcher, "spawn_session", side_effect=mock_spawn_session), \
             patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock), \
             patch.object(worktree, "create_worktree", side_effect=mock_create_worktree), \
             patch.object(worktree, "remove_worktree", new_callable=AsyncMock):

            results = await run_secops_all(
                repos=[repo_config],
                dispatcher=dispatcher,
                github=github,
                worktree=worktree,
                dashboard=None,
                state_db=db,
                transport=None,
                contexts_dir=contexts_dir,
            )

            assert len(results) == 1
            assert results[0].success
            assert "Merged 2" in results[0].summary

            session = db.execute(
                "SELECT * FROM sessions WHERE pipeline = 'secops'"
            ).fetchone()
            assert session is not None
            assert session["status"] == "done"

        db.close()
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/test_secops_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_secops_integration.py
git commit -m "$(cat <<'EOF'
test: add secops integration test

Full flow test with mocked Claude subprocess. Verifies worktree
creation, context symlinking, state parsing, and database updates.
EOF
)"
```

---

### Task 10: Linting and Final Validation

**Files:**
- All new files

- [ ] **Step 1: Run ruff check**

Run: `ruff check src/dev_sync/core/github.py src/dev_sync/core/worktree.py src/dev_sync/core/dispatcher.py src/dev_sync/dashboard/ src/dev_sync/pipelines/`
Expected: No errors

- [ ] **Step 2: Fix any linting issues**

Run: `ruff check --fix src/dev_sync/`

- [ ] **Step 3: Run type check**

Run: `pyright src/dev_sync/` or `mypy src/dev_sync/`
Expected: No errors (or only known issues)

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 5: Run codex review**

Run: `codex review`
Expected: No critical issues

- [ ] **Step 6: Final commit if any fixes**

```bash
git add -A
git commit -m "chore: fix linting and type issues"
```

---

### Task 11: Update Exports and Documentation

**Files:**
- Modify: `src/dev_sync/__init__.py`
- Modify: `src/dev_sync/core/__init__.py`

- [ ] **Step 1: Update core module exports**

```python
# src/dev_sync/core/__init__.py
"""Core modules for dev-sync orchestrator."""

from dev_sync.core import checkpoint
from dev_sync.core.audit import audit_all, audit_skill, discover_skills
from dev_sync.core.config import Config, ConfigError, load_config
from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult
from dev_sync.core.github import GitHubCLI, GitHubError
from dev_sync.core.state import StateDB
from dev_sync.core.worktree import WorktreeError, WorktreeManager

__all__ = [
    "checkpoint",
    "audit_all",
    "audit_skill",
    "discover_skills",
    "Config",
    "ConfigError",
    "load_config",
    "ClaudeDispatcher",
    "SessionResult",
    "GitHubCLI",
    "GitHubError",
    "StateDB",
    "WorktreeError",
    "WorktreeManager",
]
```

- [ ] **Step 2: Run tests to verify exports**

Run: `python -c "from dev_sync.core import ClaudeDispatcher, GitHubCLI, WorktreeManager; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add src/dev_sync/core/__init__.py
git commit -m "$(cat <<'EOF'
chore: update core module exports

Export dispatcher, github, and worktree classes from core package.
EOF
)"
```

---

## Phase Gate Validation

After completing all tasks, verify the phase gate:

- [ ] **Gate 1:** Run `dev-sync run secops --config config/orchestrator.yaml --repo <test-repo>` on a single repo
- [ ] **Gate 2:** Run on 2-3 repos, verify events are logged to state.db
- [ ] **Gate 3:** Verify dashboard client queues events when server unavailable (disconnect network, run secops, check queue file)

```bash
# Test commands
dev-sync run secops --config config/orchestrator.yaml --repo owner/repo1
dev-sync status --config config/orchestrator.yaml
sqlite3 ~/.dev-sync/state.db "SELECT * FROM sessions WHERE pipeline='secops'"
```

Expected: Sessions recorded, events queued or sent, no errors.
