---
title: Phase 4 — Dev Pipeline
layout: default
parent: Plans
nav_order: 5
---

# Phase 4: Dev Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the dev pipeline that triggers when a GitHub issue is assigned to the user, creates a branch, runs the superpowers flow, opens a PR, and watches for merge.

**Architecture:** GitHub poller detects newly assigned issues (5-min interval). Dev pipeline creates a worktree with a new branch (from config template), spawns Claude to implement the fix via /superpowers, opens a PR, then monitors for merge via PR watcher. Telegram notifications at key states.

**Tech Stack:** Python 3.11+, asyncio, gh CLI, typer, pydantic

---

## File Structure

**New Files:**
- `src/dev_sync/core/poller.py` - GitHub issue poller with state tracking
- `src/dev_sync/core/pr_watcher.py` - PR merge detection
- `src/dev_sync/pipelines/dev.py` - Dev pipeline implementation
- `tests/test_poller.py` - Poller tests
- `tests/test_dev_pipeline.py` - Dev pipeline tests
- `tests/test_pr_watcher.py` - PR watcher tests

**Modified Files:**
- `src/dev_sync/core/github.py` - Add issue-related methods
- `src/dev_sync/core/worktree.py` - Add branch creation method
- `src/dev_sync/cli.py` - Add poller and dev pipeline commands
- `src/dev_sync/core/__init__.py` - Export new modules
- `src/dev_sync/pipelines/__init__.py` - Export dev pipeline

---

### Task 1: GitHub Issue Methods

**Files:**
- Modify: `src/dev_sync/core/github.py`
- Test: `tests/test_github.py`

- [ ] **Step 1: Write the failing test for list_assigned_issues**

```python
# Add to tests/test_github.py

@pytest.mark.asyncio
async def test_list_assigned_issues(self) -> None:
    """Should list issues assigned to a user."""
    from dev_sync.core.github import GitHubCLI

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps([
                {"number": 123, "title": "Fix bug", "state": "open"},
                {"number": 456, "title": "Add feature", "state": "open"},
            ]).encode(),
            b"",
        )
        mock_exec.return_value = mock_proc

        github = GitHubCLI()
        issues = await github.list_assigned_issues("owner/repo", "username")

        assert len(issues) == 2
        assert issues[0]["number"] == 123
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_github.py::TestGitHubCLI::test_list_assigned_issues -v`
Expected: FAIL with "GitHubCLI has no attribute 'list_assigned_issues'"

- [ ] **Step 3: Implement list_assigned_issues**

```python
# Add to src/dev_sync/core/github.py after list_security_alerts

async def list_assigned_issues(
    self,
    repo: str,
    assignee: str,
    state: str = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List issues assigned to a user."""
    output = await self._run_gh(
        "issue", "list",
        "--repo", repo,
        "--assignee", assignee,
        "--state", state,
        "--limit", str(limit),
        "--json", "number,title,state,createdAt,updatedAt,labels,url",
    )
    return json.loads(output) if output.strip() else []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_github.py::TestGitHubCLI::test_list_assigned_issues -v`
Expected: PASS

- [ ] **Step 5: Write test for get_issue**

```python
# Add to tests/test_github.py

@pytest.mark.asyncio
async def test_get_issue(self) -> None:
    """Should get a single issue by number."""
    from dev_sync.core.github import GitHubCLI

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps({
                "number": 123,
                "title": "Fix bug",
                "body": "Description here",
                "state": "open",
            }).encode(),
            b"",
        )
        mock_exec.return_value = mock_proc

        github = GitHubCLI()
        issue = await github.get_issue("owner/repo", 123)

        assert issue["number"] == 123
        assert issue["title"] == "Fix bug"
```

- [ ] **Step 6: Implement get_issue**

```python
# Add to src/dev_sync/core/github.py

async def get_issue(
    self,
    repo: str,
    issue_number: int,
) -> dict[str, Any]:
    """Get a single issue by number."""
    output = await self._run_gh(
        "issue", "view",
        str(issue_number),
        "--repo", repo,
        "--json", "number,title,body,state,createdAt,updatedAt,labels,url,assignees",
    )
    return json.loads(output)
```

- [ ] **Step 7: Write test for create_pr**

```python
# Add to tests/test_github.py

@pytest.mark.asyncio
async def test_create_pr(self) -> None:
    """Should create a pull request."""
    from dev_sync.core.github import GitHubCLI

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps({
                "number": 42,
                "url": "https://github.com/owner/repo/pull/42",
            }).encode(),
            b"",
        )
        mock_exec.return_value = mock_proc

        github = GitHubCLI()
        pr = await github.create_pr(
            repo="owner/repo",
            title="Fix issue #123",
            body="Closes #123",
            head="fix/issue-123",
            base="main",
        )

        assert pr["number"] == 42
```

- [ ] **Step 8: Implement create_pr**

```python
# Add to src/dev_sync/core/github.py

async def create_pr(
    self,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
) -> dict[str, Any]:
    """Create a pull request."""
    output = await self._run_gh(
        "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--head", head,
        "--base", base,
        "--json", "number,url,state",
    )
    return json.loads(output)
```

- [ ] **Step 9: Write test for get_pr_state**

```python
# Add to tests/test_github.py

@pytest.mark.asyncio
async def test_get_pr_state(self) -> None:
    """Should get PR state including merge status."""
    from dev_sync.core.github import GitHubCLI

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps({
                "number": 42,
                "state": "MERGED",
                "mergedAt": "2026-04-17T12:00:00Z",
            }).encode(),
            b"",
        )
        mock_exec.return_value = mock_proc

        github = GitHubCLI()
        pr = await github.get_pr_state("owner/repo", 42)

        assert pr["state"] == "MERGED"
```

- [ ] **Step 10: Implement get_pr_state**

```python
# Add to src/dev_sync/core/github.py

async def get_pr_state(
    self,
    repo: str,
    pr_number: int,
) -> dict[str, Any]:
    """Get PR state including merge status."""
    output = await self._run_gh(
        "pr", "view",
        str(pr_number),
        "--repo", repo,
        "--json", "number,state,mergedAt,mergedBy,url",
    )
    return json.loads(output)
```

- [ ] **Step 11: Write test for close_issue**

```python
# Add to tests/test_github.py

@pytest.mark.asyncio
async def test_close_issue(self) -> None:
    """Should close an issue with a comment."""
    from dev_sync.core.github import GitHubCLI

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_proc

        github = GitHubCLI()
        await github.close_issue("owner/repo", 123, "Fixed in #42")

        # Verify comment was added and issue was closed
        assert mock_exec.call_count == 2
```

- [ ] **Step 12: Implement close_issue**

```python
# Add to src/dev_sync/core/github.py

async def close_issue(
    self,
    repo: str,
    issue_number: int,
    comment: str | None = None,
) -> None:
    """Close an issue with optional comment."""
    if comment:
        await self._run_gh(
            "issue", "comment",
            str(issue_number),
            "--repo", repo,
            "--body", comment,
        )
    await self._run_gh(
        "issue", "close",
        str(issue_number),
        "--repo", repo,
    )
```

- [ ] **Step 13: Run all tests and commit**

Run: `pytest tests/test_github.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/core/github.py tests/test_github.py
git commit -m "feat(github): add issue and PR management methods"
```

---

### Task 2: GitHub Poller

**Files:**
- Create: `src/dev_sync/core/poller.py`
- Test: `tests/test_poller.py`

- [ ] **Step 1: Write the failing test for IssuePoller**

```python
# tests/test_poller.py
"""Tests for GitHub issue poller."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestIssuePoller:
    @pytest.mark.asyncio
    async def test_poll_returns_new_issues(self, tmp_path: Path) -> None:
        """Should return newly assigned issues."""
        from dev_sync.core.poller import IssuePoller

        mock_github = AsyncMock()
        mock_github.list_assigned_issues.return_value = [
            {"number": 123, "title": "Fix bug", "createdAt": "2026-04-17T12:00:00Z"},
            {"number": 456, "title": "Add feature", "createdAt": "2026-04-17T13:00:00Z"},
        ]

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        new_issues = await poller.poll()

        assert len(new_issues) == 2
        assert new_issues[0]["repo"] == "owner/repo"
        assert new_issues[0]["issue"]["number"] == 123
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_poller.py::TestIssuePoller::test_poll_returns_new_issues -v`
Expected: FAIL with "No module named 'dev_sync.core.poller'"

- [ ] **Step 3: Implement IssuePoller**

```python
# src/dev_sync/core/poller.py
"""GitHub issue poller for detecting newly assigned issues."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev_sync.core.github import GitHubCLI


@dataclass
class IssuePoller:
    """Polls GitHub for issues assigned to a user."""

    github: GitHubCLI
    username: str
    repos: list[str]
    state_file: Path
    seen_issues: dict[str, set[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    def _load_state(self) -> None:
        """Load seen issues from state file."""
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            self.seen_issues = {
                repo: set(issues) for repo, issues in data.get("seen", {}).items()
            }

    def _save_state(self) -> None:
        """Save seen issues to state file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "seen": {repo: list(issues) for repo, issues in self.seen_issues.items()},
            "last_poll": datetime.now(timezone.utc).isoformat(),
        }
        self.state_file.write_text(json.dumps(data, indent=2))

    async def poll(self) -> list[dict[str, Any]]:
        """Poll all repos for new issues.

        Returns:
            List of dicts with 'repo' and 'issue' keys for new issues.
        """
        new_issues = []

        for repo in self.repos:
            if repo not in self.seen_issues:
                self.seen_issues[repo] = set()

            issues = await self.github.list_assigned_issues(repo, self.username)

            for issue in issues:
                issue_num = issue["number"]
                if issue_num not in self.seen_issues[repo]:
                    new_issues.append({"repo": repo, "issue": issue})
                    self.seen_issues[repo].add(issue_num)

        self._save_state()
        return new_issues

    def mark_seen(self, repo: str, issue_number: int) -> None:
        """Mark an issue as seen without polling."""
        if repo not in self.seen_issues:
            self.seen_issues[repo] = set()
        self.seen_issues[repo].add(issue_number)
        self._save_state()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_poller.py::TestIssuePoller::test_poll_returns_new_issues -v`
Expected: PASS

- [ ] **Step 5: Write test for seen issue filtering**

```python
# Add to tests/test_poller.py

@pytest.mark.asyncio
async def test_poll_filters_seen_issues(self, tmp_path: Path) -> None:
    """Should not return issues that were already seen."""
    from dev_sync.core.poller import IssuePoller

    # Pre-populate state file with seen issue
    state_file = tmp_path / "poller_state.json"
    state_file.write_text(json.dumps({
        "seen": {"owner/repo": [123]},
        "last_poll": "2026-04-17T11:00:00Z",
    }))

    mock_github = AsyncMock()
    mock_github.list_assigned_issues.return_value = [
        {"number": 123, "title": "Fix bug"},  # Already seen
        {"number": 456, "title": "New issue"},  # New
    ]

    poller = IssuePoller(
        github=mock_github,
        username="testuser",
        repos=["owner/repo"],
        state_file=state_file,
    )

    new_issues = await poller.poll()

    assert len(new_issues) == 1
    assert new_issues[0]["issue"]["number"] == 456
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_poller.py::TestIssuePoller::test_poll_filters_seen_issues -v`
Expected: PASS

- [ ] **Step 7: Write test for state persistence**

```python
# Add to tests/test_poller.py

@pytest.mark.asyncio
async def test_poll_saves_state(self, tmp_path: Path) -> None:
    """Should save seen issues to state file after poll."""
    from dev_sync.core.poller import IssuePoller

    state_file = tmp_path / "poller_state.json"

    mock_github = AsyncMock()
    mock_github.list_assigned_issues.return_value = [
        {"number": 789, "title": "New issue"},
    ]

    poller = IssuePoller(
        github=mock_github,
        username="testuser",
        repos=["owner/repo"],
        state_file=state_file,
    )

    await poller.poll()

    # Verify state was saved
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert 789 in saved["seen"]["owner/repo"]
```

- [ ] **Step 8: Run all tests and commit**

Run: `pytest tests/test_poller.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/core/poller.py tests/test_poller.py
git commit -m "feat(core): add GitHub issue poller"
```

---

### Task 3: Worktree Branch Creation

**Files:**
- Modify: `src/dev_sync/core/worktree.py`
- Test: `tests/test_worktree.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_worktree.py

@pytest.mark.asyncio
async def test_create_worktree_with_new_branch(self, tmp_path: Path) -> None:
    """Should create worktree with a new branch from default branch."""
    from dev_sync.core.worktree import WorktreeManager

    with patch.object(WorktreeManager, "_run_git", new_callable=AsyncMock) as mock_git:
        mock_git.return_value = "refs/heads/main\n"

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        # Create fake bare repo
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        path = await manager.create_worktree_with_new_branch(
            repo="owner/repo",
            session_id="test-123",
            new_branch="fix/issue-456",
        )

        assert path == tmp_path / "worktrees" / "owner-repo-test-123"
        # Verify git worktree add was called with -b flag
        calls = [str(c) for c in mock_git.call_args_list]
        assert any("-b" in str(c) and "fix/issue-456" in str(c) for c in calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worktree.py::TestWorktreeManager::test_create_worktree_with_new_branch -v`
Expected: FAIL with "WorktreeManager has no attribute 'create_worktree_with_new_branch'"

- [ ] **Step 3: Implement create_worktree_with_new_branch**

```python
# Add to src/dev_sync/core/worktree.py after create_worktree method

async def create_worktree_with_new_branch(
    self,
    repo: str,
    session_id: str,
    new_branch: str,
    base_branch: str | None = None,
) -> Path:
    """Create a worktree with a new branch.

    Args:
        repo: Repository name (owner/repo)
        session_id: Unique session identifier
        new_branch: Name for the new branch
        base_branch: Branch to base off (default: repo's default branch)

    Returns:
        Path to the created worktree
    """
    bare_path = self._get_bare_repo_path(repo)
    worktree_path = self._get_worktree_path(repo, session_id)

    if worktree_path.exists():
        raise WorktreeError(f"Worktree already exists: {worktree_path}")

    if base_branch is None:
        base_branch = await self.get_default_branch(repo)

    await self._run_git(
        "worktree", "add",
        "-b", new_branch,
        str(worktree_path),
        base_branch,
        cwd=bare_path,
    )

    return worktree_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worktree.py::TestWorktreeManager::test_create_worktree_with_new_branch -v`
Expected: PASS

- [ ] **Step 5: Write test for push_branch**

```python
# Add to tests/test_worktree.py

@pytest.mark.asyncio
async def test_push_branch(self, tmp_path: Path) -> None:
    """Should push branch to origin."""
    from dev_sync.core.worktree import WorktreeManager

    with patch.object(WorktreeManager, "_run_git", new_callable=AsyncMock) as mock_git:
        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        worktree_path = tmp_path / "worktrees" / "test-worktree"
        worktree_path.mkdir(parents=True)

        await manager.push_branch(worktree_path, "fix/issue-123")

        mock_git.assert_called_once()
        call_args = mock_git.call_args[0]
        assert "push" in call_args
        assert "-u" in call_args
        assert "origin" in call_args
        assert "fix/issue-123" in call_args
```

- [ ] **Step 6: Implement push_branch**

```python
# Add to src/dev_sync/core/worktree.py

async def push_branch(
    self,
    worktree_path: Path,
    branch: str,
) -> None:
    """Push a branch to origin.

    Args:
        worktree_path: Path to the worktree
        branch: Branch name to push
    """
    await self._run_git(
        "push", "-u", "origin", branch,
        cwd=worktree_path,
    )
```

- [ ] **Step 7: Run all tests and commit**

Run: `pytest tests/test_worktree.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/core/worktree.py tests/test_worktree.py
git commit -m "feat(worktree): add branch creation and push methods"
```

---

### Task 4: Dev Pipeline Implementation

**Files:**
- Create: `src/dev_sync/pipelines/dev.py`
- Test: `tests/test_dev_pipeline.py`

- [ ] **Step 1: Write the failing test for DevPipeline**

```python
# tests/test_dev_pipeline.py
"""Tests for dev pipeline."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestDevPipeline:
    @pytest.mark.asyncio
    async def test_dev_pipeline_has_name(self) -> None:
        """Pipeline should have name 'dev'."""
        from dev_sync.pipelines.dev import DevPipeline

        pipeline = DevPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        assert pipeline.name == "dev"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dev_pipeline.py::TestDevPipeline::test_dev_pipeline_has_name -v`
Expected: FAIL with "No module named 'dev_sync.pipelines.dev'"

- [ ] **Step 3: Create dev pipeline skeleton**

```python
# src/dev_sync/pipelines/dev.py
"""Dev pipeline for issue-to-PR workflow."""

from __future__ import annotations

import time
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
from dev_sync.pipelines.base import PipelineContext, PipelineResult
from dev_sync.transports.base import Transport


@dataclass
class DevPipeline:
    """Dev pipeline for implementing issues and opening PRs."""

    dispatcher: ClaudeDispatcher
    github: GitHubCLI
    worktree: WorktreeManager
    dashboard: DashboardClient | None
    state_db: StateDB
    transport: Transport | None

    name: str = "dev"

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Run dev pipeline on a single issue."""
        prompt = self._build_prompt(ctx.repo, ctx.issue_number, ctx.extra)

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
        )

        return self._session_to_result(result)

    async def resume(self, ctx: PipelineContext, answer: str) -> PipelineResult:
        """Resume blocked dev session with user answer."""
        prompt = f"User answered: {answer}\n\nContinue from where you left off."

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
            resume_session_id=ctx.session_id,
        )

        return self._session_to_result(result)

    def _build_prompt(
        self,
        repo: str,
        issue_number: int | None,
        extra: dict[str, Any],
    ) -> str:
        """Build the dev pipeline prompt."""
        issue_title = extra.get("issue_title", "")
        issue_body = extra.get("issue_body", "")
        branch_name = extra.get("branch_name", "")

        return f"""You are working on issue #{issue_number} in repository {repo}.

**Issue Title:** {issue_title}

**Issue Body:**
{issue_body}

**Branch:** {branch_name}

Execute the following workflow:

1. Validate the issue still applies to the current codebase
2. If anything is unclear, use checkpoint.blocked() to ask for clarification
3. Run /superpowers to plan and implement the fix using TDD
4. Run codex review and address any feedback
5. Push the branch and open a PR that references the issue
6. Use checkpoint.done() with the PR URL in outputs["pr_url"]

Do NOT merge the PR - wait for human review.

Use checkpoint.done() with outputs={{"pr_url": "...", "pr_number": N}} when PR is opened.
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

Run: `pytest tests/test_dev_pipeline.py::TestDevPipeline::test_dev_pipeline_has_name -v`
Expected: PASS

- [ ] **Step 5: Write test for run method**

```python
# Add to tests/test_dev_pipeline.py

@pytest.mark.asyncio
async def test_run_dispatches_claude_session(self, tmp_path: Path) -> None:
    """Should dispatch Claude session with issue context."""
    from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
    from dev_sync.core.dispatcher import SessionResult
    from dev_sync.pipelines.base import PipelineContext
    from dev_sync.pipelines.dev import DevPipeline

    mock_dispatcher = AsyncMock()
    mock_dispatcher.spawn_session.return_value = SessionResult(
        session_id="dev-123",
        exit_code=0,
        stdout="",
        stderr="",
        state=CheckpointState(
            version="1",
            status=CheckpointStatus.DONE,
            session_id="dev-123",
            timestamp="2026-04-17T12:00:00Z",
            summary="PR opened",
            outputs={"pr_url": "https://github.com/owner/repo/pull/42", "pr_number": 42},
        ),
    )

    pipeline = DevPipeline(
        dispatcher=mock_dispatcher,
        github=MagicMock(),
        worktree=MagicMock(),
        dashboard=None,
        state_db=MagicMock(),
        transport=None,
    )

    ctx = PipelineContext(
        session_id="dev-123",
        repo="owner/repo",
        worktree_path=tmp_path,
        context_path=tmp_path / "CLAUDE.md",
        state_file=tmp_path / "state.json",
        issue_number=123,
        extra={
            "issue_title": "Fix the bug",
            "issue_body": "There is a bug",
            "branch_name": "fix/issue-123",
        },
    )

    result = await pipeline.run(ctx)

    assert result.success
    assert result.outputs["pr_number"] == 42
    mock_dispatcher.spawn_session.assert_called_once()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_dev_pipeline.py::TestDevPipeline::test_run_dispatches_claude_session -v`
Expected: PASS

- [ ] **Step 7: Write test for blocked state**

```python
# Add to tests/test_dev_pipeline.py

@pytest.mark.asyncio
async def test_run_returns_blocked_when_needs_input(self, tmp_path: Path) -> None:
    """Should return blocked result when Claude needs input."""
    from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
    from dev_sync.core.dispatcher import SessionResult
    from dev_sync.pipelines.base import PipelineContext
    from dev_sync.pipelines.dev import DevPipeline

    mock_dispatcher = AsyncMock()
    mock_dispatcher.spawn_session.return_value = SessionResult(
        session_id="dev-123",
        exit_code=0,
        stdout="",
        stderr="",
        state=CheckpointState(
            version="1",
            status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
            session_id="dev-123",
            timestamp="2026-04-17T12:00:00Z",
            question="Should I use async or sync for this API?",
        ),
    )

    pipeline = DevPipeline(
        dispatcher=mock_dispatcher,
        github=MagicMock(),
        worktree=MagicMock(),
        dashboard=None,
        state_db=MagicMock(),
        transport=None,
    )

    ctx = PipelineContext(
        session_id="dev-123",
        repo="owner/repo",
        worktree_path=tmp_path,
        context_path=tmp_path / "CLAUDE.md",
        state_file=tmp_path / "state.json",
        issue_number=123,
        extra={},
    )

    result = await pipeline.run(ctx)

    assert not result.success
    assert result.blocked
    assert "async or sync" in result.question
```

- [ ] **Step 8: Run all tests and commit**

Run: `pytest tests/test_dev_pipeline.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/pipelines/dev.py tests/test_dev_pipeline.py
git commit -m "feat(pipelines): add dev pipeline implementation"
```

---

### Task 5: PR Watcher

**Files:**
- Create: `src/dev_sync/core/pr_watcher.py`
- Test: `tests/test_pr_watcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pr_watcher.py
"""Tests for PR merge watcher."""

from unittest.mock import AsyncMock

import pytest


class TestPRWatcher:
    @pytest.mark.asyncio
    async def test_check_merged_returns_true_when_merged(self) -> None:
        """Should return True when PR is merged."""
        from dev_sync.core.pr_watcher import PRWatcher

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {
            "number": 42,
            "state": "MERGED",
            "mergedAt": "2026-04-17T12:00:00Z",
        }

        watcher = PRWatcher(github=mock_github)
        is_merged = await watcher.check_merged("owner/repo", 42)

        assert is_merged is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pr_watcher.py::TestPRWatcher::test_check_merged_returns_true_when_merged -v`
Expected: FAIL with "No module named 'dev_sync.core.pr_watcher'"

- [ ] **Step 3: Implement PRWatcher**

```python
# src/dev_sync/core/pr_watcher.py
"""PR merge watcher for monitoring PR state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Awaitable

from dev_sync.core.github import GitHubCLI


@dataclass
class PRWatcher:
    """Watches PRs for merge events."""

    github: GitHubCLI
    poll_interval: int = 60

    async def check_merged(self, repo: str, pr_number: int) -> bool:
        """Check if a PR has been merged.

        Args:
            repo: Repository name (owner/repo)
            pr_number: PR number

        Returns:
            True if merged, False otherwise
        """
        pr_state = await self.github.get_pr_state(repo, pr_number)
        return pr_state.get("state") == "MERGED"

    async def wait_for_merge(
        self,
        repo: str,
        pr_number: int,
        timeout: int = 86400,
        on_poll: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Wait for a PR to be merged.

        Args:
            repo: Repository name
            pr_number: PR number
            timeout: Max seconds to wait (default 24h)
            on_poll: Optional callback after each poll

        Returns:
            True if merged within timeout, False otherwise
        """
        elapsed = 0
        while elapsed < timeout:
            if await self.check_merged(repo, pr_number):
                return True

            if on_poll:
                await on_poll()

            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pr_watcher.py::TestPRWatcher::test_check_merged_returns_true_when_merged -v`
Expected: PASS

- [ ] **Step 5: Write test for not merged**

```python
# Add to tests/test_pr_watcher.py

@pytest.mark.asyncio
async def test_check_merged_returns_false_when_open(self) -> None:
    """Should return False when PR is still open."""
    from dev_sync.core.pr_watcher import PRWatcher

    mock_github = AsyncMock()
    mock_github.get_pr_state.return_value = {
        "number": 42,
        "state": "OPEN",
        "mergedAt": None,
    }

    watcher = PRWatcher(github=mock_github)
    is_merged = await watcher.check_merged("owner/repo", 42)

    assert is_merged is False
```

- [ ] **Step 6: Write test for wait_for_merge timeout**

```python
# Add to tests/test_pr_watcher.py

@pytest.mark.asyncio
async def test_wait_for_merge_times_out(self) -> None:
    """Should return False when timeout reached."""
    from dev_sync.core.pr_watcher import PRWatcher

    mock_github = AsyncMock()
    mock_github.get_pr_state.return_value = {
        "number": 42,
        "state": "OPEN",
    }

    watcher = PRWatcher(github=mock_github, poll_interval=1)

    # Short timeout for test
    result = await watcher.wait_for_merge("owner/repo", 42, timeout=2)

    assert result is False
    assert mock_github.get_pr_state.call_count >= 2
```

- [ ] **Step 7: Run all tests and commit**

Run: `pytest tests/test_pr_watcher.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/core/pr_watcher.py tests/test_pr_watcher.py
git commit -m "feat(core): add PR merge watcher"
```

---

### Task 6: Run Dev Orchestration Function

**Files:**
- Modify: `src/dev_sync/pipelines/dev.py`
- Test: `tests/test_dev_pipeline.py`

- [ ] **Step 1: Write the failing test for run_dev_issue**

```python
# Add to tests/test_dev_pipeline.py

@pytest.mark.asyncio
async def test_run_dev_issue_full_flow(self, tmp_path: Path) -> None:
    """Should run full dev flow for a single issue."""
    from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
    from dev_sync.core.dispatcher import SessionResult
    from dev_sync.core.state import StateDB
    from dev_sync.pipelines.dev import run_dev_issue

    mock_dispatcher = AsyncMock()
    mock_dispatcher.spawn_session.return_value = SessionResult(
        session_id="dev-123",
        exit_code=0,
        stdout="",
        stderr="",
        state=CheckpointState(
            version="1",
            status=CheckpointStatus.DONE,
            session_id="dev-123",
            timestamp="2026-04-17T12:00:00Z",
            summary="PR #42 opened",
            outputs={"pr_url": "https://github.com/o/r/pull/42", "pr_number": 42},
        ),
    )

    mock_worktree = AsyncMock()
    mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
    mock_worktree.symlink_context = MagicMock()
    mock_worktree.remove_context_symlink = MagicMock()

    mock_github = AsyncMock()
    mock_github.get_issue.return_value = {
        "number": 123,
        "title": "Fix bug",
        "body": "Bug description",
    }

    state_db = StateDB(tmp_path / "state.db")

    result = await run_dev_issue(
        repo="owner/repo",
        issue_number=123,
        branch_template="fix/issue-{n}",
        dispatcher=mock_dispatcher,
        github=mock_github,
        worktree=mock_worktree,
        dashboard=None,
        state_db=state_db,
        transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    assert result.success
    assert result.outputs["pr_number"] == 42
    mock_worktree.create_worktree_with_new_branch.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dev_pipeline.py::TestDevPipeline::test_run_dev_issue_full_flow -v`
Expected: FAIL with "cannot import name 'run_dev_issue'"

- [ ] **Step 3: Implement run_dev_issue**

```python
# Add to src/dev_sync/pipelines/dev.py at the end

async def run_dev_issue(
    repo: str,
    issue_number: int,
    branch_template: str,
    dispatcher: ClaudeDispatcher,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
) -> PipelineResult:
    """Run dev pipeline for a single issue.

    Args:
        repo: Repository name (owner/repo)
        issue_number: GitHub issue number
        branch_template: Branch name template (e.g., "fix/issue-{n}")
        dispatcher: Claude dispatcher
        github: GitHub CLI wrapper
        worktree: Worktree manager
        dashboard: Optional dashboard client
        state_db: State database
        transport: Optional transport for notifications
        contexts_dir: Directory containing repo contexts

    Returns:
        PipelineResult with success/failure and outputs
    """
    session_id = f"dev-{repo.replace('/', '-')}-{issue_number}-{uuid.uuid4().hex[:8]}"
    branch_name = branch_template.replace("{n}", str(issue_number))

    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo}",
            error="Repository locked by another session",
        )

    try:
        # Get issue details
        issue = await github.get_issue(repo, issue_number)

        # Create worktree with new branch
        await worktree.ensure_bare_repo(repo)
        worktree_path = await worktree.create_worktree_with_new_branch(
            repo=repo,
            session_id=session_id,
            new_branch=branch_name,
        )

        # Symlink context
        context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
        if context_path.exists():
            worktree.symlink_context(worktree_path, context_path)

        # Setup state file
        state_file = worktree_path / ".dev-sync" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)

        ctx = PipelineContext(
            session_id=session_id,
            repo=repo,
            worktree_path=worktree_path,
            context_path=context_path,
            state_file=state_file,
            issue_number=issue_number,
            extra={
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body", ""),
                "branch_name": branch_name,
            },
        )

        # Record session
        state_db.execute(
            """INSERT INTO sessions (id, pipeline, repo, worktree_path, status, started_at, issue_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, "dev", repo, str(worktree_path), "running", int(time.time()), issue_number),
        )
        state_db.commit()

        # Run pipeline
        pipeline = DevPipeline(
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=state_db,
            transport=transport,
        )
        result = await pipeline.run(ctx)

        # Update session status
        status = "done" if result.success else ("blocked" if result.blocked else "failed")
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            (status, result.summary, int(time.time()), session_id),
        )
        state_db.commit()

        # Push event to dashboard
        if dashboard and result.success:
            await dashboard.push_event(EventPayload(
                level="info",
                pipeline="dev",
                repo=repo,
                message=result.summary,
                session_id=session_id,
                details={"issue_number": issue_number, "pr_number": result.outputs.get("pr_number")},
            ))

        # Send notification via transport
        if transport and result.success:
            pr_url = result.outputs.get("pr_url", "")
            await transport.send(f"PR ready for review: {pr_url}")

        # Cleanup
        worktree.remove_context_symlink(worktree_path)
        await worktree.remove_worktree(repo, session_id)

        return result

    except Exception as e:
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            ("failed", f"Error: {e}", int(time.time()), session_id),
        )
        state_db.commit()
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Error processing issue #{issue_number}",
            error=str(e),
        )

    finally:
        state_db.release_lock(repo, session_id)
```

- [ ] **Step 4: Add issue_number column to sessions table**

```python
# Modify src/dev_sync/core/state.py - add to _init_db method
# After "summary TEXT," add:
#     issue_number INTEGER,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_dev_pipeline.py::TestDevPipeline::test_run_dev_issue_full_flow -v`
Expected: PASS

- [ ] **Step 6: Run all tests and commit**

Run: `pytest tests/test_dev_pipeline.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/pipelines/dev.py src/dev_sync/core/state.py tests/test_dev_pipeline.py
git commit -m "feat(pipelines): add run_dev_issue orchestration function"
```

---

### Task 7: CLI Commands

**Files:**
- Modify: `src/dev_sync/cli.py`
- Test: `tests/test_cli_dev.py`

- [ ] **Step 1: Write test for run dev command**

```python
# tests/test_cli_dev.py
"""Tests for dev pipeline CLI commands."""

from typer.testing import CliRunner

runner = CliRunner()


class TestRunDevCommand:
    def test_run_dev_requires_issue(self) -> None:
        """Should require issue number."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["run", "dev"])

        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_run_dev_help(self) -> None:
        """Should show help for run dev command."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["run", "dev", "--help"])

        assert result.exit_code == 0
        assert "--issue" in result.output
        assert "--repo" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_dev.py -v`
Expected: FAIL (no 'dev' command)

- [ ] **Step 3: Add run dev command**

```python
# Add to src/dev_sync/cli.py after run_secops command

@run_app.command("dev")
def run_dev(
    issue: int = typer.Option(
        ...,
        "--issue",
        "-i",
        help="GitHub issue number to work on",
    ),
    repo: str = typer.Option(
        None,
        "--repo",
        "-r",
        help="Repository (owner/repo). If not specified, uses first configured repo.",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Run dev pipeline on a specific issue."""
    import asyncio

    from dev_sync.core.dispatcher import ClaudeDispatcher
    from dev_sync.core.github import GitHubCLI
    from dev_sync.core.state import StateDB
    from dev_sync.core.worktree import WorktreeManager
    from dev_sync.dashboard.client import DashboardClient
    from dev_sync.pipelines.dev import run_dev_issue

    path = Path(config_path)

    try:
        config = load_config(path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    # Find repo config
    if repo:
        repo_configs = [r for r in config.repos if r.name == repo]
        if not repo_configs:
            console.print(f"[red]Repo not found:[/red] {repo}")
            raise typer.Exit(1)
        repo_config = repo_configs[0]
    else:
        if not config.repos:
            console.print("[red]No repos configured.[/red]")
            raise typer.Exit(1)
        repo_config = config.repos[0]
        repo = repo_config.name

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

    console.print(f"Running dev pipeline on {repo} issue #{issue}...")

    async def _run():
        return await run_dev_issue(
            repo=repo,
            issue_number=issue,
            branch_template=repo_config.dev_branch_template,
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=db,
            transport=None,
            contexts_dir=config.paths.contexts,
        )

    try:
        result = asyncio.run(_run())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        db.close()

    if result.success:
        console.print(f"[green]Success:[/green] {result.summary}")
        if result.outputs.get("pr_url"):
            console.print(f"PR URL: {result.outputs['pr_url']}")
    elif result.blocked:
        console.print(f"[yellow]Blocked:[/yellow] {result.question}")
    else:
        console.print(f"[red]Failed:[/red] {result.error}")
        raise typer.Exit(1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_dev.py -v`
Expected: PASS

- [ ] **Step 5: Write test for poller command group**

```python
# Add to tests/test_cli_dev.py

class TestPollerCommands:
    def test_poller_start_help(self) -> None:
        """Should show help for poller start."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["poller", "start", "--help"])

        assert result.exit_code == 0
        assert "--daemon" in result.output

    def test_poller_status(self) -> None:
        """Should show poller status."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["poller", "status"])

        # Should not crash, status depends on state
        assert result.exit_code in [0, 1]
```

- [ ] **Step 6: Add poller command group**

```python
# Add to src/dev_sync/cli.py after bridge_app

# Poller subcommand group
poller_app = typer.Typer(help="GitHub issue poller commands.")
app.add_typer(poller_app, name="poller")


def _get_poller_pid_file(config_path: str) -> Path:
    """Get PID file for poller process."""
    try:
        config = load_config(config_path)
        return config.paths.state_db.parent / "poller.pid"
    except ConfigError:
        return Path("~/.dev-sync/poller.pid").expanduser()


@poller_app.command("start")
def poller_start(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
    daemon: bool = typer.Option(
        False,
        "--daemon",
        "-d",
        help="Run in background",
    ),
    interval: int = typer.Option(
        300,
        "--interval",
        "-i",
        help="Poll interval in seconds",
    ),
) -> None:
    """Start the GitHub issue poller."""
    import os
    import subprocess
    import sys

    try:
        config = load_config(config_path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    pid_file = _get_poller_pid_file(config_path)

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[yellow]Poller already running (PID {pid})[/yellow]")
            raise typer.Exit(1)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    if daemon:
        console.print("[yellow]Daemon mode not yet implemented[/yellow]")
        raise typer.Exit(1)
    else:
        console.print(f"Starting poller with {interval}s interval...")
        console.print("Press Ctrl+C to stop")
        # TODO: Implement foreground poller loop
        console.print("[yellow]Poller loop not yet implemented[/yellow]")


@poller_app.command("stop")
def poller_stop(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Stop the GitHub issue poller."""
    import os
    import signal

    pid_file = _get_poller_pid_file(config_path)

    if not pid_file.exists():
        console.print("[yellow]Poller not running (no PID file)[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Stopped poller (PID {pid})[/green]")
        pid_file.unlink(missing_ok=True)
    except ProcessLookupError:
        console.print("[yellow]Poller process not found[/yellow]")
        pid_file.unlink(missing_ok=True)
    except ValueError:
        console.print("[red]Invalid PID file[/red]")
        pid_file.unlink(missing_ok=True)


@poller_app.command("status")
def poller_status(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Check poller status."""
    import os

    pid_file = _get_poller_pid_file(config_path)

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[green]Poller running (PID {pid})[/green]")
            return
        except (ProcessLookupError, ValueError):
            pass

    console.print("[dim]Poller not running[/dim]")
```

- [ ] **Step 7: Run all tests and commit**

Run: `pytest tests/test_cli_dev.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/cli.py tests/test_cli_dev.py
git commit -m "feat(cli): add dev pipeline and poller commands"
```

---

### Task 8: Update Exports

**Files:**
- Modify: `src/dev_sync/core/__init__.py`
- Modify: `src/dev_sync/pipelines/__init__.py`

- [ ] **Step 1: Update core exports**

```python
# Add to src/dev_sync/core/__init__.py imports
from dev_sync.core.poller import IssuePoller
from dev_sync.core.pr_watcher import PRWatcher

# Add to __all__
"IssuePoller",
"PRWatcher",
```

- [ ] **Step 2: Update pipelines exports**

```python
# Add to src/dev_sync/pipelines/__init__.py imports
from dev_sync.pipelines.dev import DevPipeline, run_dev_issue

# Add to __all__
"DevPipeline",
"run_dev_issue",
```

- [ ] **Step 3: Verify imports work**

```bash
python -c "from dev_sync.core import IssuePoller, PRWatcher; from dev_sync.pipelines import DevPipeline, run_dev_issue; print('Imports OK')"
```

- [ ] **Step 4: Commit**

```bash
git add src/dev_sync/core/__init__.py src/dev_sync/pipelines/__init__.py
git commit -m "feat: export Phase 4 modules"
```

---

### Task 9: Integration Test

**Files:**
- Create: `tests/test_dev_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_dev_integration.py
"""Integration test for dev pipeline."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDevIntegration:
    @pytest.mark.asyncio
    async def test_full_dev_flow_with_mocked_claude(self, tmp_path: Path) -> None:
        """Should run full dev flow from issue to PR."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint
        from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult
        from dev_sync.core.github import GitHubCLI
        from dev_sync.core.state import StateDB
        from dev_sync.core.worktree import WorktreeManager
        from dev_sync.pipelines.dev import run_dev_issue

        # Setup state DB
        state_db = StateDB(tmp_path / "state.db")

        # Setup mock worktree
        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        # Create fake bare repo
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)
        (bare_path / "HEAD").write_text("ref: refs/heads/main\n")

        # Mock GitHub
        mock_github = AsyncMock(spec=GitHubCLI)
        mock_github.get_issue.return_value = {
            "number": 123,
            "title": "Fix the login bug",
            "body": "Users cannot log in when...",
        }

        # Mock dispatcher
        mock_dispatcher = AsyncMock(spec=ClaudeDispatcher)

        async def mock_spawn_session(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "PR #42 opened for issue #123",
                "outputs": {
                    "pr_url": "https://github.com/owner/repo/pull/42",
                    "pr_number": 42,
                },
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        mock_dispatcher.spawn_session.side_effect = mock_spawn_session

        # Mock worktree methods
        with patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock), \
             patch.object(worktree, "create_worktree_with_new_branch", new_callable=AsyncMock) as mock_create, \
             patch.object(worktree, "remove_worktree", new_callable=AsyncMock), \
             patch.object(worktree, "symlink_context"), \
             patch.object(worktree, "remove_context_symlink"):

            worktree_path = tmp_path / "worktrees" / "test-worktree"
            worktree_path.mkdir(parents=True)
            mock_create.return_value = worktree_path

            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=123,
                branch_template="fix/issue-{n}",
                dispatcher=mock_dispatcher,
                github=mock_github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
            )

        # Verify result
        assert result.success
        assert result.outputs["pr_number"] == 42

        # Verify branch was created with correct name
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["new_branch"] == "fix/issue-123"

        # Verify session was recorded
        rows = state_db.execute("SELECT * FROM sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["issue_number"] == 123

        state_db.close()
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/test_dev_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_dev_integration.py
git commit -m "test: add dev pipeline integration test"
```

---

### Task 10: Linting and Final Validation

- [ ] **Step 1: Run ruff check**

```bash
ruff check src/dev_sync/core/poller.py src/dev_sync/core/pr_watcher.py src/dev_sync/pipelines/dev.py
```

- [ ] **Step 2: Fix any linting issues**

```bash
ruff check --fix src/dev_sync/core/poller.py src/dev_sync/core/pr_watcher.py src/dev_sync/pipelines/dev.py
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All tests PASS

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "fix: address linting issues in Phase 4"
```

---

### Task 11: Post-Merge Handler

**Files:**
- Create: `src/dev_sync/pipelines/post_merge.py`
- Test: `tests/test_post_merge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_post_merge.py
"""Tests for post-merge handler."""

from unittest.mock import AsyncMock

import pytest


class TestPostMergeHandler:
    @pytest.mark.asyncio
    async def test_handle_merge_closes_issue(self) -> None:
        """Should close issue after successful merge."""
        from dev_sync.pipelines.post_merge import handle_merge

        mock_github = AsyncMock()
        mock_transport = AsyncMock()

        await handle_merge(
            repo="owner/repo",
            pr_number=42,
            issue_number=123,
            github=mock_github,
            transport=mock_transport,
        )

        mock_github.close_issue.assert_called_once_with(
            "owner/repo",
            123,
            "Closed by PR #42",
        )
        mock_transport.send.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_post_merge.py -v`
Expected: FAIL with "No module named 'dev_sync.pipelines.post_merge'"

- [ ] **Step 3: Implement handle_merge**

```python
# src/dev_sync/pipelines/post_merge.py
"""Post-merge handling for dev pipeline."""

from __future__ import annotations

from dev_sync.core.github import GitHubCLI
from dev_sync.transports.base import Transport


async def handle_merge(
    repo: str,
    pr_number: int,
    issue_number: int,
    github: GitHubCLI,
    transport: Transport | None = None,
) -> None:
    """Handle post-merge actions.

    Args:
        repo: Repository name (owner/repo)
        pr_number: Merged PR number
        issue_number: Issue number to close
        github: GitHub CLI wrapper
        transport: Optional transport for notifications
    """
    # Close the issue with reference to PR
    await github.close_issue(
        repo,
        issue_number,
        f"Closed by PR #{pr_number}",
    )

    # Send notification
    if transport:
        await transport.send(
            f"Issue #{issue_number} closed after PR #{pr_number} merged in {repo}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_post_merge.py -v`
Expected: PASS

- [ ] **Step 5: Write test for watch_and_handle_merge**

```python
# Add to tests/test_post_merge.py

@pytest.mark.asyncio
async def test_watch_and_handle_merge(self) -> None:
    """Should watch for merge then close issue."""
    from dev_sync.pipelines.post_merge import watch_and_handle_merge

    mock_github = AsyncMock()
    mock_github.get_pr_state.return_value = {"state": "MERGED"}

    mock_transport = AsyncMock()

    result = await watch_and_handle_merge(
        repo="owner/repo",
        pr_number=42,
        issue_number=123,
        github=mock_github,
        transport=mock_transport,
        poll_interval=1,
        timeout=5,
    )

    assert result is True
    mock_github.close_issue.assert_called_once()
```

- [ ] **Step 6: Implement watch_and_handle_merge**

```python
# Add to src/dev_sync/pipelines/post_merge.py

from dev_sync.core.pr_watcher import PRWatcher


async def watch_and_handle_merge(
    repo: str,
    pr_number: int,
    issue_number: int,
    github: GitHubCLI,
    transport: Transport | None = None,
    poll_interval: int = 60,
    timeout: int = 86400,
) -> bool:
    """Watch for PR merge and handle post-merge actions.

    Args:
        repo: Repository name
        pr_number: PR number to watch
        issue_number: Issue to close on merge
        github: GitHub CLI wrapper
        transport: Optional transport for notifications
        poll_interval: Seconds between polls
        timeout: Max seconds to wait

    Returns:
        True if merged and handled, False if timeout
    """
    watcher = PRWatcher(github=github, poll_interval=poll_interval)

    merged = await watcher.wait_for_merge(repo, pr_number, timeout=timeout)

    if merged:
        await handle_merge(
            repo=repo,
            pr_number=pr_number,
            issue_number=issue_number,
            github=github,
            transport=transport,
        )
        return True

    return False
```

- [ ] **Step 7: Run all tests and commit**

Run: `pytest tests/test_post_merge.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/pipelines/post_merge.py tests/test_post_merge.py
git commit -m "feat(pipelines): add post-merge handler"
```

---

### Task 12: Poller Loop Implementation

**Files:**
- Modify: `src/dev_sync/core/poller.py`
- Modify: `src/dev_sync/cli.py`
- Test: `tests/test_poller.py`

- [ ] **Step 1: Write test for run_poll_loop**

```python
# Add to tests/test_poller.py

@pytest.mark.asyncio
async def test_run_poll_loop_processes_new_issues(self, tmp_path: Path) -> None:
    """Should call handler for each new issue."""
    from dev_sync.core.poller import IssuePoller, run_poll_loop

    mock_github = AsyncMock()
    mock_github.list_assigned_issues.return_value = [
        {"number": 123, "title": "Fix bug"},
    ]

    poller = IssuePoller(
        github=mock_github,
        username="testuser",
        repos=["owner/repo"],
        state_file=tmp_path / "poller_state.json",
    )

    handled_issues = []

    async def handler(repo: str, issue: dict) -> None:
        handled_issues.append((repo, issue["number"]))

    # Run one iteration
    await run_poll_loop(
        poller=poller,
        handler=handler,
        max_iterations=1,
    )

    assert len(handled_issues) == 1
    assert handled_issues[0] == ("owner/repo", 123)
```

- [ ] **Step 2: Implement run_poll_loop**

```python
# Add to src/dev_sync/core/poller.py

import asyncio
from typing import Callable, Awaitable


async def run_poll_loop(
    poller: IssuePoller,
    handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    interval: int = 300,
    max_iterations: int | None = None,
) -> None:
    """Run the polling loop.

    Args:
        poller: IssuePoller instance
        handler: Async function to call for each new issue (repo, issue)
        interval: Seconds between polls
        max_iterations: Max iterations (None = infinite)
    """
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        new_issues = await poller.poll()

        for item in new_issues:
            await handler(item["repo"], item["issue"])

        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            await asyncio.sleep(interval)
```

- [ ] **Step 3: Update CLI poller start to use run_poll_loop**

Replace the TODO in poller_start with actual implementation:

```python
# In src/dev_sync/cli.py, update poller_start command's else branch:

else:
    import asyncio
    from dev_sync.core.github import GitHubCLI
    from dev_sync.core.poller import IssuePoller, run_poll_loop
    from dev_sync.pipelines.dev import run_dev_issue
    from dev_sync.core.dispatcher import ClaudeDispatcher
    from dev_sync.core.state import StateDB
    from dev_sync.core.worktree import WorktreeManager

    github = GitHubCLI()
    state_db = StateDB(config.paths.state_db)
    dispatcher = ClaudeDispatcher(claude_binary=config.claude.binary)
    worktree = WorktreeManager(
        worktrees_dir=config.paths.worktrees,
        bare_repos_dir=config.paths.bare_repos,
    )

    # Get GitHub username
    import subprocess
    username = subprocess.check_output(
        ["gh", "api", "user", "--jq", ".login"],
        text=True,
    ).strip()

    poller = IssuePoller(
        github=github,
        username=username,
        repos=[r.name for r in config.repos],
        state_file=config.paths.state_db.parent / "poller_state.json",
    )

    async def handle_issue(repo: str, issue: dict) -> None:
        repo_config = next((r for r in config.repos if r.name == repo), None)
        if not repo_config:
            return
        console.print(f"[cyan]New issue:[/cyan] {repo} #{issue['number']}: {issue['title']}")
        await run_dev_issue(
            repo=repo,
            issue_number=issue["number"],
            branch_template=repo_config.dev_branch_template,
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=config.paths.contexts,
        )

    console.print(f"Starting poller with {interval}s interval...")
    console.print("Press Ctrl+C to stop")

    try:
        asyncio.run(run_poll_loop(poller, handle_issue, interval=interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping poller...[/yellow]")
    finally:
        state_db.close()
```

- [ ] **Step 4: Run tests and commit**

Run: `pytest tests/test_poller.py -v`
Expected: All tests PASS

```bash
git add src/dev_sync/core/poller.py src/dev_sync/cli.py tests/test_poller.py
git commit -m "feat(poller): implement polling loop with issue handler"
```

---

### Task 13: Final Linting and Validation

- [ ] **Step 1: Run ruff check on all new files**

```bash
ruff check src/dev_sync/core/poller.py src/dev_sync/core/pr_watcher.py src/dev_sync/pipelines/dev.py src/dev_sync/pipelines/post_merge.py
```

- [ ] **Step 2: Fix any issues**

```bash
ruff check --fix src/dev_sync/
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```

- [ ] **Step 4: Commit fixes**

```bash
git add -A
git commit -m "fix: address linting issues in Phase 4"
```

---

## Phase Gate Validation

After completing all tasks:

1. **Self-assign a real issue** to trigger the dev pipeline
2. **Run**: `dev-sync run dev --issue <number> --repo owner/repo`
3. **Verify**: Session recorded in state DB with status
4. **Test blocked flow**: If Claude asks a question, verify it surfaces correctly
5. **Verify PR**: Check that PR was opened with correct branch name
6. **Test poller**: `dev-sync poller start` detects new assigned issues
7. **Test post-merge**: After PR merge, issue should be closed automatically

The phase is complete when:
- Issue poller detects assigned issues and triggers dev pipeline
- Dev pipeline creates branch, runs Claude, opens PR
- Post-merge handler closes issues after PR merge
- Session states are tracked in database
- Telegram notifications work (if configured)
