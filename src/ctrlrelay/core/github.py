"""GitHub CLI (gh) wrapper for ctrlrelay."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from typing import Any

from ctrlrelay.core.obs import get_logger

_logger = get_logger("core.github")


class GitHubError(Exception):
    """Raised when gh CLI operations fail."""


# CheckRun.conclusion values (only present once status == "COMPLETED").
_CHECK_RUN_PASS = frozenset({"SUCCESS", "NEUTRAL"})
_CHECK_RUN_SKIP = frozenset({"SKIPPED"})
_CHECK_RUN_CANCEL = frozenset({"CANCELLED"})
# StatusContext.state values (the legacy commit-status API, e.g. CLA bots).
_STATUS_CONTEXT_PASS = frozenset({"SUCCESS"})
_STATUS_CONTEXT_PENDING = frozenset({"PENDING", "EXPECTED"})


def _normalize_check(entry: dict[str, Any]) -> dict[str, Any]:
    """Map one `statusCheckRollup` entry to the `{name, state, bucket,
    link}` shape `PRVerifier` expects (see #145).

    `statusCheckRollup` mixes two GraphQL shapes: `CheckRun` (Actions
    jobs, CodeQL — `status`/`conclusion`) and `StatusContext` (the
    legacy commit-status API, e.g. CLA bots — `state`). `bucket` is
    the only field the rest of the app actually branches on; `state`
    is carried through for parity/debugging.
    """
    if entry.get("__typename") == "StatusContext":
        state = entry.get("state", "")
        if state in _STATUS_CONTEXT_PASS:
            bucket = "pass"
        elif state in _STATUS_CONTEXT_PENDING:
            bucket = "pending"
        else:
            bucket = "fail"
        return {
            "name": entry.get("context", ""),
            "state": state,
            "bucket": bucket,
            "link": entry.get("targetUrl", ""),
        }

    status = entry.get("status", "")
    conclusion = entry.get("conclusion")
    if status != "COMPLETED":
        bucket = "pending"
        state = status
    elif conclusion in _CHECK_RUN_PASS:
        bucket = "pass"
        state = conclusion
    elif conclusion in _CHECK_RUN_SKIP:
        bucket = "skipping"
        state = conclusion
    elif conclusion in _CHECK_RUN_CANCEL:
        bucket = "cancel"
        state = conclusion
    else:
        bucket = "fail"
        state = conclusion or "FAILURE"
    return {
        "name": entry.get("name", ""),
        "state": state,
        "bucket": bucket,
        "link": entry.get("detailsUrl", ""),
    }


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

    async def repo_is_archived(
        self,
        repo: str,
        timeout: int | None = None,
    ) -> bool:
        """Return whether ``repo`` is archived on GitHub.

        Deliberately strict: anything that isn't a boolean ``isArchived``
        field raises :class:`GitHubError` rather than being coerced. The
        caller (:class:`~ctrlrelay.core.archived.ArchivedRepoTracker`)
        gates every repo the daemon works on, so "we don't know" must
        never be able to look like "archived" — an ambiguous answer has
        to raise so the repo keeps being polled.
        """
        output = await self._run_gh(
            "repo", "view", repo, "--json", "isArchived", timeout=timeout,
        )
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            raise GitHubError(
                f"gh repo view returned non-JSON for {repo}: {e}"
            ) from e
        value = data.get("isArchived") if isinstance(data, dict) else None
        if not isinstance(value, bool):
            raise GitHubError(
                f"gh repo view returned no boolean isArchived for {repo}"
            )
        return value

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

        Uses `gh pr view --json statusCheckRollup` rather than `gh pr
        checks --json ...` — the latter's `--json` flag isn't available
        on `gh` releases before it was added (confirmed absent on gh
        2.45.0, the current Ubuntu 24.04 package with no newer version
        in the distro repos), which made this unconditionally broken on
        those installs regardless of the PR's actual state (#145).

        `gh pr view --json` always exits 0 once the PR itself resolves —
        the check states live in the JSON, not the exit code — so unlike
        the old implementation, a non-zero exit here is always a genuine
        failure (auth, network, missing PR): `_run_gh` already raises
        `GitHubError` for that case. No checks yet (or ever) on the PR
        just means an empty `statusCheckRollup` list — the ambiguity
        between "not registered yet" and "no CI configured" is handled
        by `PRVerifier`'s empty-streak retry, not here.
        """
        stdout = await self._run_gh(
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "statusCheckRollup",
        )
        payload = json.loads(stdout)
        rollup = payload.get("statusCheckRollup") or []
        return [_normalize_check(entry) for entry in rollup]

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

    async def get_issue_or_pr_state(
        self,
        repo: str,
        number: int,
        *,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Return ``{"number", "state", "is_pr", "merged"}`` for a
        number that may name either an issue or a PR.

        A BLOCKED question only ever cites ``#N``; nothing in the text
        says which kind it is. The REST ``issues`` endpoint covers both
        — GitHub models a PR as an issue — so one call answers it
        without guessing and retrying. ``state`` is ``"open"`` or
        ``"closed"``; ``merged`` is only meaningful when ``is_pr``.
        """
        output = await self._run_gh(
            "api", f"repos/{repo}/issues/{number}",
            "--jq",
            '{number: .number, state: .state, '
            'is_pr: (.pull_request != null), '
            'merged: (.pull_request.merged_at != null)}',
            timeout=timeout,
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

    async def comment_on_pr(self, repo: str, pr_number: int, body: str) -> None:
        """Post a comment on a PR."""
        await self._run_gh(
            "pr", "comment", str(pr_number), "--repo", repo, "--body", body
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
