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
import re
import shutil
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

    def init(self, *, adopt: bool = True) -> str:
        """Clone the personalization repo into ``checkout_path`` and
        wire symlinks. Refuses to overwrite an existing non-empty
        directory; if the checkout already looks like a clone of the
        right repo, falls through to ``wire_symlinks`` so a re-run is
        idempotent.

        ``adopt=True`` (the default since Slice 2) moves pre-existing
        real files/dirs at target paths into the personalization
        checkout and wires the symlink, eliminating the manual
        adoption dance Slice 1 required. ``adopt=False`` preserves
        the conservative Slice 1 behavior of skipping such targets
        with a "back up and remove" instruction.

        Returns a human-readable summary.
        """
        if self.checkout_path.exists():
            # An empty directory is fine — ``git clone <url> <empty-dir>``
            # works. Treat as not-yet-cloned and fall through.
            is_empty_dir = (
                self.checkout_path.is_dir()
                and not any(self.checkout_path.iterdir())
            )
            if not is_empty_dir:
                if not self._is_existing_checkout_ours():
                    raise PersonalizationError(
                        f"checkout_path {self.checkout_path} already exists "
                        f"and is not a clone of github.com:{self.cfg.repo}; "
                        "back it up or remove it before running init"
                    )
                # Same repo already there — converge to the right
                # branch and re-wire. Useful when ``init`` is re-run
                # after a config change. ``_bootstrap_main_if_empty``
                # also runs here because an existing clone of an
                # empty remote still has unborn HEAD; without
                # bootstrap, ``_ensure_working_branch`` would fail
                # rev-parsing HEAD or origin/<main> (Codex pass 10
                # caught this).
                self._bootstrap_main_if_empty()
                self._ensure_working_branch()
                results = self.wire_symlinks(adopt=adopt)
                return self._format_init_summary(results, cloned=False)

        self.checkout_path.parent.mkdir(parents=True, exist_ok=True)
        self._git_global("clone", self.repo_url, str(self.checkout_path))
        self._bootstrap_main_if_empty()
        self._ensure_working_branch()
        results = self.wire_symlinks(adopt=adopt)
        return self._format_init_summary(results, cloned=True)

    def _bootstrap_main_if_empty(self) -> None:
        """Create an initial commit on ``main_branch`` ONLY if the
        remote has zero refs.

        A brand-new GitHub repo (no commits, no default branch) clones
        with an "unborn HEAD" and ``git ls-remote origin`` is empty —
        bootstrap is safe and required. But if the remote has commits
        under a DIFFERENT branch (typical when an existing repo uses
        ``master`` and ``main_branch`` is misconfigured), we'd
        otherwise silently create a parallel ``main_branch`` and push
        it, which is rarely what the user wants. Treat that case as a
        configuration error and surface it loudly with a hint.

        Note: a missing local ``refs/remotes/origin/<main>`` is NOT
        proof the remote lacks the branch — a ``--single-branch`` or
        stale clone may not have fetched it. Always consult
        ``ls-remote`` for the authoritative answer (Codex pass 11
        caught this).
        """
        # Authoritative check against the remote: does it have
        # refs/heads/<main_branch>?
        ls = self._git_capturing("ls-remote", "origin", check=False)
        if ls.returncode != 0:
            # Network / auth failure: surface, don't silently bootstrap.
            raise PersonalizationError(
                f"`git ls-remote origin` failed: "
                f"{ls.stderr.strip() or ls.stdout.strip()}"
            )

        # Parse ``<sha>\trefs/...`` lines.
        remote_refs: list[str] = []
        for line in ls.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                remote_refs.append(parts[1])

        wanted_ref = f"refs/heads/{self.main_branch}"
        if wanted_ref in remote_refs:
            # Remote has main_branch. Make sure our local remote-
            # tracking ref reflects it (single-branch clones may not
            # have it without an explicit fetch) so subsequent
            # operations can use ``origin/<main_branch>``.
            self._git_capturing(
                "fetch", "origin",
                f"+{wanted_ref}:refs/remotes/origin/{self.main_branch}",
                check=False,
            )
            return

        if remote_refs:
            actual = "\n".join("  " + r for r in remote_refs)
            raise PersonalizationError(
                f"remote {self.cfg.repo} has no branch named "
                f"'{self.main_branch}', but the repo is not empty. "
                "Set personalization.main_branch in orchestrator.yaml "
                "to one of the existing remote refs (or rename the "
                "remote branch). Existing refs:\n" + actual
            )

        # Truly empty remote — bootstrap.
        self._ensure_local_identity()
        readme = self.checkout_path / "README.md"
        if not readme.exists():
            readme.write_text(
                "# personalization\n\n"
                "Managed by ctrlrelay personalization sync. "
                "See `ctrlrelay personalization --help`.\n"
            )
        self._git("checkout", "-b", self.main_branch)
        self._git("add", "README.md")
        self._git(
            "commit", "-m",
            "personalization: bootstrap empty repo",
        )
        self._git("push", "-u", "origin", self.main_branch)

    def _ensure_local_identity(self) -> None:
        """Ensure ``user.email`` and ``user.name`` are set on this
        checkout so ``git commit`` doesn't fail with "Author identity
        unknown".

        Only writes when the value is missing — never overrides an
        existing setting (operator's global ``~/.gitconfig`` values
        come through to the cloned repo on default git settings, and
        we don't want to silently shadow them). Used by both
        bootstrap and the regular ``push`` commit path so a fresh
        machine without global identity can still complete the first
        sync.
        """
        if not self._git_capturing(
            "config", "--get", "user.email", check=False
        ).stdout.strip():
            self._git("config", "user.email", "ctrlrelay@local")
        if not self._git_capturing(
            "config", "--get", "user.name", check=False
        ).stdout.strip():
            self._git("config", "user.name", "ctrlrelay")

    def status(self) -> str:
        """Return a human-readable summary of working-tree state +
        symlink correctness. Read-only; does not fetch from origin.

        Safe to run before ``init`` — the same path checks ``init``
        uses to decide whether to clone (missing dir / empty dir /
        non-checkout) all return a friendly "run init" message
        instead of raising. Codex pass 14 caught the gap where an
        existing non-checkout (e.g. empty dir or stray files) would
        fall through to ``git rev-parse`` and traceback.
        """
        if not self.checkout_path.exists():
            return (
                f"checkout_path {self.checkout_path} does not exist; "
                "run `ctrlrelay personalization init`"
            )
        if not (self.checkout_path / ".git").exists():
            return (
                f"checkout_path {self.checkout_path} exists but is not a "
                "git checkout yet; run `ctrlrelay personalization init`"
            )
        # Unborn HEAD (clone of an empty repo, or interrupted init
        # right after ``git clone``): ``rev-parse HEAD`` and
        # ``rev-parse --abbrev-ref HEAD`` would both error. Detect
        # and return the same friendly message instead of letting
        # the error escape (Codex pass 19).
        unborn = self._git_capturing(
            "rev-parse", "--verify", "--quiet", "HEAD",
            check=False,
        )
        if unborn.returncode != 0:
            return (
                f"checkout at {self.checkout_path} has unborn HEAD (no "
                "commits yet); run `ctrlrelay personalization init` to "
                "bootstrap"
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

    def wire_symlinks(self, *, adopt: bool = True) -> list[SymlinkResult]:
        """Apply the ``paths`` config to the filesystem. Idempotent.

        For each plan: create missing symlinks, replace pointing-
        elsewhere symlinks, skip entries whose source doesn't exist
        in the checkout yet, and (when ``adopt=True``, the default)
        adopt pre-existing target files/dirs by moving them into the
        checkout. ``adopt=False`` reverts to Slice 1 behavior:
        refuse to clobber a real file/dir at the target.
        """
        results: list[SymlinkResult] = []
        for plan in self._plan_symlinks():
            results.append(self._apply_symlink(plan, adopt=adopt))
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

        # Reset the index to HEAD before staging. This unstages any
        # ad-hoc ``git add`` the operator (or an interrupted previous
        # run) left in the index, ensuring:
        #   1. our commit's pathspec scope matches the actual diff,
        #   2. ``git rebase`` doesn't refuse with "Your index contains
        #      uncommitted changes". Working-tree changes are
        #      preserved (mixed reset is index-only); the operator's
        #      out-of-allowlist files stay on disk and can be staged/
        #      committed by them manually after push completes. This
        #      pairs with the pathspec-scoped commit (Codex pass 12)
        #      to make the allowlist a hard guarantee.
        self._git("reset", "--mixed")

        # Stage and commit once up-front; the commit object stays the
        # same across retries, only the rebase base shifts.
        configured_pathspecs = self._stage_configured_paths()
        if configured_pathspecs:
            # ``git commit`` errors with "Author identity unknown" if
            # neither global nor local user.email/user.name is set.
            # Bootstrap already does this for the empty-repo path;
            # call it here too so a fresh machine cloning a non-empty
            # personalization repo can complete its first sync.
            self._ensure_local_identity()
            commit_msg = message or "personalization: sync from {}".format(
                self.working_branch
            )
            # ``-- <pathspecs>`` constrains the commit to ONLY the
            # allowlisted paths. Anything an operator manually staged
            # outside the allowlist (Codex pass 12 finding — e.g.
            # interrupted run, ad-hoc ``git add``) stays in the
            # index but does NOT enter the personalization repo's
            # history.
            try:
                self._git(
                    "commit", "-m", commit_msg, "--", *configured_pathspecs
                )
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

            # Push the per-machine branch. Use ``--force-with-lease``
            # whenever the local working branch has diverged from
            # ``origin/<working_branch>`` (i.e., the rebase rewrote
            # commits that origin already had). This is BROADER than
            # just iteration > 1 within this call: if a previous
            # invocation left ``origin/<working_branch>`` updated but
            # ``origin/main`` not fast-forwarded (process interrupted,
            # retry budget exhausted, etc.), the rebase at the top of
            # this iteration rewrites the local commit and a plain
            # push is non-FF on the per-machine branch. Codex review
            # pass 9 caught this state-bridging case. Lease still
            # protects against an unexpected concurrent update to the
            # per-machine branch — per-machine branches are owned by
            # exactly one node by design, so the lease almost always
            # passes here, but if a stray update slips in we fail
            # safely instead of clobbering it.
            push_cmd: list[str] = ["push", "origin", self.working_branch]
            if self._working_branch_diverged_from_origin():
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

    def pull(self, *, adopt: bool = True) -> PullResult:
        """Fetch and rebase the per-machine branch onto ``origin/<main>``.
        FF-update local main if possible. Re-wire symlinks at the end
        because the config-as-code shipped in the personalization repo
        may have changed.

        ``adopt`` is forwarded to :meth:`wire_symlinks`. The interactive
        ``ctrlrelay personalization pull`` defaults to ``True`` (matches
        ``init`` semantics: pre-existing real targets are moved into the
        synced repo). The daemon-scheduled :meth:`auto_pull` passes
        ``False`` so a background sync never silently moves operator
        files.
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
        self.wire_symlinks(adopt=adopt)

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

    def auto_pull(self) -> PullResult:
        """Pull variant for the daemon scheduler.

        Differs from ``pull`` in two ways:

        1. Refuses to run when the working tree is dirty. The
           operator may be mid-edit (writes go straight to the
           personalization repo via the symlinks); rebasing under
           them would lose the unsaved work, fail the rebase, or
           both. Skip-on-dirty is the safe default.
        2. Refuses if no checkout exists yet, returning a benign
           summary instead of raising — the daemon shouldn't crash
           because an operator hasn't run ``init`` on this machine.

        Conflicts during rebase still abort (same as ``pull``); the
        daemon log records the conflict files. The re-wire phase runs
        with ``adopt=False`` — a background sync must never silently
        move operator files. Adoption stays an explicit init-time act.
        """
        if not (self.checkout_path / ".git").exists():
            return PullResult(
                success=True,
                summary="auto-pull skipped: no checkout (run init first)",
            )
        # ``status --porcelain`` outputs nothing for a clean working
        # tree; non-empty means there are uncommitted changes.
        porcelain = self._git("status", "--porcelain").strip()
        if porcelain:
            return PullResult(
                success=True,
                summary=(
                    "auto-pull skipped: working tree dirty "
                    f"({len(porcelain.splitlines())} entries)"
                ),
            )
        return self.pull(adopt=False)

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

    def _apply_symlink(
        self, plan: SymlinkPlan, *, adopt: bool = True
    ) -> SymlinkResult:
        """Apply one symlink wiring decision.

        With ``adopt=True`` (Slice 2 default), a target that is a real
        file/dir AND a missing source triggers ADOPTION: the target is
        moved into the checkout at the source location, then the
        symlink is created. This eliminates the manual "move then
        re-init" dance Slice 1 required.

        Adoption is intentionally narrow: it only fires when the
        source is missing. If BOTH source and target exist as real
        content the manager refuses (``skipped-conflict-both-exist``)
        — that needs operator judgment to reconcile.
        """
        source_exists = plan.source.exists()
        target_is_real = plan.target.exists() and not plan.target.is_symlink()

        if source_exists and target_is_real:
            # Both populated — operator must reconcile manually.
            return SymlinkResult(
                plan=plan,
                action="skipped-conflict-both-exist",
                detail=(
                    "both the personalization repo's source and the on-disk "
                    "target have content; reconcile manually (move one into "
                    "the other or delete one) before re-running init"
                ),
            )

        if not source_exists and target_is_real:
            if not adopt:
                return SymlinkResult(
                    plan=plan,
                    action="skipped-real-file-at-target",
                    detail=(
                        "adopt disabled (--no-adopt or wire(adopt=False)); "
                        "back up and remove the existing path before retrying"
                    ),
                )
            # Type sanity: refuse if target's on-disk shape doesn't
            # match config's trailing-slash declaration. Better to
            # surface the mismatch than to move a file into a slot
            # the config says is a directory.
            actual_is_dir = plan.target.is_dir()
            if plan.is_dir != actual_is_dir:
                expected = "directory" if plan.is_dir else "file"
                actual = "directory" if actual_is_dir else "file"
                return SymlinkResult(
                    plan=plan,
                    action="skipped-target-type-mismatch",
                    detail=(
                        f"config declares {expected} (trailing slash) but "
                        f"on-disk target {plan.target} is a {actual}; "
                        "fix the config or the target before retrying"
                    ),
                )
            # Adopt: move target into source location, then continue
            # the wire path as if source had been there all along.
            plan.source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(plan.target), str(plan.source))
            source_exists = True
            adopted = True
        else:
            adopted = False

        if not source_exists:
            # Source missing AND no real-file target to adopt. Common
            # for partial-repo state where another machine hasn't
            # populated this entry yet. Skip without failing.
            return SymlinkResult(plan=plan, action="skipped-source-missing")

        # Source-side type check (existing Slice 1 invariant).
        actual_is_dir = plan.source.is_dir()
        if plan.is_dir != actual_is_dir:
            expected = "directory" if plan.is_dir else "file"
            actual = "directory" if actual_is_dir else "file"
            return SymlinkResult(
                plan=plan,
                action="skipped-source-type-mismatch",
                detail=(
                    f"config declares {expected} (target/source trailing "
                    f"slash) but on-disk source is a {actual}"
                ),
            )

        plan.target.parent.mkdir(parents=True, exist_ok=True)

        if plan.target.is_symlink():
            current = plan.target.readlink()
            if current == plan.source or self._same_resolved(current, plan.source):
                return SymlinkResult(plan=plan, action="already-correct")
            plan.target.unlink()
            plan.target.symlink_to(plan.source)
            return SymlinkResult(
                plan=plan,
                action="replaced-stale-symlink",
                detail=f"was -> {current}",
            )

        plan.target.symlink_to(plan.source)
        return SymlinkResult(
            plan=plan,
            action="adopted" if adopted else "created",
        )

    def _inspect_symlink(self, plan: SymlinkPlan) -> str:
        """Read-only counterpart to ``_apply_symlink`` for ``status``."""
        if not plan.source.exists():
            return "source-missing"
        if plan.is_dir != plan.source.is_dir():
            expected = "directory" if plan.is_dir else "file"
            actual = "directory" if plan.source.is_dir() else "file"
            return f"source-type-mismatch (config={expected}, on-disk={actual})"
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

    # Parses host + owner/repo out of any common GitHub remote URL:
    #   https://github.com/owner/repo(.git)?
    #   git@github.com:owner/repo(.git)?
    #   git://github.com/owner/repo(.git)?
    #   ssh://git@github.com/owner/repo(.git)?
    # The host capture must be exactly ``github.com`` (case-
    # insensitive). Owner/repo is captured separately and compared
    # against ``cfg.repo``. Codex pass 20 caught that an earlier
    # tail-only match accepted any host; a foreign clone with a
    # matching tail (``https://evil.example/owner/repo.git``) would
    # be wired against, leaking personalization data.
    _ORIGIN_GITHUB_RE = re.compile(
        r"^(?:"
        r"https?://(?P<host_https>[^/]+)/"
        r"|git@(?P<host_ssh>[^:]+):"
        r"|git://(?P<host_git>[^/]+)/"
        r"|ssh://(?:git@)?(?P<host_sshurl>[^/]+)/"
        r")"
        r"(?P<owner_repo>[^/:]+/[^/:]+?)"
        r"(?:\.git)?/?\s*\Z",
        re.IGNORECASE,
    )

    def _is_existing_checkout_ours(self) -> bool:
        """Return True iff ``checkout_path`` is a clone of
        ``github.com:<self.cfg.repo>``.

        Tail-only matching (Codex pass 6 fix) was insufficient: an
        existing checkout whose origin pointed at a non-github host
        with the matching ``owner/repo`` tail would still be
        accepted, and after the origin-URL reset (Codex pass 18 fix)
        the foreign checkout's working tree / branch contents would
        get wired into ``~/.claude/`` and friends. Now we require
        the host to be ``github.com`` exactly. Anything else is
        treated as not-ours and ``init`` refuses, telling the
        operator to back up + remove the directory before retrying
        (Codex pass 20 P1 fix).
        """
        if not (self.checkout_path / ".git").exists():
            return False
        try:
            origin = self._git("remote", "get-url", "origin").strip()
        except _GitError:
            return False
        match = self._ORIGIN_GITHUB_RE.match(origin)
        if not match:
            return False
        host = (
            match.group("host_https")
            or match.group("host_ssh")
            or match.group("host_git")
            or match.group("host_sshurl")
            or ""
        )
        if host.lower() != "github.com":
            return False
        return match.group("owner_repo").lower() == self.cfg.repo.lower()

    def _ensure_working_branch(self) -> None:
        """Make sure HEAD is on ``working_branch``, creating it from
        ``main_branch`` if it doesn't exist on origin or locally.

        Refreshes remote-tracking refs first via ``git fetch origin``
        so an existing-but-stale checkout doesn't miss a per-node
        branch that was created on origin after this clone last
        fetched. Codex pass 16 caught this P1: without the fetch,
        ``init`` on a stale clone would branch off main, then
        ``push`` would later fetch and force-with-lease over commits
        that only lived on the remote per-node branch.
        """
        current = self._current_branch()
        if current == self.working_branch:
            return
        # Local branch already present? Just check it out — even if
        # origin has a divergent copy we'll handle it on push (the
        # rebase/lease logic is built for exactly that).
        existing = self._git_capturing(
            "show-ref", "--verify", "--quiet",
            f"refs/heads/{self.working_branch}",
            check=False,
        )
        if existing.returncode == 0:
            self._git("checkout", self.working_branch)
            return
        # Refresh remote-tracking refs so the next show-ref reflects
        # what origin actually has, not what we last knew. Best-
        # effort — a fetch failure (offline, auth) shouldn't block
        # branching off the local main; later push will surface the
        # network/auth error properly.
        self._git_capturing("fetch", "origin", "--prune", check=False)
        existing_remote = self._git_capturing(
            "show-ref", "--verify", "--quiet",
            f"refs/remotes/origin/{self.working_branch}",
            check=False,
        )
        if existing_remote.returncode == 0:
            # Adopt the remote per-node branch instead of clobbering
            # it later.
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

    def _working_branch_diverged_from_origin(self) -> bool:
        """Return True iff ``origin/<working_branch>`` exists and is
        NOT an ancestor of the local ``working_branch``.

        Used to decide whether the per-machine branch push needs
        ``--force-with-lease``. Plain push works when:
          - origin counterpart doesn't exist yet (first push), OR
          - origin counterpart is an ancestor of local (FF possible).
        Otherwise the rebase has rewritten commits that origin still
        has on the per-machine branch and we need a leased force.
        """
        remote_exists = self._git_capturing(
            "show-ref", "--verify", "--quiet",
            f"refs/remotes/origin/{self.working_branch}",
            check=False,
        ).returncode == 0
        if not remote_exists:
            return False
        # ``--is-ancestor A B`` exits 0 when A is ancestor of B (or
        # equal). If origin is ancestor of local, FF push works.
        is_ancestor = self._git_capturing(
            "merge-base", "--is-ancestor",
            f"origin/{self.working_branch}", self.working_branch,
            check=False,
        ).returncode == 0
        return not is_ancestor

    def _stage_configured_paths(self) -> list[str]:
        """``git add -A`` each on-disk location of configured sources
        and return the pathspecs that should bound the subsequent
        ``git commit``.

        The returned list is non-empty whenever the configured paths
        produced ANY staged change — including tracked-file deletions
        (Codex review pass 1 finding: filtering on ``Path.exists()``
        would otherwise leave deletions out of the commit). The
        caller MUST scope ``git commit`` to these pathspecs (Codex
        pass 12 finding) so anything an operator pre-staged outside
        the allowlist (manual ``git add``, interrupted run) does not
        ride along into the personalization repo's history.

        Tolerates ``pathspec did not match any files`` for paths that
        are neither in the working tree nor tracked — typical for a
        project_scoped entry whose source dir was never populated on
        any machine.

        Returns ``[]`` when nothing in the allowlist changed.
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
            return []

        # Only paths that ``git add`` actually matched (path exists
        # in the working tree or is tracked) survive. The caller
        # passes the survivors to ``git commit -- <paths>``; commit
        # would fail with "pathspec did not match" if we left a
        # never-tracked, never-present path in the list (Codex
        # review pass 13 caught this — a normal partial-repo state
        # where one configured source is populated and another is
        # not blocked the whole push).
        matched: list[str] = []
        for rel in rels:
            result = self._git_capturing("add", "-A", "--", rel, check=False)
            if result.returncode == 0:
                matched.append(rel)
                continue
            if "did not match" in (result.stderr + result.stdout):
                # Benign — path is neither in the working tree nor
                # in the index. Skip it.
                continue
            raise _GitError(
                args=("add", "-A", "--", rel),
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        if not matched:
            return []

        # Only return the pathspecs if SOMETHING in the allowlist
        # actually has staged changes. Pre-staged files outside the
        # allowlist won't show up here because we restrict the diff
        # to the configured pathspecs.
        diff = self._git(
            "diff", "--cached", "--name-only", "--", *matched
        ).strip()
        return matched if diff else []

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


class _GitError(PersonalizationError):
    """Raised when a git subprocess invoked by the manager exits non-zero.

    Subclasses ``PersonalizationError`` so the CLI's ``PersonalizationError``
    handlers catch git failures (auth, network, unborn HEAD reads, etc.)
    and emit a clean error instead of a traceback. Codex pass 15 caught
    that bare ``Exception`` made these escape the CLI wrappers.
    """

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
    # Treat all pathspecs as LITERAL filenames — never as git
    # pathspec magic (``:(top)``, ``:(glob)``, ``:!exclude``, etc.).
    # A config ``source: ":(top)secrets.txt"`` (or any other
    # magic prefix) would otherwise let ``git add -- <rel>`` reach
    # outside the intended allowlist (Codex pass 17 caught this).
    # Forced on the env so every subprocess invoked via ``_run_git``
    # gets the protection — manager-side validators already reject
    # ``:`` in sources but defense-in-depth here is cheap.
    env["GIT_LITERAL_PATHSPECS"] = "1"
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
