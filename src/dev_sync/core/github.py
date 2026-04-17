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
