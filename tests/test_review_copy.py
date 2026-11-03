"""#263: the reviewer reads a fresh copy of the pushed commit, never the
agent's worktree.

Measured before the fix: an agent can leave a nested repository in its
worktree (a gitlink plus its own ``.git``) whose config names a program
as ``core.fsmonitor`` or as a clean filter. A plain ``git status`` or
``git diff`` in the worktree recurses into it and runs that program. The
reviewer ran in that worktree, outside the agent sandbox.

Each test that asserts a trap stays quiet in the copy first shows it
fires in the agent's worktree, so quiet means the trap is gone, not that
it never worked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeError, WorktreeManager
from tests._publish_fakes import fake_relay_publish
from tests.test_event_emission import _done_state

REPO = "o/r"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def git_home(tmp_path: Path, monkeypatch) -> Path:
    """A HOME with only an identity in it, and no system config, so the
    machine's own git setup plays no part."""
    home = tmp_path / "home"
    home.mkdir()
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (home / ".gitconfig").write_text(
        "[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n"
    )
    return home


def _trapped_worktree(tmp_path: Path, mgr: WorktreeManager) -> tuple[Path, str, Path]:
    """A bare clone at the manager's path, an agent worktree cut from it,
    and in that worktree a committed gitlink to a nested repository whose
    own config runs a marker script. Returns the worktree, the commit,
    and the marker directory."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    (src / "a").write_text("a\n")
    _git("add", "a", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    bare = mgr._get_bare_repo_path(REPO)
    _git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)

    wt = tmp_path / "agent-wt"
    _git("worktree", "add", "-q", "-b", "run", str(wt), cwd=bare)

    marks = tmp_path / "marks"
    marks.mkdir()
    script = tmp_path / "trap.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marks}/fired-$1"\n')
    script.chmod(0o755)

    sub = wt / "sub"
    sub.mkdir()
    _git("init", "-q", cwd=sub)
    (sub / ".gitattributes").write_text("*.txt filter=trap\n")
    (sub / "f.txt").write_text("x\n")
    _git("config", "core.fsmonitor", f"{script} fsmonitor", cwd=sub)
    _git("config", "filter.trap.clean", f"{script} filter", cwd=sub)
    _git("add", ".gitattributes", "f.txt", cwd=sub)
    _git("commit", "-qm", "s", cwd=sub)
    _git("add", "sub", cwd=wt)
    _git("commit", "-qm", "gitlink", cwd=wt)
    # Dirty the nested repo so a status in the parent recurses into it.
    (sub / "f.txt").write_text("y\n")
    sha = _git("rev-parse", "HEAD", cwd=wt).strip()
    return wt, sha, marks


def _fired_after(cmds: list[list[str]], cwd: Path, marks: Path) -> list[str]:
    for p in marks.iterdir():
        p.unlink()
    for cmd in cmds:
        subprocess.run(["git", *cmd], cwd=cwd, capture_output=True)
    return sorted(p.name for p in marks.iterdir())


READS = [["status"], ["diff"], ["diff", "main...HEAD"], ["log", "-1", "-p"]]


@pytest.mark.asyncio
async def test_the_trap_fires_in_the_agent_worktree_and_not_in_the_copy(
    tmp_path: Path, git_home: Path,
) -> None:
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    wt, sha, marks = _trapped_worktree(tmp_path, mgr)

    # Control: the plant works where the reviewer used to run.
    assert _fired_after(READS, wt, marks), "control: the trap did not fire"

    copy = await mgr.make_review_copy(REPO, sha)
    try:
        assert _git("rev-parse", "HEAD", cwd=copy).strip() == sha
        assert _fired_after(READS, copy, marks) == []
        # The gitlink came across as a commit id only: an empty directory,
        # no nested repository and none of its config.
        assert (copy / "sub").is_dir()
        assert list((copy / "sub").iterdir()) == []
        # The reviewer is given `--base <default branch>`; it must resolve.
        _git("rev-parse", "--verify", "main", cwd=copy)
    finally:
        await mgr.remove_review_copy(copy)
    assert not copy.parent.exists()


@pytest.mark.asyncio
async def test_the_copy_writes_nothing_to_the_bare_clone(
    tmp_path: Path, git_home: Path,
) -> None:
    """It runs while the repo lock may be released for the CI wait, so it
    must not register a worktree or move a ref in the shared clone."""
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    _wt, sha, _marks = _trapped_worktree(tmp_path, mgr)
    bare = mgr._get_bare_repo_path(REPO)
    before = (
        _git("worktree", "list", "--porcelain", cwd=bare),
        _git("for-each-ref", cwd=bare),
    )

    objects = sorted(p.name for p in (bare / "objects").rglob("*"))
    copy = await mgr.make_review_copy(REPO, sha)
    try:
        # Control: a copy was really made, at the pushed commit.
        assert _git("rev-parse", "HEAD", cwd=copy).strip() == sha
        after = (
            _git("worktree", "list", "--porcelain", cwd=bare),
            _git("for-each-ref", cwd=bare),
        )
        assert sorted(p.name for p in (bare / "objects").rglob("*")) == objects
    finally:
        await mgr.remove_review_copy(copy)
    assert after == before


@pytest.mark.asyncio
async def test_a_sha_that_is_not_a_commit_id_is_refused(tmp_path: Path) -> None:
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    mgr._get_bare_repo_path(REPO).mkdir(parents=True)
    for bad in ("main", "--upload-pack=x", "HEAD~1", "c0ffee"):
        with pytest.raises(WorktreeError, match="not a commit id"):
            await mgr.make_review_copy(REPO, bad)
    assert not (tmp_path / "review").exists() or not any(
        (tmp_path / "review").iterdir()
    )


@pytest.mark.asyncio
async def test_a_failed_copy_leaves_no_directory(
    tmp_path: Path, git_home: Path,
) -> None:
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    _wt, sha, _marks = _trapped_worktree(tmp_path, mgr)
    # Control: the directory does get used, so empty-after means cleaned.
    ok = await mgr.make_review_copy(REPO, sha)
    assert ok.parent.parent == tmp_path / "review"
    await mgr.remove_review_copy(ok)
    with pytest.raises(WorktreeError):
        await mgr.make_review_copy(REPO, "f" * 40)  # no such commit
    assert list((tmp_path / "review").iterdir()) == []


@pytest.mark.asyncio
async def test_remove_touches_only_a_review_copy(tmp_path: Path) -> None:
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    victim = tmp_path / "keep" / "repo"
    victim.mkdir(parents=True)
    lookalike = tmp_path / "elsewhere" / "ctrlrelay-review-x" / "repo"
    lookalike.mkdir(parents=True)
    for path in (victim, lookalike):
        with pytest.raises(WorktreeError, match="not a review copy"):
            await mgr.remove_review_copy(path)
        assert path.exists()


@pytest.mark.asyncio
async def test_the_copy_keeps_the_clones_origin(
    tmp_path: Path, git_home: Path,
) -> None:
    """A reviewer that asks the forge about the branch must find the same
    remote it found in the worktree, not the bare clone's local path."""
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    _wt, sha, _marks = _trapped_worktree(tmp_path, mgr)
    bare = mgr._get_bare_repo_path(REPO)
    _git("remote", "set-url", "origin", "https://github.com/o/r.git", cwd=bare)
    copy = await mgr.make_review_copy(REPO, sha)
    try:
        assert _git("remote", "get-url", "origin", cwd=copy).strip() == (
            "https://github.com/o/r.git"
        )
    finally:
        await mgr.remove_review_copy(copy)


# --- through the dev pipeline, where the path is chosen ------------------


async def _dev_run_capturing(tmp_path: Path, monkeypatch, *, code_review, copy):
    """A full `run_dev_issue` with the fakes the event tests use, and a
    reviewer that records the directory it was started in."""
    from ctrlrelay.core.code_review import CodeReviewOutcome, ReviewStatus
    from ctrlrelay.core.pr_verifier import VerificationResult
    from ctrlrelay.pipelines import dev as dev_mod

    seen: list[Path] = []

    async def fake_review(**kw):
        seen.append(kw["worktree_path"])
        return CodeReviewOutcome(
            status=ReviewStatus.REVIEWED, output="ok",
            evidence=("git diff main...HEAD",),
        )

    monkeypatch.setattr(dev_mod, "run_code_review", fake_review)

    worktree = AsyncMock()
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.get_default_branch.return_value = "main"
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    copy(worktree)

    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = _done_state(7)
    github = AsyncMock()
    fake_relay_publish(github, pr_number=7, repo=REPO)
    github.get_issue.return_value = {
        "number": 1, "title": "x", "body": "y", "comments": [],
    }
    verifier = AsyncMock()
    verifier.verify.return_value = VerificationResult(ready=True)

    result = await dev_mod.run_dev_issue(
        claims=None, repo=REPO, issue_number=1,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=github, worktree=worktree, dashboard=None,
        state_db=StateDB(tmp_path / "state.db"), transport=None,
        contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
        code_review=code_review,
    )
    return result, worktree, seen


@pytest.mark.asyncio
async def test_the_reviewer_is_started_in_the_copy_of_the_pushed_commit(
    tmp_path: Path, monkeypatch,
) -> None:
    copy_path = tmp_path / "ctrlrelay-review-x" / "repo"

    def copy(worktree):
        worktree.make_review_copy.return_value = copy_path

    result, worktree, seen = await _dev_run_capturing(
        tmp_path, monkeypatch,
        code_review=SimpleNamespace(method="cli", comment_on_pr=False),
        copy=copy,
    )

    assert result.success, result
    worktree.make_review_copy.assert_awaited_once_with(REPO, "c0ffee")
    assert seen == [copy_path]
    assert tmp_path / "wt" not in seen
    worktree.remove_review_copy.assert_awaited_once_with(copy_path)


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_made_is_reported_not_reviewed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Never a fallback to the agent's worktree: that is the fault."""

    def copy(worktree):
        worktree.make_review_copy.side_effect = WorktreeError("clone failed")

    result, worktree, seen = await _dev_run_capturing(
        tmp_path, monkeypatch,
        code_review=SimpleNamespace(method="cli", comment_on_pr=True),
        copy=copy,
    )

    assert seen == []
    assert result.review.status == "copy_failed"
    assert result.review.is_failure is True
    assert "clone failed" in result.review.detail
    worktree.remove_review_copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_switched_off_makes_no_copy(
    tmp_path: Path, monkeypatch,
) -> None:
    result, worktree, seen = await _dev_run_capturing(
        tmp_path, monkeypatch,
        code_review=SimpleNamespace(method="off"),
        copy=lambda _w: None,
    )

    assert seen == []
    worktree.make_review_copy.assert_not_awaited()
    assert result.review.status == "skipped"
    assert result.review.deliberate is True


@pytest.mark.asyncio
async def test_the_operators_own_filters_and_textconv_do_not_run_on_agent_files(
    tmp_path: Path, git_home: Path,
) -> None:
    """A committed `.gitattributes` can name a driver the operator's
    config defines. Control: it runs in the agent's worktree."""
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    wt, _sha, marks = _trapped_worktree(tmp_path, mgr)
    # Passes content through, so only the marker shows it ran.
    op = tmp_path / "op.sh"
    op.write_text(
        f'#!/bin/sh\ntouch "{marks}/fired-$1"\n'
        'if [ "$1" = textconv ]; then cat "$2"; else cat; fi\n'
    )
    op.chmod(0o755)
    with (git_home / ".gitconfig").open("a") as f:
        f.write(
            f'[filter "op"]\n\tsmudge = {op} smudge\n\tclean = {op} clean\n'
            f'[diff "op"]\n\ttextconv = {op} textconv\n'
        )
    (wt / ".gitattributes").write_text("*.txt filter=op diff=op\n")
    (wt / "t.txt").write_text("one\n")
    _git("add", ".gitattributes", "t.txt", cwd=wt)
    _git("commit", "-qm", "attrs", cwd=wt)
    (wt / "t.txt").write_text("two\n")
    _git("commit", "-qam", "two", cwd=wt)
    sha = _git("rev-parse", "HEAD", cwd=wt).strip()

    diff = [["diff", "HEAD~1", "HEAD", "--", "t.txt"], ["log", "-p", "-2"]]
    assert "fired-textconv" in _fired_after(diff, wt, marks), "control"

    for p in marks.iterdir():
        p.unlink()
    copy = await mgr.make_review_copy(REPO, sha)
    try:
        assert sorted(p.name for p in marks.iterdir()) == [], "checkout ran a driver"
        assert _fired_after(diff, copy, marks) == []
        # Still a readable text diff, not "Binary files differ".
        out = _git("diff", "HEAD~1", "HEAD", "--", "t.txt", cwd=copy)
        assert "+two" in out
    finally:
        await mgr.remove_review_copy(copy)


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_deleted_says_so(
    tmp_path: Path, git_home: Path,
) -> None:
    """A leaked copy must reach the caller's log line, not vanish into an
    ignored error."""
    if os.geteuid() == 0:
        pytest.skip("root can delete a read-only directory")
    mgr = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )
    _wt, sha, _marks = _trapped_worktree(tmp_path, mgr)
    copy = await mgr.make_review_copy(REPO, sha)
    copy.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            await mgr.remove_review_copy(copy)
    finally:
        copy.chmod(0o700)
        await mgr.remove_review_copy(copy)
