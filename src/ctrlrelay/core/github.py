"""GitHub CLI (gh) wrapper for ctrlrelay."""

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


# Terminal CheckRun conclusions that don't indicate a passing or skipped
# check. Anything completed and not in _PASS_CONCLUSIONS/_SKIP_CONCLUSIONS/
# _CANCEL_CONCLUSIONS (FAILURE, TIMED_OUT, ACTION_REQUIRED, STARTUP_FAILURE,
# STALE, ...) maps to "fail".
_PASS_CONCLUSIONS = frozenset({"SUCCESS"})
_SKIP_CONCLUSIONS = frozenset({"NEUTRAL", "SKIPPED"})
_CANCEL_CONCLUSIONS = frozenset({"CANCELLED"})

_STATUS_CONTEXT_BUCKETS = {
    "SUCCESS": "pass",
    "PENDING": "pending",
    "ERROR": "fail",
    "FAILURE": "fail",
}


def _normalize_check(entry: dict[str, Any]) -> dict[str, Any]:
    """Reduce one `statusCheckRollup` entry to the {name, state, bucket,
    link} shape `gh pr checks --json` used to hand us directly.

    The rollup mixes two GraphQL types: `CheckRun` (GitHub Actions and
    most third-party checks) and `StatusContext` (legacy commit statuses).
    They use different field names for the same concepts, so each gets
    its own bucket derivation mirroring gh's own `pr checks` logic.
    """
    if entry.get("__typename") == "StatusContext":
        state = entry.get("state", "")
        return {
            "name": entry.get("context", "?"),
            "state": state,
            "bucket": _STATUS_CONTEXT_BUCKETS.get(state, "fail"),
            "link": entry.get("targetUrl"),
        }

    status = entry.get("status", "")
    conclusion = entry.get("conclusion")
    if status != "COMPLETED":
        bucket = "pending"
    elif conclusion in _PASS_CONCLUSIONS:
        bucket = "pass"
    elif conclusion in _SKIP_CONCLUSIONS:
        bucket = "skipping"
    elif conclusion in _CANCEL_CONCLUSIONS:
        bucket = "cancel"
    else:
        bucket = "fail"
    return {
        "name": entry.get("name", "?"),
        "state": conclusion or status,
        "bucket": bucket,
        "link": entry.get("detailsUrl"),
    }


@dataclass
class GitHubCLI:
    """Async wrapper around the gh CLI."""

    gh_binary: str = field(default_factory=_find_gh)
    timeout: int = 60

    async def _run_gh(
        self, *args: str, timeout: int | None = None,
    ) -> str:
        """Run gh command and return stdout; raise GitHubError on non-zero.

        ``timeout`` overrides ``self.timeout`` for one call — useful
        for probes (like the open-PR check in worktree reuse) that
        shouldn't inherit the full 60s default when any delay holds
        the repo lock.

        Kills the child and waits for it to reap on timeout so a
        long-running daemon (e.g. 7-day PR-watch loop that retries on
        TimeoutError) doesn't leak subprocesses while the network hangs.
        """
        effective_timeout = (
            self.timeout if timeout is None else timeout
        )
        cmd = [self.gh_binary, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            # Reap the hung child so we don't accumulate zombies across
            # many retries. kill() is SIGKILL on POSIX; wait() returns
            # quickly because the signal is terminal.
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise

        if proc.returncode != 0:
            raise GitHubError(f"gh failed: {stderr.decode().strip()}")

        return stdout.decode()

    async def list_prs(
        self,
        repo: str,
        state: str = "open",
        limit: int = 100,
        head: str | None = None,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """List pull requests for a repository.

        When ``head`` is given, restrict the result to PRs whose head
        branch is ``head`` (passed to ``gh pr list --head``). Used by
        the dev pipeline's worktree reuse path to refuse a branch that
        still backs an open PR (issue #52).

        ``timeout`` overrides the default per call so the reuse probe
        can bound latency — the probe holds the repo lock, so a full
        default-timeout hang would stall every other session on the
        same repo.
        """
        args = [
            "pr", "list",
            "--repo", repo,
            "--state", state,
            "--limit", str(limit),
            "--json", (
                "number,title,author,labels,headRefName,mergeable,"
                "reviewDecision,headRepositoryOwner,headRepository"
            ),
        ]
        if head is not None:
            args.extend(["--head", head])
        output = await self._run_gh(*args, timeout=timeout)
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

        Shells out to `gh pr view --json statusCheckRollup` rather than
        `gh pr checks --json` — the latter's `--json` flag isn't supported
        by every `gh` build in the wild (older/distro-packaged `gh` rejects
        it outright with "unknown flag: --json"), while `pr view --json`
        has been supported since `gh` introduced `--json` at all.

        Bypasses `_run_gh` because we need to inspect stdout, stderr, and
        the exit code independently: `gh pr view` exits 0 with an empty or
        null rollup when the PR has no CI configured, and non-zero with an
        error on stderr for genuine failures (auth, network, missing PR).

        We compute the same pass/fail/pending/skipping/cancel "bucket"
        `gh pr checks` itself derives, from the raw CheckRun/StatusContext
        fields in the rollup.
        """
        cmd = [
            self.gh_binary,
            "pr", "view",
            str(pr_number),
            "--repo", repo,
            "--json", "statusCheckRollup",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise
        stdout = stdout_bytes.decode().strip()
        stderr = stderr_bytes.decode().strip()

        if stdout:
            rollup = json.loads(stdout).get("statusCheckRollup") or []
            return [_normalize_check(entry) for entry in rollup]

        raise GitHubError(f"gh pr view failed: {stderr}")

    def all_checks_passed(self, checks: list[dict[str, Any]]) -> bool:
        """Check if all PR checks passed."""
        if not checks:
            return False
        return all(c.get("bucket") in ("pass", "skipping") for c in checks)

    async def list_assigned_issues(
        self,
        repo: str,
        assignee: str | None,
        state: str = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List issues for a repo, optionally filtered by assignee.

        When ``assignee`` is ``None`` the ``--assignee`` flag is dropped
        and all open issues are returned. Prefer :meth:`list_issues_by_label`
        for label-triggered polling — the unfiltered call caps at
        ``limit`` (default 100) and silently drops anything past that in
        a busy repo. The poller uses the assignee path + per-label
        targeted queries and merges results by issue number.
        """
        args = [
            "issue", "list",
            "--repo", repo,
            "--state", state,
            "--limit", str(limit),
            "--json", "number,title,state,body,labels,assignees,createdAt,updatedAt",
        ]
        if assignee is not None:
            # Insert before ``--state`` to keep the existing call signature
            # shape when assignee is set (makes recorded gh invocations in
            # fixtures / audit logs visually diff-clean).
            args[4:4] = ["--assignee", assignee]
        output = await self._run_gh(*args)
        return json.loads(output) if output.strip() else []

    async def list_issues_by_label(
        self,
        repo: str,
        label: str,
        state: str = "open",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List open issues carrying a specific label.

        Issued per label in ``include_labels`` (issue #80): a single
        ``gh issue list --label`` call filters server-side, so the
        result is bounded by the number of issues wearing that label
        rather than by total open issues in the repo. That keeps the
        label-triggered poll path scale-safe on large repos, where
        fetching all open issues (pre-codex-review behavior) would
        silently cap at 100 and miss labeled issues on later pages.

        ``limit`` defaults to 1000 (vs the 100 default on
        ``list_assigned_issues``) because an opt-in label can, in
        principle, wear many more issues than a single user is
        assigned to. gh paginates internally up to ``--limit`` so one
        call covers the realistic range. If a repo ever has >1000
        open issues sharing the same opt-in label, the operator will
        need a larger limit or more granular labels — it's a
        configuration smell at that scale.

        gh CLI treats ``--label foo --label bar`` as AND; the poller
        therefore runs one call per label and dedupes by issue number
        at the caller.
        """
        args = [
            "issue", "list",
            "--repo", repo,
            "--label", label,
            "--state", state,
            "--limit", str(limit),
            "--json", "number,title,state,body,labels,assignees,createdAt,updatedAt",
        ]
        output = await self._run_gh(*args)
        return json.loads(output) if output.strip() else []

    async def list_assignment_events(
        self,
        repo: str,
        issue_number: int,
    ) -> list[dict[str, Any]]:
        """List ``assigned`` events for an issue in chronological order.

        Returns the GitHub issue-events payload filtered to ``event == "assigned"``.
        Each entry includes ``actor`` (who performed the assignment) and
        ``assignee`` (who was assigned). Used by the poller to verify that the
        most recent self-assignment was actually performed by the operator.
        """
        output = await self._run_gh(
            "api",
            f"/repos/{repo}/issues/{issue_number}/events",
            "--jq", '[.[] | select(.event=="assigned")]',
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
