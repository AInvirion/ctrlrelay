"""Git worktree management for isolated sessions."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.github import GitHubCLI

_logger = get_logger("core.worktree")


# Open-PR probe (issue #52) retries a handful of times before failing
# closed. Transient gh/network errors are common enough that one-shot
# failure would be noisy; but infinite retry would block retries when
# gh is genuinely down. Three quick attempts strike a balance.
_PR_PROBE_RETRY_ATTEMPTS = 3
_PR_PROBE_RETRY_SLEEP_SECONDS = 1.0
# Per-call gh timeout for the open-PR probe. This probe holds the repo
# lock, so inheriting GitHubCLI's 60s default would let a hung gh
# process block other sessions on the same repo for up to ~3 minutes
# (3 attempts × 60s) before failing closed. 10s is plenty for a
# single pr-list call with --head filter against GitHub.
_PR_PROBE_TIMEOUT_SECONDS = 10

# Network-bound git transfers (bare clone, fetch) get their own, much
# larger budget than the 120s that suits local plumbing. A 1.8 GB repo
# cannot clone or fetch its first time inside two minutes on any normal
# link, and the failure mode was silent: `asyncio.wait_for` raises a
# bare `TimeoutError` whose `str()` is empty, so the sweep reported
# "Error processing <repo>" with no cause attached and wrote neither a
# session row nor a log line. 1800s matches claude's own
# `default_timeout_seconds` — a transfer still running past that is
# genuinely stuck, not merely large.
_GIT_TRANSFER_TIMEOUT_SECONDS = 1800

# Branches ctrlrelay never authors, so rewinding one can never destroy
# local work. Dependabot rebases its branches onto a moved base, which
# rewrites their tips — under a non-force refspec every such rebase is
# a non-fast-forward rejection.
_BOT_BRANCH_REFSPEC = "+refs/heads/dependabot/*:refs/heads/dependabot/*"


def _rejections_are_only_non_fast_forward(stderr: str) -> list[str]:
    """Return the refs a fetch declined to rewind, or ``[]`` if the
    failure was anything else.

    A non-fast-forward rejection is the non-force refspec doing its job:
    it refused to discard local commits. Git still exits 1, which the
    caller must not read as "the fetch failed" — the rest of the fetch
    (the default branch, prunes, new branches) applied normally.

    Anything git considers a real error — ``fatal:`` on a dead remote,
    ``error:`` on a bad object, or a rejection for some other reason —
    returns ``[]`` so the caller raises as before. Failing open here
    would turn an unreachable remote into a silent no-op and let
    pipelines run against a stale tree, which is the bug this module's
    explicit refspec was added to prevent in the first place.
    """
    refs: list[str] = []
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("fatal:", "error:")):
            return []
        if not line.startswith("!"):
            continue  # progress line: "From ...", " * [new branch]", " abc..def"
        if "non-fast-forward" not in low and "non-fast forward" not in low:
            return []  # rejected for some other reason — not ours to swallow
        # " ! [rejected]  <src> -> <dst>  (non-fast-forward)"
        body = line.lstrip("! ")
        if "]" in body:
            body = body.split("]", 1)[1]
        target = body.split("->")[-1].split("(")[0].strip()
        refs.append(target or body.strip())
    return refs


class WorktreeError(Exception):
    """Raised when worktree operations fail."""


class StaleRecreatePartialFailureError(WorktreeError):
    """Raised when `create_worktree_with_new_branch` had committed to the
    stale-merged delete+recreate path (destroying the old local ref)
    and then the subsequent `git worktree add -b` failed partway. The
    exception signals to the caller that any leftover ref on disk was
    introduced by THIS session, so run_dev_issue's FAILED-path cleanup
    must delete it regardless of the pre-call branch_existed_before
    snapshot — the old ref is gone and anything present now is ours.

    Scoped to the exception path so concurrent sessions can't clobber
    each other's ownership state (instance-level flags on the shared
    WorktreeManager were racy under concurrent run_dev_issue calls)."""


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

    async def _run_git(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: int | None = None,
        tolerate_non_fast_forward: bool = False,
        protected_refs: frozenset[str] = frozenset(),
    ) -> str:
        """Run git command and return stdout. `timeout` overrides self.timeout
        for one call — useful for cheap probes that shouldn't inherit the full
        120s default when network is flaky.

        ``tolerate_non_fast_forward`` is for fetches under a non-force
        refspec: git exits 1 when it declines to rewind a diverged local
        branch, even though every other ref updated. See
        :func:`_rejections_are_only_non_fast_forward`.

        ``protected_refs`` names branches whose rejection is never
        tolerated. A declined rewind of the default branch means local
        and origin have diverged there, and every worktree cut from it
        would run against a tree origin no longer has.
        """
        cmd = ["git", *args]
        effective_timeout = timeout if timeout is not None else self.timeout
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            # Re-raise the SAME type (callers filter on TimeoutError) but
            # with a message attached. A bare `asyncio.TimeoutError()`
            # stringifies to "", so every layer above rendered it as
            # empty: the secops sweep reported "Error processing <repo>"
            # with no cause, and the operator got a Telegram alert that
            # named a repo and nothing else.
            raise asyncio.TimeoutError(
                f"git {' '.join(args)} timed out after "
                f"{effective_timeout}s"
                + (f" (cwd={cwd})" if cwd is not None else "")
            ) from None
        except asyncio.CancelledError:
            # Scheduler/shutdown cancel: kill the child BEFORE re-raising
            # so the git subprocess isn't left mutating the bare repo /
            # worktree after the daemon exits. A restarted daemon would
            # otherwise race with a stray `git worktree add` on the
            # same repo. Shield the reaping so a second cancel between
            # kill() and wait() doesn't leak the zombie.
            if proc.returncode is None:
                try:
                    proc.kill()
                    await asyncio.shield(proc.wait())
                except (asyncio.CancelledError, Exception):
                    pass
            raise

        if proc.returncode != 0:
            err = stderr.decode().strip()
            declined = (
                _rejections_are_only_non_fast_forward(err)
                if tolerate_non_fast_forward
                else []
            )
            if not declined:
                raise WorktreeError(f"git failed: {err}")
            stuck = sorted(set(declined) & protected_refs)
            if stuck:
                raise WorktreeError(
                    f"git failed: default branch diverged from origin "
                    f"({', '.join(stuck)}); refusing to use a stale tree: "
                    f"{err}"
                )
            log_event(
                _logger,
                "worktree.fetch.non_fast_forward",
                cwd=str(cwd) if cwd is not None else None,
                refs=declined,
                ref_count=len(declined),
            )

        return stdout.decode()

    def _get_bare_repo_path(self, repo: str) -> Path:
        """Get path to bare repo clone."""
        repo_name = repo.replace("/", "-")
        return self.bare_repos_dir / f"{repo_name}.git"

    def _get_worktree_path(self, repo: str, session_id: str) -> Path:
        """Get path for a worktree."""
        repo_name = repo.replace("/", "-")
        return self.worktrees_dir / f"{repo_name}-{session_id}"

    async def get_default_branch(self, repo: str) -> str:
        """Get the default branch for a repo from bare clone."""
        bare_path = self._get_bare_repo_path(repo)
        output = await self._run_git("symbolic-ref", "HEAD", cwd=bare_path)
        return output.strip().replace("refs/heads/", "")

    async def create_worktree(
        self,
        repo: str,
        session_id: str,
        branch: str | None = None,
    ) -> Path:
        """Create a worktree for a session."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        if branch is None:
            branch = await self.get_default_branch(repo)

        await self._run_git(
            "worktree", "add",
            str(worktree_path),
            branch,
            cwd=bare_path,
        )

        return worktree_path

    async def create_worktree_with_new_branch(
        self,
        repo: str,
        session_id: str,
        new_branch: str,
        base_branch: str | None = None,
        github: GitHubCLI | None = None,
    ) -> tuple[Path, bool]:
        """Create a worktree for ``new_branch``.

        If ``new_branch`` already exists in the bare repo — e.g. because a
        previous session for the same issue ran out of fix attempts and left
        its PR branch pushed to origin — reuse that branch so the retry can
        iterate on the prior commits instead of hitting
        ``fatal: a branch named 'fix/issue-N' already exists`` from
        ``git worktree add -b``. Without this, any issue that exhausts the
        verify-fix loop once gets permanently wedged until someone manually
        deletes the branch.

        When we have to fall back to a brand-new branch, it's cut from
        ``base_branch`` (default: the repo's default branch).

        Returns ``(worktree_path, created_fresh)`` where ``created_fresh``
        is True when THIS call either created a brand-new branch from
        ``base_branch`` or detected a stale fully-merged local branch,
        deleted it, and recreated it. Callers use this flag to decide
        whether cleanup on session failure should delete the branch
        (issue #51): a pre-snapshotted ``branch_exists_locally`` check
        goes stale the moment we delete+recreate, so the ownership
        signal has to come from the function that performed the
        mutation.

        When ``github`` is provided and the branch already exists locally,
        probe GitHub for an open PR with ``new_branch`` as head. If one
        exists, refuse to reuse the branch — the reviewer's already-
        reviewed work must not be hijacked by a fresh session, and
        ``gh pr create`` would later fail with "A pull request already
        exists" (issue #52). Raises :class:`WorktreeError` with the PR
        number and a concrete operator action. The probe runs BEFORE
        any ref mutations so a concurrent PR is surfaced cleanly.
        """
        # Per-call flag: tracks whether we committed to the stale-merged
        # delete+recreate path. Must stay local to this coroutine call —
        # placing it on self would race under concurrent sessions sharing
        # the WorktreeManager instance (one session's start-of-call reset
        # would clobber another's in-progress signal).
        entered_stale_recreate = False

        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        if await self.branch_exists_locally(repo, new_branch):
            # CRITICAL safety: never mutate or delete a branch that's still
            # checked out by another linked worktree (e.g. a BLOCKED session
            # that kept its worktree alive on purpose so it can be resumed
            # when the operator replies). `git update-ref` / `update-ref -d`
            # don't refuse in that case; they'd leave the live worktree's
            # HEAD pointing at a stale or missing ref, corrupting the
            # resumable state. Surface a clean error instead.
            if await self._branch_is_checked_out_elsewhere(bare_path, new_branch):
                raise WorktreeError(
                    f"Branch {new_branch!r} is already checked out in another "
                    f"worktree of {repo!r}. Resolve with `git worktree list` + "
                    "`git worktree remove` in the bare repo before retrying."
                )

            # Issue #52: probe for an open same-repo PR BEFORE any ref
            # mutation. Must run for BOTH on-remote and local-only
            # branches — GitHub can keep a PR open after its head
            # branch is deleted, and our next push would recreate
            # origin/<branch>, re-attaching this session's commits to
            # the reviewer-owned PR (codex round-7). Fail-closed on
            # probe error is the correct trade-off: a flaky gh can
            # delay recovery, but a missed probe silently hijacks a
            # PR. See _refuse_if_branch_backs_open_pr.
            if github is not None:
                await self._refuse_if_branch_backs_open_pr(
                    github, repo, new_branch,
                )

            # Remote presence has to be KNOWN to take either sync-to-origin
            # or stale-merged branches. branch_exists_on_remote is
            # fail-closed (returns True on timeout/auth error) which is
            # good for "should I delete this branch" decisions but wrong
            # here — a transient probe failure would skip the stale-merged
            # recreate path and resurrect already-merged commits. Use the
            # strict variant that raises on probe failure; on error we
            # reuse the local ref unchanged, without mutations.
            try:
                on_remote = await self._branch_exists_on_remote_strict(
                    bare_path, new_branch,
                )
            except Exception:
                await self._worktree_add_with_stale_cleanup(
                    bare_path, worktree_path, new_branch,
                )
                return worktree_path, False

            if on_remote:
                # Remote exists → sync local to remote head (preserving
                # unpushed ahead-of-origin commits) and reuse.
                await self._sync_reused_branch_to_origin(bare_path, new_branch)
                await self._worktree_add_with_stale_cleanup(
                    bare_path, worktree_path, new_branch,
                )
                return worktree_path, False

            # Local-only (confirmed). Distinguish between:
            #   (a) stale-merged: the prior PR was merged (any strategy) and
            #       the remote branch auto-deleted. The local ref's commits
            #       are all patch-equivalent to commits in the default
            #       branch. Reusing it would resurrect already-merged
            #       changes into the next PR — delete + create fresh.
            #   (b) recoverable unpushed work: the prior session made
            #       commits locally and died before push. Local has
            #       content not represented in the default branch — reuse
            #       so the operator's work isn't silently dropped.
            if await self._branch_is_fully_merged(repo, new_branch):
                # Commit to the delete+recreate path. From here on,
                # any ref left on disk — whether the old one we just
                # deleted, a partial one `worktree add -b` wrote
                # before crashing, or the successfully recreated one
                # — belongs to THIS session. Flip the local flag
                # BEFORE the delete so even an exception in
                # update-ref still surfaces StaleRecreatePartialFailureError
                # to the caller.
                entered_stale_recreate = True
                try:
                    await self._run_git(
                        "update-ref", "-d", f"refs/heads/{new_branch}",
                        cwd=bare_path, timeout=10,
                    )
                except Exception:
                    pass
                # Fall through to the fresh-branch creation below. The
                # delete+recreate path is the exact scenario issue #51
                # covers: the branch that now exists was created by THIS
                # session, so cleanup on failure must treat it as ours.
            else:
                await self._worktree_add_with_stale_cleanup(
                    bare_path, worktree_path, new_branch,
                )
                return worktree_path, False

        if base_branch is None:
            base_branch = await self.get_default_branch(repo)

        try:
            await self._run_git(
                "worktree", "add",
                "-b", new_branch,
                str(worktree_path),
                base_branch,
                cwd=bare_path,
            )
        except Exception as e:
            # If we destroyed the old stale-merged ref on the way here,
            # re-raise as StaleRecreatePartialFailureError so the caller
            # knows any leftover ref belongs to this session. Without
            # this signal, a pre-call branch_existed_before=True
            # snapshot would misclassify the leftover as "not ours"
            # and leak it into the next retry (issue #51).
            if entered_stale_recreate:
                raise StaleRecreatePartialFailureError(str(e)) from e
            raise
        return worktree_path, True

    async def _refuse_if_branch_backs_open_pr(
        self, github: GitHubCLI, repo: str, branch: str,
    ) -> None:
        """Issue #52: raise WorktreeError if ``branch`` is the head of an
        open PR on ``repo``. Probe is fail-closed (on repeated probe
        error, we refuse reuse): the dev workflow pushes to the shared
        branch BEFORE calling ``gh pr create``, so relying on the
        later create to catch a PR-backed branch is unsafe — a
        transient ``gh`` error would let the push hijack the existing
        PR before any failure surfaces.

        ``gh pr list --head`` filters by branch name only, so PRs from
        unrelated repos (external forks, same-owner forks like
        ``acme/repo-fork`` targeting ``acme/repo``) with the same
        branch name would appear in the result and wrongly block
        our reuse. Filter client-side on the FULL head repo identity
        (owner + name) so only PRs whose head actually lives in the
        repo we're about to push to can veto reuse.
        """
        last_exc: Exception | None = None
        prs: list | None = None
        for attempt in range(_PR_PROBE_RETRY_ATTEMPTS):
            try:
                prs = await github.list_prs(
                    repo, state="open", head=branch,
                    timeout=_PR_PROBE_TIMEOUT_SECONDS,
                )
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                if attempt < _PR_PROBE_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_PR_PROBE_RETRY_SLEEP_SECONDS)
        if prs is None:
            raise WorktreeError(
                f"Could not determine whether branch {branch!r} on "
                f"{repo!r} backs an open PR (gh probe failed after "
                f"{_PR_PROBE_RETRY_ATTEMPTS} attempts: "
                f"{type(last_exc).__name__ if last_exc else 'unknown'}). "
                "Refusing reuse to avoid hijacking an open PR. Retry "
                "once gh is reachable, or delete the branch manually "
                "if you're certain no PR is open."
            ) from last_exc
        target_owner, _, target_name = repo.partition("/")
        # GitHub owner/repo names are case-insensitive: config may say
        # "Owner/Repo" while the API returns "owner/repo". Normalize
        # both sides before comparing so a case mismatch doesn't let
        # a colliding PR slip past the veto.
        target_owner_lc = target_owner.lower()
        target_name_lc = target_name.lower()

        def _same_repo(pr: dict) -> bool:
            owner = (pr.get("headRepositoryOwner") or {}).get("login") or ""
            name = (pr.get("headRepository") or {}).get("name") or ""
            return (
                owner.lower() == target_owner_lc
                and name.lower() == target_name_lc
            )

        same_repo_prs = [p for p in prs if _same_repo(p)]
        if not same_repo_prs:
            return
        pr_number = same_repo_prs[0].get("number")
        raise WorktreeError(
            f"Branch {branch!r} already has open PR #{pr_number} on "
            f"{repo!r} — close or merge it before retrying this issue."
        )

    async def _worktree_add_with_stale_cleanup(
        self, bare_path: Path, worktree_path: Path, branch: str,
    ) -> None:
        """``git worktree add <path> <branch>`` with targeted recovery
        if the add fails because a stale (prunable) admin entry still
        claims the branch.

        Try the add. If it succeeds, done. If it fails with "already
        checked out" AND we've already established there's no LIVE
        checkout of this branch (the caller checked
        _branch_is_checked_out_elsewhere), we scope a `git worktree
        prune` to this one stale entry and retry.

        We deliberately do NOT prune up front: an unconditional prune
        in a bare repo whose worktrees_dir is on a removable / network
        path would destroy admin state for unrelated worktrees whose
        paths are temporarily unavailable. Scoping to this specific
        branch via a post-failure recovery keeps the blast radius
        minimal.
        """
        try:
            await self._run_git(
                "worktree", "add", str(worktree_path), branch,
                cwd=bare_path,
            )
            return
        except WorktreeError as e:
            if "already checked out" not in str(e):
                raise
            # Find the stale admin entry for this branch and clear it
            # WITHOUT running repo-wide `git worktree prune` — that would
            # also discard admin state for unrelated prunable worktrees
            # whose paths are temporarily unavailable (removable media,
            # network mounts), making them unrecoverable when the path
            # comes back. Instead, delete just the one admin directory.
            stale_path = await self._find_stale_worktree_path(bare_path, branch)
            if stale_path is None:
                raise
            # Only touch stale entries we OWN — under our managed
            # worktrees_dir — AND only when our worktrees_dir is actually
            # accessible on disk right now. A prunable stanza can also
            # represent a worktree on a temporarily-unavailable path
            # (network mount, removable drive); if the mount is down,
            # every managed worktree under it appears prunable. Deleting
            # their admin state would orphan real checkouts when the
            # mount comes back.
            #
            # Safety gate: require worktrees_dir itself to exist and be
            # a directory. If the whole mount is offline, bail.
            if not self.worktrees_dir.is_dir():
                raise e
            try:
                stale_resolved = Path(stale_path).resolve()
                wt_root = self.worktrees_dir.resolve()
                stale_resolved.relative_to(wt_root)
            except (OSError, ValueError):
                raise e  # not under our dir — unsafe to recover
            # Locate the admin dir via its canonical `gitdir` pointer,
            # NOT by assuming the path basename matches. Git sanitizes
            # names (`foo bar` → `foo-bar`) and disambiguates duplicates
            # (`wt`, `wt1`, …), so Path(stale_path).name can point at the
            # wrong admin dir or a live unrelated worktree's metadata.
            # Each admin dir has a `gitdir` file that points at
            # `<worktree-path>/.git` — that pointer is the source of truth.
            admin_dir = self._resolve_admin_dir(bare_path, stale_path)
            if admin_dir is not None and admin_dir.exists():
                try:
                    shutil.rmtree(admin_dir)
                except OSError:
                    pass
            # Retry the add; if it fails again, let the error surface.
            await self._run_git(
                "worktree", "add", str(worktree_path), branch,
                cwd=bare_path,
            )

    def _resolve_admin_dir(
        self, bare_path: Path, worktree_dir: str,
    ) -> Path | None:
        """Find the bare repo's admin dir whose ``gitdir`` pointer file
        resolves to ``<worktree_dir>/.git``. Returns None if no
        matching entry exists.

        Git may name admin dirs differently from the worktree path
        basename (sanitization, collision-suffix), so the only robust
        way to pair them is to inspect each admin's gitdir pointer.
        """
        worktrees_root = bare_path / "worktrees"
        if not worktrees_root.is_dir():
            return None
        target = str(Path(worktree_dir) / ".git")
        try:
            entries = list(worktrees_root.iterdir())
        except OSError:
            return None
        for admin_dir in entries:
            gitdir_file = admin_dir / "gitdir"
            if not gitdir_file.exists():
                continue
            try:
                pointer = gitdir_file.read_text().strip()
            except OSError:
                continue
            if pointer == target:
                return admin_dir
        return None

    async def _find_stale_worktree_path(
        self, bare_path: Path, branch: str,
    ) -> str | None:
        """If ``refs/heads/<branch>`` is claimed by a worktree entry marked
        ``prunable``, return the path of that entry. Otherwise None.
        Used to confirm that a "already checked out" failure is safe to
        recover from via prune (i.e. the stale entry IS for our branch
        and not a real live checkout).
        """
        try:
            output = await self._run_git(
                "worktree", "list", "--porcelain",
                cwd=bare_path, timeout=10,
            )
        except Exception:
            return None
        target = f"refs/heads/{branch}"
        current_path: str | None = None
        current_branch: str | None = None
        current_prunable = False
        for raw in output.splitlines() + [""]:
            line = raw.strip()
            if not line:
                if (
                    current_branch == target
                    and current_prunable
                    and current_path is not None
                ):
                    return current_path
                current_path = None
                current_branch = None
                current_prunable = False
                continue
            if line.startswith("worktree "):
                current_path = line[9:].strip()
            elif line.startswith("branch "):
                current_branch = line[7:].strip()
            elif line.startswith("prunable"):
                current_prunable = True
        return None

    async def _sync_reused_branch_to_origin(
        self, bare_path: Path, branch: str,
    ) -> None:
        """Fast-forward the local ``branch`` to origin's head using a
        scratch ref so it never touches ``refs/heads/<branch>`` except
        via a safe `update-ref` that's gated by a merge-base ancestor
        check.

        Bare clones from `git clone --bare` have fetch refspec
        `+refs/heads/*:refs/heads/*`, so there are NO
        `refs/remotes/origin/*` tracking refs AND an unscoped
        `git fetch origin <branch>` would overwrite the live local ref
        directly — destroying any unpushed local commits before we get
        to compare. Routing through a scratch ref in
        `refs/ctrlrelay/sync/*` avoids both problems.

        Rules:
          - If local is ancestor of the fetched remote tip → fast-forward.
          - If local is ahead or diverged → leave local alone (ahead
            contains unpushed work; forcing would destroy it).

        All steps are best-effort: any failure (timeout, network,
        permissions, bad ref) falls back to reusing the local ref
        unchanged, so a flaky origin never aborts the retry.
        """
        scratch_ref = f"refs/ctrlrelay/sync/{branch}"
        fetched = False
        try:
            await self._run_git(
                "fetch", "origin",
                f"+refs/heads/{branch}:{scratch_ref}",
                cwd=bare_path, timeout=30,
            )
            fetched = True
        except Exception:
            return
        try:
            # Distinguish three cases cleanly:
            #   a) local is ancestor of remote → local behind → fast-forward
            #   b) remote is ancestor of local → local ahead (unpushed work) → preserve
            #   c) neither → diverged → raise; caller surfaces as session failure
            #      so we don't silently reuse a branch that will fail on push.
            local_behind = False
            local_ahead = False
            try:
                await self._run_git(
                    "merge-base", "--is-ancestor", branch, scratch_ref,
                    cwd=bare_path, timeout=10,
                )
                local_behind = True
            except WorktreeError:
                pass
            except Exception:
                # Timeout or other — play safe and preserve local.
                return
            if not local_behind:
                try:
                    await self._run_git(
                        "merge-base", "--is-ancestor", scratch_ref, branch,
                        cwd=bare_path, timeout=10,
                    )
                    local_ahead = True
                except WorktreeError:
                    pass
                except Exception:
                    return

            if local_behind:
                try:
                    await self._run_git(
                        "update-ref", f"refs/heads/{branch}", scratch_ref,
                        cwd=bare_path, timeout=10,
                    )
                except Exception:
                    pass
            elif local_ahead:
                # Preserve local — ahead means unpushed operator work.
                pass
            else:
                # Diverged: local has commits origin doesn't, and origin has
                # commits local doesn't. Reusing either side silently loses
                # work or produces non-ff pushes. Surface the conflict so
                # the session fails cleanly.
                raise WorktreeError(
                    f"Local branch {branch!r} has diverged from "
                    f"origin/{branch}. Rebase or reset manually in the bare "
                    "repo before retrying."
                )
        finally:
            if fetched:
                try:
                    await self._run_git(
                        "update-ref", "-d", scratch_ref,
                        cwd=bare_path, timeout=10,
                    )
                except Exception:
                    pass

    async def _branch_exists_on_remote_strict(
        self, bare_path: Path, branch: str,
    ) -> bool:
        """Strict variant of branch_exists_on_remote: True if origin
        has the branch, False if it does not, RAISES on probe failure.

        The public branch_exists_on_remote is fail-closed (returns True
        on error) so callers making "safe to delete" decisions err
        on preservation. That semantic is wrong for the reuse path,
        where a transient error must NOT be interpreted as "remote
        exists" — that would skip the stale-merged recreate branch and
        resurrect already-merged commits into a new PR.
        """
        output = await self._run_git(
            "ls-remote", "--heads", "origin", branch,
            cwd=bare_path, timeout=10,
        )
        return bool(output.strip())

    async def _branch_is_checked_out_elsewhere(
        self, bare_path: Path, branch: str,
    ) -> bool:
        """Return True if ``refs/heads/<branch>`` is currently checked out
        by any LIVE linked worktree of this bare repo.

        `git worktree list --porcelain` emits a blank-line-separated
        stanza per worktree. A stanza with a ``prunable <reason>`` line
        is a stale metadata entry — the worktree directory is gone, we
        just haven't run ``git worktree prune`` yet. Such stanzas don't
        represent a real checkout and MUST be ignored here, otherwise a
        crash between ``shutil.rmtree()`` and ``worktree prune`` wedges
        the branch for all future retries.

        Conservative (fails closed): if the probe itself errors, returns
        True — better to refuse mutation than to risk corrupting a live
        worktree.
        """
        try:
            output = await self._run_git(
                "worktree", "list", "--porcelain",
                cwd=bare_path, timeout=10,
            )
        except Exception:
            return True

        target = f"refs/heads/{branch}"
        current_branch: str | None = None
        current_prunable = False
        for raw in output.splitlines() + [""]:  # trailing "" to flush last stanza
            line = raw.strip()
            if not line:
                # End of stanza — check if this one matches and is live.
                if (
                    current_branch == target
                    and not current_prunable
                ):
                    return True
                current_branch = None
                current_prunable = False
                continue
            if line.startswith("branch "):
                current_branch = line[7:].strip()
            elif line.startswith("prunable"):
                current_prunable = True
        return False

    async def _branch_is_fully_merged(self, repo: str, branch: str) -> bool:
        """Return True if every commit reachable from ``branch`` is
        patch-equivalent to something already in the default branch.

        Uses ``git cherry <default> <branch>`` which compares commits
        by patch-id (content), not by SHA — so this catches squash and
        rebase merges too, not just regular merge commits.

        Conservative: on any error (unknown default branch, cherry
        failure) returns False so we preserve the local ref.
        """
        bare_path = self._get_bare_repo_path(repo)
        try:
            default = await self.get_default_branch(repo)
        except Exception:
            return False
        try:
            out = await self._run_git(
                "cherry", default, branch,
                cwd=bare_path, timeout=15,
            )
        except Exception:
            return False
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines:
            # No commits in branch not in default — fully merged / stale.
            return True
        # Each line starts with "+" (unique) or "-" (patch-equivalent in upstream).
        return all(line.startswith("-") for line in lines)

    async def push_branch(self, worktree_path: Path, branch: str) -> None:
        """Push a branch to origin."""
        await self._run_git("push", "-u", "origin", branch, cwd=worktree_path)

    async def ensure_bare_repo(self, repo: str) -> Path:
        """Ensure bare repo exists, cloning if needed.

        Uses an explicit refspec on fetch instead of relying on
        ``remote.origin.fetch`` config. Some bare clones in the wild
        have the refspec missing or empty; ``git fetch --all`` is a
        silent no-op in that case, so repeated calls never update
        ``refs/heads/*``. Worktrees created from that stale bare
        then check out commits days or weeks behind origin —
        exactly what we saw today when a task-pipeline run reported
        test counts from a commit that was two weeks old.

        Refspec: ``refs/heads/*:refs/heads/*`` (no leading ``+``).
        Fast-forward-only on purpose: a force update would clobber
        the dev pipeline's ``create_worktree_with_new_branch`` reuse
        path, which keeps local-ahead-of-origin commits when a prior
        session pushed partial work. Non-force semantics give us the
        right behavior in all three cases:

        - Local branch BEHIND origin (default-branch case, the bug
          we're fixing): fast-forward applies, local catches up.
        - Local branch AHEAD of origin (unpushed dev work): fetch
          refuses to rewind, local stays as-is, dev's reuse logic
          gets to inspect divergence and decide.
        - Local and origin equal: no-op.

        ``--prune`` drops refs that no longer exist on origin so
        deleted branches don't linger forever.

        Two things keep that non-force refspec from failing the
        caller, both learned from a secops sweep that died on
        ``AInvirion/zzsites`` with ``main`` already updated:

        - ``dependabot/*`` is fetched with a force refspec first.
          Dependabot rebases its branches, which rewrites their tips,
          so under a plain refspec every rebase is a rejection. These
          branches are bot-authored and never carry local work, so
          rewinding one destroys nothing. Twelve bare repos were
          holding such a ref when this was found.
        - Any rejection that survives is tolerated rather than raised.
          Git exits 1 on a declined rewind even when every other ref
          applied, and that exit code was aborting the whole session
          for a branch it had correctly protected.

        The default branch is the exception: a rejection there still
        raises. Sessions are cut from it, so tolerating that one would
        swap a loud failure for work done quietly on a stale tree —
        e.g. after a force-push to ``main`` on GitHub.
        """
        bare_path = self._get_bare_repo_path(repo)

        if bare_path.exists():
            try:
                default_branch = await self.get_default_branch(repo)
            except WorktreeError:
                # A bare repo with no symbolic HEAD cannot name a default
                # branch to protect. create_worktree fails on the same
                # lookup, so the stale-tree risk never reaches a session.
                default_branch = ""
            await self._run_git(
                "fetch", "--prune", "origin",
                _BOT_BRANCH_REFSPEC,
                "refs/heads/*:refs/heads/*",
                cwd=bare_path,
                timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
                tolerate_non_fast_forward=True,
                protected_refs=(
                    frozenset({default_branch}) if default_branch
                    else frozenset()
                ),
            )
        else:
            await self._run_git(
                "clone", "--bare",
                f"https://github.com/{repo}.git",
                str(bare_path),
                timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
            )

        return bare_path

    async def remove_worktree(self, repo: str, session_id: str) -> None:
        """Remove a worktree and clean up."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        if bare_path.exists():
            await self._run_git("worktree", "prune", cwd=bare_path)

    async def delete_branch(self, repo: str, branch: str) -> None:
        """Delete a local branch in the bare repo. Best-effort; no-op if absent."""
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            return
        try:
            await self._run_git("branch", "-D", branch, cwd=bare_path)
        except WorktreeError:
            pass

    async def branch_exists_locally(self, repo: str, branch: str) -> bool:
        """Check if `branch` exists as a local ref in the bare repo."""
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            return False
        try:
            await self._run_git(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                cwd=bare_path,
            )
            return True
        except Exception:
            return False

    async def branch_exists_on_remote(self, repo: str, branch: str) -> bool:
        """Return True if `branch` exists on origin. Fail-closed: on any error
        (WorktreeError, asyncio.TimeoutError from _run_git's wait_for, etc.)
        returns True so callers err on the side of NOT deleting."""
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            return True
        try:
            output = await self._run_git(
                "ls-remote", "--heads", "origin", branch,
                cwd=bare_path,
                timeout=10,
            )
            return bool(output.strip())
        except Exception:
            return True

    def _get_gitdir(self, worktree_path: Path) -> Path:
        """Get the real gitdir for a worktree.

        In linked worktrees, .git is a file pointing to the actual gitdir.
        """
        dot_git = worktree_path / ".git"
        if dot_git.is_file():
            content = dot_git.read_text().strip()
            if content.startswith("gitdir: "):
                return Path(content[8:])
        return dot_git

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

        gitdir = self._get_gitdir(worktree_path)
        exclude_file = gitdir / "info" / "exclude"
        if exclude_file.exists():
            content = exclude_file.read_text()
            if "CLAUDE.md" not in content:
                exclude_file.write_text(content.rstrip() + "\nCLAUDE.md\n")

    def remove_context_symlink(self, worktree_path: Path) -> None:
        """Remove CLAUDE.md symlink before git operations."""
        target = worktree_path / "CLAUDE.md"
        if target.is_symlink():
            target.unlink()
