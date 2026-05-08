"""PersonalizationManager — clone, sync, wire symlinks for the
operator's cross-machine personalization repo.

Lifecycle:
    init  — clone the repo (or refuse if checkout already populated),
            check out the per-machine working branch, wire symlinks.
    wire_symlinks — (re-)apply the ``paths`` config; idempotent.
    status — print working-tree state + per-symlink correctness.
    push  — stage allowlisted paths, commit on per-machine branch,
            fetch, rebase onto ``main_branch``. Clean rebase → push
            branch + FF-push ``main_branch``. Conflict → abort rebase
            and surface the conflict files (Telegram escalation comes
            in Slice 2).
    pull  — fetch, rebase local working branch onto origin/<main>,
            FF local main, re-wire symlinks (config-as-code may have
            changed under us).

Design choices that are easy to miss in review:

* The personalization checkout is a regular ``git clone``, NOT a
  ctrlrelay worktree-from-bare. Worktree-per-session semantics fight
  the requirement that Claude writes flow straight to the working tree
  in real time.
* Per-machine branches (``personalization/<node_id>``) are the
  conflict-avoidance mechanism. Each machine pushes its own branch and
  then attempts a fast-forward of ``main``. Two machines pushing
  simultaneously: one wins the FF, the other's next push starts with a
  rebase onto the new ``main`` and tries again — never force-pushes,
  never silently drops the other side's deltas.
* Symlinks point INTO the checkout, not the other way around. ``git
  status`` is therefore the source of truth for "what changed since
  last push" — no filesystem watcher needed.
* ``git`` is invoked synchronously via ``subprocess.run``. The rest of
  the codebase is asyncio but the CLI commands using this manager are
  Typer (sync), so threading an event loop through is overhead with no
  payoff for a tool that runs ``git`` a handful of times per command.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ctrlrelay.core.config import Config, Personalization, PersonalizationPath
from ctrlrelay.personalization.paths import (
    TemplateContext,
    project_slug,
    resolve_template,
)


class PersonalizationError(Exception):
    """Raised on any unrecoverable personalization-sync failure."""


@dataclass(frozen=True)
class SymlinkPlan:
    """One concrete (resolved) source → target wiring decision.

    Produced by ``PersonalizationManager._plan_symlinks`` from the
    user's ``PersonalizationPath`` config + the available
    ``RepoConfig`` rows. ``apply`` turns the plan into a real
    filesystem mutation (idempotent).
    """

    source: Path  # absolute path inside the checkout
    target: Path  # absolute path on disk where the symlink lives
    is_dir: bool  # whether source/target denote a directory (per template)
    repo_name: str | None  # populated for project_scoped entries


@dataclass(frozen=True)
class SymlinkResult:
    """What ``apply`` actually did, for status/diagnostic display."""

    plan: SymlinkPlan
    action: str  # "created" | "already-correct" | "replaced-stale-symlink"
                 # | "skipped-source-missing" | "skipped-real-file-at-target"
    detail: str = ""


@dataclass(frozen=True)
class PushResult:
    success: bool
    summary: str
    conflict_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullResult:
    success: bool
    summary: str
    conflict_files: tuple[str, ...] = ()


class PersonalizationManager:
    """Orchestrates a personalization-sync repo on the local machine."""

    def __init__(self, config: Config) -> None:
        if config.personalization is None:
            raise PersonalizationError(
                "personalization is not configured in orchestrator.yaml"
            )
        self.config = config
        self.cfg: Personalization = config.personalization
        self.checkout_path: Path = self.cfg.checkout_path
        # ``Config.personalization_branch`` is the single source of
        # truth for the per-machine branch (handles default-from-top-
        # level-node_id). Memoize so subsequent git invocations don't
        # re-derive.
        branch = config.personalization_branch()
        if branch is None:
            # Defensive — shouldn't happen given the personalization
            # check above, but explicit beats AttributeError later.
            raise PersonalizationError("personalization_branch is unset")
        self.working_branch: str = branch
        self.main_branch: str = self.cfg.main_branch
        self.repo_url: str = f"https://github.com/{self.cfg.repo}.git"

    # ----- top-level commands ------------------------------------------------

    def init(self) -> str:
        """Clone the personalization repo into ``checkout_path`` and
        wire symlinks. Refuses to overwrite an existing non-empty
        directory; if the checkout already looks like a clone of the
        right repo, falls through to ``wire_symlinks`` so a re-run is
        idempotent.

        Returns a human-readable summary.
        """
        if self.checkout_path.exists():
            if not self._is_existing_checkout_ours():
                raise PersonalizationError(
                    f"checkout_path {self.checkout_path} already exists and is not a "
                    f"clone of {self.cfg.repo}; back it up or remove it before "
                    "running init"
                )
            # Same repo already there — converge to the right branch and
            # re-wire. Useful when ``init`` is re-run after a config change.
            self._ensure_working_branch()
            results = self.wire_symlinks()
            return self._format_init_summary(results, cloned=False)

        self.checkout_path.parent.mkdir(parents=True, exist_ok=True)
        self._git_global("clone", self.repo_url, str(self.checkout_path))
        self._ensure_working_branch()
        results = self.wire_symlinks()
        return self._format_init_summary(results, cloned=True)

    def status(self) -> str:
        """Return a human-readable summary of working-tree state +
        symlink correctness. Read-only; does not fetch from origin.
        """
        if not self.checkout_path.exists():
            return (
                f"checkout_path {self.checkout_path} does not exist; "
                "run `ctrlrelay personalization init`"
            )

        lines: list[str] = []
        lines.append(f"checkout: {self.checkout_path}")
        lines.append(f"repo:     {self.cfg.repo}")
        lines.append(f"branch:   {self.working_branch} (main: {self.main_branch})")

        current = self._current_branch()
        if current != self.working_branch:
            lines.append(
                f"  ⚠  HEAD is on {current!r}, not {self.working_branch!r}"
            )

        porcelain = self._git("status", "--porcelain").strip()
        if porcelain:
            lines.append("dirty working tree:")
            for entry in porcelain.splitlines():
                lines.append(f"  {entry}")
        else:
            lines.append("working tree clean")

        plans = list(self._plan_symlinks())
        if plans:
            lines.append(f"symlinks ({len(plans)} entries):")
            for plan in plans:
                state = self._inspect_symlink(plan)
                lines.append(f"  [{state}] {plan.target} -> {plan.source}")
        else:
            lines.append("no symlinks configured")

        return "\n".join(lines)

    def wire_symlinks(self) -> list[SymlinkResult]:
        """Apply the ``paths`` config to the filesystem. Idempotent.

        For each plan: create missing symlinks, replace pointing-
        elsewhere symlinks, refuse to clobber a real file/dir, skip
        entries whose source doesn't exist in the checkout yet (so a
        partially-populated personalization repo is OK).
        """
        results: list[SymlinkResult] = []
        for plan in self._plan_symlinks():
            results.append(self._apply_symlink(plan))
        return results

    # Bounded retries for the rebase + branch-push + FF-push cycle on
    # FF rejection. Codex review caught the earlier silent-success-on-
    # FF-rejection bug: if origin/main moved between our fetch and our
    # FF-push, the per-machine branch lands on the remote but main
    # stays at the racing machine's commit. Retrying refetches the new
    # main, rebases on top, and tries again. Cap is small because the
    # fleet should be ~3-5 machines and a hot loop here usually means
    # a deeper problem (auth, branch protection, network) that won't
    # fix itself.
    _PUSH_MAX_ATTEMPTS = 3

    def push(self, message: str | None = None) -> PushResult:
        """Commit working-tree changes on the per-machine branch, then
        rebase onto ``origin/<main>`` and push. On rebase conflict,
        abort the rebase and return a ``PushResult`` listing the
        conflict files; the working tree is left in its pre-rebase
        state so the operator can resolve manually.

        On a successful per-branch push but rejected FF of main
        (concurrent push from another node), retries the cycle up to
        ``_PUSH_MAX_ATTEMPTS`` times. Returns ``success=False`` if the
        cycle never lands main — silently reporting success would
        leave this node's commit reachable only via
        ``origin/personalization/<node>``, never on main, which other
        machines wouldn't see.
        """
        self._require_checkout()
        self._ensure_working_branch()

        # Stage and commit once up-front; the commit object stays the
        # same across retries, only the rebase base shifts.
        if self._stage_configured_paths():
            commit_msg = message or "personalization: sync from {}".format(
                self.working_branch
            )
            try:
                self._git("commit", "-m", commit_msg)
            except _GitError as e:
                # ``git commit`` exits 1 with "nothing to commit" when
                # the staged set turns out to be a no-op (e.g. only
                # mode-bit churn). Treat as a no-op so push is
                # idempotent.
                if "nothing to commit" not in (e.stdout + e.stderr).lower():
                    raise

        last_summary = ""
        for attempt in range(1, self._PUSH_MAX_ATTEMPTS + 1):
            try:
                self._git("fetch", "origin", "--prune")
            except _GitError as e:
                return PushResult(
                    success=False,
                    summary=f"fetch failed: {e.stderr.strip() or e.stdout.strip()}",
                )

            rebase = self._git_capturing(
                "rebase", f"origin/{self.main_branch}",
                check=False,
            )
            if rebase.returncode != 0:
                unmerged = self._git(
                    "diff", "--name-only", "--diff-filter=U"
                ).strip()
                self._git_capturing("rebase", "--abort", check=False)
                files = tuple(unmerged.splitlines()) if unmerged else ()
                return PushResult(
                    success=False,
                    summary=(
                        "rebase onto origin/{main} hit conflicts; aborted. "
                        "Resolve the listed files in the checkout, commit, "
                        "then re-run push."
                    ).format(main=self.main_branch),
                    conflict_files=files,
                )

            # Iteration 1: plain push (creates the per-machine branch
            # on the remote if missing, FF otherwise).
            # Iteration 2+: ``--force-with-lease``. After an FF-
            # rejection on the previous iteration, the rebase that
            # opens this iteration rewrites our local commit on top
            # of the new origin/main, so a plain push to the per-
            # machine branch is non-FF on the remote (Codex review
            # pass 2 caught this). Lease checks against the local
            # remote-tracking ref, which the fetch above just
            # refreshed — so this only force-pushes when WE hold the
            # only outstanding update. Per-machine branches are by
            # design owned by exactly one node; an unexpected
            # concurrent update would still be rejected by the lease,
            # exactly as intended.
            push_cmd: list[str] = ["push", "origin", self.working_branch]
            if attempt > 1:
                push_cmd.insert(1, "--force-with-lease")
            try:
                self._git(*push_cmd)
            except _GitError as e:
                return PushResult(
                    success=False,
                    summary=(
                        f"push of {self.working_branch} failed: "
                        f"{e.stderr.strip() or e.stdout.strip()}"
                    ),
                )

            ff_push = self._git_capturing(
                "push", "origin",
                f"{self.working_branch}:{self.main_branch}",
                check=False,
            )
            if ff_push.returncode == 0:
                return PushResult(
                    success=True,
                    summary=(
                        f"pushed {self.working_branch} and fast-forwarded "
                        f"origin/{self.main_branch}"
                        + (
                            f" (after {attempt} attempts)"
                            if attempt > 1 else ""
                        )
                    ),
                )
            # FF rejected — another machine pushed to main between our
            # fetch and our push. Loop and try again on top of the new
            # tip. Capture the last error message in case we exhaust
            # the retry budget.
            last_summary = (
                ff_push.stderr.strip() or ff_push.stdout.strip()
                or "fast-forward of origin/main rejected"
            )

        return PushResult(
            success=False,
            summary=(
                f"push of {self.working_branch} succeeded but origin/"
                f"{self.main_branch} could not be fast-forwarded after "
                f"{self._PUSH_MAX_ATTEMPTS} attempts ({last_summary}); "
                "another node is racing pushes — re-run push to retry"
            ),
        )

    def pull(self) -> PullResult:
        """Fetch and rebase the per-machine branch onto ``origin/<main>``.
        FF-update local main if possible. Re-wire symlinks at the end
        because the config-as-code shipped in the personalization repo
        may have changed.
        """
        self._require_checkout()
        self._ensure_working_branch()

        try:
            self._git("fetch", "origin", "--prune")
        except _GitError as e:
            return PullResult(
                success=False,
                summary=f"fetch failed: {e.stderr.strip() or e.stdout.strip()}",
            )

        rebase = self._git_capturing(
            "rebase", f"origin/{self.main_branch}",
            check=False,
        )
        if rebase.returncode != 0:
            unmerged = self._git("diff", "--name-only", "--diff-filter=U").strip()
            self._git_capturing("rebase", "--abort", check=False)
            files = tuple(unmerged.splitlines()) if unmerged else ()
            return PullResult(
                success=False,
                summary=(
                    "rebase onto origin/{main} hit conflicts; aborted. "
                    "Resolve in the checkout, then re-run pull."
                ).format(main=self.main_branch),
                conflict_files=files,
            )

        # FF local main if it's a strict ancestor of origin/main.
        ff = self._git_capturing(
            "fetch", "origin",
            f"{self.main_branch}:{self.main_branch}",
            check=False,
        )
        # Re-wire (config may have changed; harmless if not).
        self.wire_symlinks()

        ff_note = "" if ff.returncode == 0 else (
            f" (local {self.main_branch} not fast-forwarded — diverged "
            "from origin)"
        )
        return PullResult(
            success=True,
            summary=(
                f"pulled and rebased {self.working_branch} onto "
                f"origin/{self.main_branch}{ff_note}"
            ),
        )

    # ----- internals: symlink planning + apply -------------------------------

    def _plan_symlinks(self) -> Iterable[SymlinkPlan]:
        """Yield resolved (source, target) plans from the config.

        For non-project-scoped paths: one plan with the configured
        source/target, resolved against ``${HOME}`` only.
        For project-scoped paths: one plan per local repo whose
        ``RepoConfig.local_path`` exists on disk. Repos that aren't
        cloned on this machine are silently skipped (lazy wiring is a
        feature — a memory entry for a project this machine doesn't
        have is a no-op until the project arrives).
        """
        for entry in self.cfg.paths:
            if entry.project_scoped:
                yield from self._plan_project_scoped(entry)
            else:
                yield self._plan_global(entry)

    def _plan_global(self, entry: PersonalizationPath) -> SymlinkPlan:
        ctx = TemplateContext()
        source = self.checkout_path / entry.source.lstrip("/")
        target = resolve_template(entry.target, ctx)
        return SymlinkPlan(
            source=source,
            target=target,
            is_dir=entry.target.endswith("/"),
            repo_name=None,
        )

    def _plan_project_scoped(
        self, entry: PersonalizationPath
    ) -> Iterable[SymlinkPlan]:
        for repo in self.config.repos:
            local = repo.local_path
            if local is None or not local.exists():
                continue
            ctx = TemplateContext(
                project=project_slug(repo.name),
                project_local=local,
            )
            source_rel = resolve_template(entry.source, ctx)
            # ``source`` is interpreted relative to the checkout root,
            # but ``resolve_template`` returns an absolute Path on
            # systems where the source happens to start with ``/``.
            # Strip a leading ``/`` if present and re-anchor under the
            # checkout. Any ``${HOME}``/``${PROJECT_LOCAL}`` etc. in
            # the source field would be a config bug — we still defend
            # against it by anchoring.
            source = self.checkout_path / str(source_rel).lstrip("/")
            target = resolve_template(entry.target, ctx)
            yield SymlinkPlan(
                source=source,
                target=target,
                is_dir=entry.target.endswith("/"),
                repo_name=repo.name,
            )

    def _apply_symlink(self, plan: SymlinkPlan) -> SymlinkResult:
        if not plan.source.exists():
            # Tolerated: the user may not have populated this entry on
            # any machine yet. Skip without failing so wire-symlinks is
            # safe to run on a partial repo.
            return SymlinkResult(plan=plan, action="skipped-source-missing")

        # Ensure parent dir of target exists.
        plan.target.parent.mkdir(parents=True, exist_ok=True)

        if plan.target.is_symlink():
            current = plan.target.readlink()
            if current == plan.source or self._same_resolved(current, plan.source):
                return SymlinkResult(plan=plan, action="already-correct")
            # Wrong symlink — replace.
            plan.target.unlink()
            plan.target.symlink_to(plan.source)
            return SymlinkResult(
                plan=plan,
                action="replaced-stale-symlink",
                detail=f"was -> {current}",
            )

        if plan.target.exists():
            # Real file or directory at the target. Refuse — the user
            # must back up and remove. Adopt-flow comes in Slice 2.
            return SymlinkResult(
                plan=plan,
                action="skipped-real-file-at-target",
                detail="back up and remove the existing path before retrying",
            )

        plan.target.symlink_to(plan.source)
        return SymlinkResult(plan=plan, action="created")

    def _inspect_symlink(self, plan: SymlinkPlan) -> str:
        """Read-only counterpart to ``_apply_symlink`` for ``status``."""
        if not plan.source.exists():
            return "source-missing"
        if plan.target.is_symlink():
            current = plan.target.readlink()
            if current == plan.source or self._same_resolved(current, plan.source):
                return "ok"
            return f"wrong-symlink->{current}"
        if plan.target.exists():
            return "real-file-blocking"
        return "missing"

    @staticmethod
    def _same_resolved(a: Path, b: Path) -> bool:
        try:
            return a.resolve() == b.resolve()
        except (OSError, RuntimeError):
            return False

    # ----- internals: git helpers --------------------------------------------

    def _is_existing_checkout_ours(self) -> bool:
        if not (self.checkout_path / ".git").exists():
            return False
        try:
            origin = self._git("remote", "get-url", "origin").strip()
        except _GitError:
            return False
        # Be permissive about the URL form (https vs ssh) but require
        # the owner/repo to match.
        owner_repo = self.cfg.repo.lower()
        return owner_repo in origin.lower()

    def _ensure_working_branch(self) -> None:
        """Make sure HEAD is on ``working_branch``, creating it from
        ``main_branch`` if it doesn't exist locally yet.
        """
        current = self._current_branch()
        if current == self.working_branch:
            return
        # Does the working branch exist locally?
        existing = self._git_capturing(
            "show-ref", "--verify", "--quiet",
            f"refs/heads/{self.working_branch}",
            check=False,
        )
        if existing.returncode == 0:
            self._git("checkout", self.working_branch)
            return
        # Try origin/<working_branch> first (another machine of ours).
        existing_remote = self._git_capturing(
            "show-ref", "--verify", "--quiet",
            f"refs/remotes/origin/{self.working_branch}",
            check=False,
        )
        if existing_remote.returncode == 0:
            self._git(
                "checkout", "-b", self.working_branch,
                f"origin/{self.working_branch}",
            )
            return
        # Brand new — branch off origin's main. ``origin/<main>``
        # rather than the bare local name so a non-default
        # ``main_branch`` (e.g. ``develop``) works on a fresh clone:
        # ``git clone`` only checks out the repository's default
        # branch locally, so ``git checkout -b ... develop`` would
        # fail with "did not match any file(s) known to git". The
        # remote ref is always present after ``fetch``/``clone``.
        self._git(
            "checkout", "-b", self.working_branch,
            f"origin/{self.main_branch}",
        )

    def _current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def _stage_configured_paths(self) -> bool:
        """``git add -A`` each on-disk location of configured sources.

        Returns True if anything got staged. Selective staging keeps
        ad-hoc cruft a user dropped in the checkout out of the sync
        repo's history.

        Uses ``git add -A`` per-path so tracked-file *deletions* are
        also staged (Codex review caught this: filtering on
        ``Path.exists()`` would otherwise leave a file the operator
        deleted in the checkout sitting on the remote forever).

        Tolerates ``pathspec did not match any files`` for paths that
        are neither in the working tree nor tracked — typical for a
        project_scoped entry whose source dir was never populated on
        any machine.
        """
        rels: list[str] = []
        for entry in self.cfg.paths:
            for plan in self._plan_for_entry(entry):
                try:
                    rel = plan.source.relative_to(self.checkout_path)
                except ValueError:
                    continue
                rels.append(str(rel))
        if not rels:
            return False

        for rel in rels:
            result = self._git_capturing("add", "-A", "--", rel, check=False)
            if result.returncode != 0:
                # ``did not match`` is the benign case: path is
                # neither in the working tree nor in the index.
                # Anything else is a real error and should propagate.
                if "did not match" in (result.stderr + result.stdout):
                    continue
                raise _GitError(
                    args=("add", "-A", "--", rel),
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
        staged = self._git("diff", "--cached", "--name-only").strip()
        return bool(staged)

    def _plan_for_entry(self, entry: PersonalizationPath) -> list[SymlinkPlan]:
        if entry.project_scoped:
            return list(self._plan_project_scoped(entry))
        return [self._plan_global(entry)]

    def _require_checkout(self) -> None:
        if not (self.checkout_path / ".git").exists():
            raise PersonalizationError(
                f"no checkout at {self.checkout_path}; run "
                "`ctrlrelay personalization init` first"
            )

    # ----- subprocess wrappers ------------------------------------------------

    def _git(self, *args: str) -> str:
        """Run ``git <args>`` inside the checkout and return stdout.

        Uses ``check=True`` semantics; failures raise ``_GitError``
        carrying captured stderr/stdout for the caller to inspect.
        """
        result = self._git_capturing(*args, check=True)
        return result.stdout

    def _git_capturing(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        return _run_git(
            args,
            cwd=self.checkout_path,
            check=check,
        )

    def _git_global(self, *args: str) -> str:
        """Run ``git <args>`` without a cwd inside the checkout (for
        operations like ``clone`` that target a path that doesn't
        exist yet).
        """
        result = _run_git(args, cwd=None, check=True)
        return result.stdout

    # ----- formatting --------------------------------------------------------

    def _format_init_summary(
        self, results: list[SymlinkResult], *, cloned: bool
    ) -> str:
        lines: list[str] = []
        if cloned:
            lines.append(f"cloned {self.cfg.repo} -> {self.checkout_path}")
        else:
            lines.append(
                f"checkout already at {self.checkout_path}; converged config"
            )
        lines.append(f"working branch: {self.working_branch}")
        if results:
            counts: dict[str, int] = {}
            for r in results:
                counts[r.action] = counts.get(r.action, 0) + 1
            for action, n in sorted(counts.items()):
                lines.append(f"  {action}: {n}")
        else:
            lines.append("  (no symlinks configured)")
        return "\n".join(lines)


# -----------------------------------------------------------------------------
# Module-level helpers (kept outside the class so tests can patch them)


class _GitError(Exception):
    def __init__(
        self, args: tuple[str, ...], returncode: int, stdout: str, stderr: str
    ):
        super().__init__(
            f"git {' '.join(args)} exited {returncode}: {stderr.strip() or stdout.strip()}"
        )
        self.args_run = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_git(
    args: tuple[str, ...] | list[str],
    *,
    cwd: Path | None,
    check: bool,
) -> subprocess.CompletedProcess:
    """Thin wrapper around ``subprocess.run(['git', ...])``.

    Captures both stdout and stderr as text (utf-8 with replacement so
    a stray non-utf-8 byte from a remote can't blow up the orchestrator).
    Sets ``GIT_TERMINAL_PROMPT=0`` so an HTTP auth prompt against a
    private repo fails fast instead of hanging the daemon.
    """
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise _GitError(
            args=tuple(args),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return proc


def remove_symlink(target: Path) -> None:
    """Best-effort symlink removal helper used by the CLI's
    ``unwire`` future command (Slice 2). Kept here so tests can hit it
    without spinning up a full manager.
    """
    if target.is_symlink():
        target.unlink()
    elif target.exists() and not target.is_symlink():
        # Real file/dir — refuse to delete; that's a manual operator
        # decision.
        raise PersonalizationError(
            f"refusing to delete real path at {target}; not a symlink"
        )
    # Missing is fine — nothing to remove.
