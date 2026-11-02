"""GitHub CLI (gh) wrapper for ctrlrelay."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ctrlrelay.core.github_budget import GitHubBudget, bucket_for
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


def _iso_utc(ts: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # The account's API budget (#237). When a bucket is used up, calls
    # that spend it fail here at once, as rate limited, until its reset,
    # instead of each going to GitHub to fail there. ``None``: no gate.
    budget: GitHubBudget | None = None

    async def rate_limit(self) -> str:
        """``/rate_limit``, raw. Free to call, and never gated."""
        return await self._run_gh("api", "rate_limit")

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
        bucket = bucket_for(args)
        if self.budget is not None and tuple(args) != ("api", "rate_limit"):
            until = self.budget.blocked_until(bucket)
            if until is not None:
                # Worded so `classify_gh_failure` reads it as rate limited.
                raise GitHubError(
                    f"gh failed: API rate limit exceeded: the {bucket} budget "
                    f"is used up until {_iso_utc(until)}; not calling GitHub"
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
            message = stderr.decode().strip()
            lowered = message.lower()
            if (
                self.budget is not None
                and tuple(args) != ("api", "rate_limit")
                and "api rate limit exceeded" in lowered
                and "secondary" not in lowered
            ):
                # The primary hourly quota, not a short secondary limit.
                self.budget.mark_out(bucket)
            raise GitHubError(f"gh failed: {message}")

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
                "reviewDecision,headRepositoryOwner,headRepository,"
                "baseRefName,url"
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

    async def search_assigned_issues(
        self,
        assignee: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Open issues assigned to ``assignee`` across every repository,
        in one ``gh search issues`` call (the status page's waiting line,
        #209).

        Each dict gets ``"repo"`` set from ``repository.nameWithOwner`` so
        callers can treat it like a per-repo ``gh issue list`` row. 1000 is
        the most GitHub's search returns; a result of exactly ``limit`` rows
        may have been cut short, and the caller must treat it so. Read only;
        raises ``GitHubError`` when ``gh`` fails.
        """
        output = await self._run_gh(
            "search", "issues",
            "--assignee", assignee,
            "--state", "open",
            "--limit", str(limit),
            "--json", "repository,number,title,labels,assignees,updatedAt",
        )
        issues = json.loads(output) if output.strip() else []
        for issue in issues:
            issue["repo"] = (issue.get("repository") or {}).get("nameWithOwner", "")
        return issues

    async def search_open_issues(
        self,
        *,
        repos: Sequence[str],
        assignee: str | None = None,
        label: str | None = None,
        limit: int = 1000,
    ) -> dict[str, list[dict[str, Any]]]:
        """Open issues in ``repos`` assigned to ``assignee`` or carrying
        ``label``, in ONE ``gh search issues`` call rather than one call per
        repo (#282): the poller's intake, whatever the number of repos.

        The search is scoped to the owners of ``repos`` and the rows are
        then kept only for ``repos`` (matched without regard to case), keyed
        by the configured name. Every configured repo is a key, with ``[]``
        when nothing matched, so a caller can tell "looked, found nothing"
        from "did not look": a failed search raises ``GitHubError`` and
        returns nothing. A result of exactly ``limit`` rows may have been
        cut short, so it raises too rather than pass for complete.
        """
        if (assignee is None) == (label is None):
            raise ValueError("pass exactly one of assignee or label")
        by_name = {r.lower(): r for r in repos}
        owners = sorted({r.split("/", 1)[0] for r in repos if "/" in r})
        args = [
            "search", "issues", "--state", "open", "--limit", str(limit),
            "--json",
            "repository,number,title,state,body,labels,assignees,createdAt,updatedAt",
        ]
        if assignee is not None:
            args += ["--assignee", assignee]
        else:
            args += ["--label", label]
        for owner in owners:
            args += ["--owner", owner]
        output = await self._run_gh(*args)
        rows = json.loads(output) if output.strip() else []
        if len(rows) >= limit:
            raise GitHubError(
                f"gh search issues returned {len(rows)} rows, the most it "
                "returns, so the answer may be incomplete"
            )
        out: dict[str, list[dict[str, Any]]] = {r: [] for r in repos}
        for row in rows:
            name = (row.get("repository") or {}).get("nameWithOwner", "")
            repo = by_name.get(str(name).lower())
            if repo is not None:
                out[repo].append(row)
        return out

    async def list_label_events(
        self,
        repo: str,
        issue_number: int,
    ) -> list[dict[str, Any]]:
        """``labeled`` events for an issue, oldest first, every page. On a
        ``labeled`` event ``actor`` is the account that added the label
        (unlike an ``assigned`` event, where it is the assignee, #274)."""
        output = await self._run_gh(
            "api", "--paginate",
            f"/repos/{repo}/issues/{issue_number}/events",
            "--jq", '.[] | select(.event=="labeled")',
        )
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    async def current_login(self) -> str:
        """The login ``gh`` is authenticated as (``gh api user``).

        Raises ``GitHubError`` when ``gh`` fails or answers with nothing,
        so an empty answer is never mistaken for a user.
        """
        login = (await self._run_gh("api", "user", "--jq", ".login")).strip()
        if not login:
            raise GitHubError("gh api user returned no login")
        return login

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

        Returns the GitHub issue-events payload filtered to ``event == "assigned"``,
        every page of it. Each entry includes ``assigner`` (who performed the
        assignment) and ``assignee`` (who was assigned); its ``actor`` is the
        assignee too, not who made it (#274). Used by the poller to verify
        that the most recent self-assignment was actually performed by the
        operator.
        """
        # Every page: on an issue with more than one page of events the
        # latest assignment would otherwise be missed.
        output = await self._run_gh(
            "api", "--paginate",
            f"/repos/{repo}/issues/{issue_number}/events",
            "--jq", '.[] | select(.event=="assigned")',
        )
        return [json.loads(line) for line in output.splitlines() if line.strip()]

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
        base: str,
    ) -> dict[str, Any]:
        """Create a pull request; return ``{"number", "url"}``.

        ``gh pr create`` has no ``--json`` flag (it prints the new
        pull request's URL), so the number is read from that URL. The
        old version passed ``--json`` and could never have succeeded;
        nothing called it until #219 moved PR creation into the relay.

        ``base`` is required: a default of ``"main"`` would quietly
        target the wrong branch on a repository whose default differs.
        """
        output = await self._run_gh(
            "pr", "create",
            "--repo", repo,
            "--title", title,
            "--body", body,
            "--head", head,
            "--base", base,
        )
        url = ""
        for line in output.splitlines():
            line = line.strip()
            if "/pull/" in line:
                url = line
        match = re.search(r"/pull/(\d+)\b", url)
        if not match:
            raise GitHubError(
                f"gh pr create returned no pull request URL: {output.strip()[:200]!r}"
            )
        return {"number": int(match.group(1)), "url": url}

    async def get_default_branch(self, repo: str) -> str:
        """The repository's default branch, as GitHub reports it.

        Read from the API rather than from the bare clone's HEAD,
        because the agent can write to the bare clone and must not be
        able to change which branch the push guard protects (#219).
        """
        output = await self._run_gh(
            "repo", "view", repo,
            "--json", "defaultBranchRef",
            "--jq", ".defaultBranchRef.name",
        )
        name = output.strip()
        if not name:
            raise GitHubError(f"could not read the default branch of {repo}")
        return name

    async def get_failed_job_log(
        self, repo: str, job_id: str, *, timeout: int | None = None,
    ) -> str:
        """``gh run view --job <id> --log-failed`` for one Actions job.

        The dev agent used to fetch these itself. It has no GitHub
        login now (#219), so the relay fetches them and puts an
        excerpt in the fix prompt.
        """
        return await self._run_gh(
            "run", "view", "--repo", repo, "--job", str(job_id),
            "--log-failed",
            timeout=timeout,
        )

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

    async def list_issue_comments(
        self, repo: str, issue_number: int,
    ) -> list[dict[str, Any]]:
        """Every comment on an issue, oldest first, from the REST API.

        Not ``gh issue view --json comments``: that carries no numeric
        id and no ``updated_at``, and the claim rules (#245) need both.
        """
        output = await self._run_gh(
            "api", "--paginate",
            f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
            "--jq",
            ".[] | {id, body, created_at, updated_at, user: {login: .user.login}}",
        )
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    async def post_issue_comment(
        self, repo: str, issue_number: int, body: str,
    ) -> int:
        """Post a comment and return its id, which ``comment_on_issue``
        cannot (``gh issue comment`` prints only a URL)."""
        output = await self._run_gh(
            "api", "-X", "POST",
            f"repos/{repo}/issues/{issue_number}/comments",
            "-f", f"body={body}",
            "--jq", ".id",
        )
        try:
            return int(output.strip())
        except ValueError:
            raise GitHubError(
                f"posting a comment returned no id: {output.strip()[:200]!r}"
            ) from None

    async def edit_issue_comment(
        self, repo: str, comment_id: int, body: str,
    ) -> None:
        """Replace the body of an issue comment."""
        await self._run_gh(
            "api", "-X", "PATCH",
            f"repos/{repo}/issues/comments/{comment_id}",
            "-f", f"body={body}",
            "--jq", ".id",
        )

    async def get_login(self) -> str:
        """The login ``gh`` is authenticated as."""
        login = (await self._run_gh("api", "user", "--jq", ".login")).strip()
        if not login:
            raise GitHubError("could not read the GitHub login")
        return login

    async def create_blob(self, repo: str, content: str) -> str:
        """Store ``content`` as a git blob; return its sha (#246)."""
        sha = (await self._run_gh(
            "api", "-X", "POST", f"repos/{repo}/git/blobs",
            "-f", f"content={content}", "-f", "encoding=utf-8", "--jq", ".sha",
        )).strip()
        if not sha:
            raise GitHubError("creating a blob returned no sha")
        return sha

    async def create_ref(self, repo: str, ref: str, sha: str) -> bool:
        """Create ``ref`` at ``sha``. ``True`` if created, ``False`` if it
        already existed (GitHub answers 422 "Reference already exists");
        anything else raises. All-or-nothing on GitHub's side, which is
        what makes it a claim (#246)."""
        try:
            created = (await self._run_gh(
                "api", "-X", "POST", f"repos/{repo}/git/refs",
                "-f", f"ref={ref}", "-f", f"sha={sha}", "--jq", ".ref",
            )).strip()
        except GitHubError as e:
            if "reference already exists" in str(e).lower():
                return False
            raise
        if created != ref:
            # Only GitHub naming the ref back proves it was created.
            raise GitHubError(f"creating {ref} returned {created[:100]!r}")
        return True

    async def get_ref_sha(self, repo: str, ref: str) -> str:
        """The sha ``ref`` points at (``ref`` given as ``refs/...``)."""
        path = ref.removeprefix("refs/")
        return (await self._run_gh(
            "api", f"repos/{repo}/git/ref/{path}", "--jq", ".object.sha",
        )).strip()

    async def get_blob(self, repo: str, sha: str) -> str:
        """A blob's content, base64 as GitHub returns it."""
        return (await self._run_gh(
            "api", f"repos/{repo}/git/blobs/{sha}", "--jq", ".content",
        )).strip()

    async def list_refs(self, repo: str, prefix: str) -> list[str]:
        """Refs whose name starts with ``prefix`` (``refs/...``)."""
        path = prefix.removeprefix("refs/")
        out = await self._run_gh(
            "api", "--paginate", f"repos/{repo}/git/matching-refs/{path}",
            "--jq", ".[].ref",
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def delete_ref(self, repo: str, ref: str) -> None:
        path = ref.removeprefix("refs/")
        await self._run_gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/{path}")

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
