"""GitHub CLI (gh) wrapper for dev-sync."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from typing import Any


class GitHubError(Exception):
    """Raised when gh CLI operations fail."""


def _find_gh() -> str:
    """Find gh binary, checking common paths if not in PATH."""
    gh = shutil.which("gh")
    if gh:
        return gh
    for path in ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"]:
        if shutil.which(path):
            return path
    return "gh"


@dataclass
class GitHubCLI:
    """Async wrapper around the gh CLI."""

    gh_binary: str = field(default_factory=_find_gh)
    timeout: int = 60

    async def _run_gh(self, *args: str, capture_on_nonzero: bool = False) -> str:
        """Run gh command and return stdout.

        By default, non-zero exits raise GitHubError. Some commands
        (notably `gh pr checks`) print their JSON payload to stdout even
        when they exit non-zero (e.g. while checks are still pending).
        Callers that need that payload can set `capture_on_nonzero=True` to
        receive stdout regardless of exit code.
        """
        cmd = [self.gh_binary, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.timeout
        )

        if proc.returncode != 0 and not capture_on_nonzero:
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

    async def list_security_alerts(
        self,
        repo: str,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        """List Dependabot security alerts with pagination."""
        output = await self._run_gh(
            "api",
            "--paginate",
            f"/repos/{repo}/dependabot/alerts",
            "--jq", f'[.[] | select(.state == "{state}")]',
        )
        return json.loads(output) if output.strip() else []

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

    async def get_pr_checks(
        self,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        """Get status checks for a PR.

        Uses `gh pr checks --json name,state,bucket,link`. `bucket` categorizes
        the raw state into: pass, fail, pending, skipping, cancel. `gh pr
        checks` exits non-zero while any check is pending or failing, but it
        still prints the JSON payload to stdout — so we use
        `capture_on_nonzero=True` and parse whatever comes back. Genuine
        failures (auth, network, missing PR) print nothing/garbage and raise
        `json.JSONDecodeError`, which we let surface so callers don't mistake
        them for 'no CI configured'.
        """
        output = await self._run_gh(
            "pr", "checks",
            str(pr_number),
            "--repo", repo,
            "--json", "name,state,bucket,link",
            capture_on_nonzero=True,
        )
        stripped = output.strip()
        if not stripped:
            return []
        return json.loads(stripped)

    def all_checks_passed(self, checks: list[dict[str, Any]]) -> bool:
        """Check if all PR checks passed."""
        if not checks:
            return False
        return all(c.get("bucket") in ("pass", "skipping") for c in checks)

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
            "--json", "number,title,state,body,labels,assignees,createdAt,updatedAt",
        )
        return json.loads(output) if output.strip() else []

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
            "--json",
            "number,title,state,body,labels,assignees,author,createdAt,updatedAt,comments",
        )
        return json.loads(output)

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
            "--json", "number,title,url,state",
        )
        return json.loads(output)

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
            "--json", "number,state,mergeable,mergeStateStatus,title,url,headRefName,baseRefName",
        )
        return json.loads(output)

    async def comment_on_issue(
        self,
        repo: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Post a comment on an issue."""
        await self._run_gh(
            "issue", "comment",
            str(issue_number),
            "--repo", repo,
            "--body", body,
        )

    async def close_issue(
        self,
        repo: str,
        issue_number: int,
        comment: str | None = None,
    ) -> None:
        """Close an issue with an optional comment."""
        if comment is not None:
            await self.comment_on_issue(repo, issue_number, comment)
        await self._run_gh(
            "issue", "close",
            str(issue_number),
            "--repo", repo,
        )
