"""#263, the relay's own touches of agent-written trees outside the sandbox.

Measured before the fix, with plain git 2.43:

- ``git worktree add`` in the bare clone ran the operator's filter
  drivers (``git lfs install`` puts ``filter.lfs.*`` in the global
  config) on whatever tree it checked out. A reused branch carries the
  previous agent's ``.gitattributes``, and ``<bare>/info/attributes``,
  which the agent can write, applies to every checkout from the clone.
- ``symlink_context`` followed a link at ``<gitdir>/info/exclude`` (or at
  ``info``) and appended ``CLAUDE.md`` to whatever it pointed at.
- The review base was the bare clone's ``HEAD`` branch at the bare
  clone's ref, both of which the agent can move.

Every test that asserts a trap stays quiet first shows it fires without
the relay's guard, so quiet means guarded, not broken.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.code_review import (
    CodeReviewOutcome,
    ReviewStatus,
    format_review_comment,
)
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


def _mgr(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "bare",
    )


# --- 1. the relay's checkout runs no operator filter or hook -------------


def _setup_checkout_traps(tmp_path: Path, home: Path, mgr: WorktreeManager):
    """An origin with `main` and an agent branch whose committed
    `.gitattributes` selects driver `lfs` for `*.bin`; a bare clone of it
    at the manager's path; an operator global config defining that
    driver as a marker script; and a post-checkout hook in the clone.
    Returns (origin, bare, marks)."""
    marks = tmp_path / "marks"
    marks.mkdir()
    trap = tmp_path / "trap.sh"
    trap.write_text(f'#!/bin/sh\ntouch "{marks}/fired-$1"\ncat\n')
    trap.chmod(0o755)

    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", cwd=origin)
    (origin / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=origin)
    _git("commit", "-qm", "init", cwd=origin)
    _git("checkout", "-qb", "fix/issue-1", cwd=origin)
    (origin / ".gitattributes").write_text("*.bin filter=lfs\n")
    (origin / "x.bin").write_text("agent data\n")
    _git("add", ".gitattributes", "x.bin", cwd=origin)
    _git("commit", "-qm", "agent", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)

    bare = mgr._get_bare_repo_path(REPO)
    _git("clone", "-q", "--bare", str(origin), str(bare), cwd=tmp_path)
    _git("fetch", "-q", "origin", "refs/heads/*:refs/heads/*", cwd=bare)

    # As `git lfs install` writes it, after the clone so the setup
    # itself runs no driver.
    with (home / ".gitconfig").open("a") as f:
        f.write(
            f'[filter "lfs"]\n\tsmudge = {trap} smudge\n'
            f"\tclean = {trap} clean\n\trequired = true\n"
        )
    hook = bare / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\ntouch "{marks}/fired-hook"\n')
    hook.chmod(0o755)
    return origin, bare, marks


def _fired(marks: Path) -> list[str]:
    return sorted(p.name for p in marks.iterdir())


def _clear(marks: Path) -> None:
    for p in marks.iterdir():
        p.unlink()


@pytest.mark.asyncio
async def test_reusing_a_branch_runs_no_operator_filter_or_hook(
    tmp_path: Path, git_home: Path,
) -> None:
    mgr = _mgr(tmp_path)
    _origin, bare, marks = _setup_checkout_traps(tmp_path, git_home, mgr)

    # Control: the same checkout, as plain git runs it, fires both.
    _git("worktree", "add", "-q", str(tmp_path / "ctl"), "fix/issue-1", cwd=bare)
    assert _fired(marks) == ["fired-hook", "fired-smudge"], "control"
    _git("worktree", "remove", "--force", str(tmp_path / "ctl"), cwd=bare)
    _clear(marks)

    # The branch exists locally and on origin: the reuse path.
    wt, fresh = await mgr.create_worktree_with_new_branch(
        repo=REPO, session_id="s1", new_branch="fix/issue-1",
    )
    assert fresh is False
    assert _fired(marks) == []
    # The agent still gets the committed files.
    assert (wt / "x.bin").read_text() == "agent data\n"
    assert (wt / ".gitattributes").exists()


@pytest.mark.asyncio
async def test_a_planted_info_attributes_selects_no_driver_on_a_fresh_branch(
    tmp_path: Path, git_home: Path,
) -> None:
    """`<bare>/info/attributes` is agent-writable and applies to every
    checkout from the clone, including a new branch cut from `main`."""
    mgr = _mgr(tmp_path)
    _origin, bare, marks = _setup_checkout_traps(tmp_path, git_home, mgr)
    (bare / "info").mkdir(exist_ok=True)
    (bare / "info" / "attributes").write_text("* filter=lfs\n")

    _git("worktree", "add", "-q", "-b", "ctl", str(tmp_path / "ctl"), "main",
         cwd=bare)
    assert "fired-smudge" in _fired(marks), "control"
    _clear(marks)

    wt, fresh = await mgr.create_worktree_with_new_branch(
        repo=REPO, session_id="s2", new_branch="fix/issue-2",
    )
    assert fresh is True
    assert _fired(marks) == []
    assert (wt / "a.txt").read_text() == "a\n"


@pytest.mark.asyncio
async def test_create_worktree_runs_no_operator_filter_or_hook(
    tmp_path: Path, git_home: Path,
) -> None:
    """The plain `create_worktree` the other pipelines use."""
    mgr = _mgr(tmp_path)
    _origin, bare, marks = _setup_checkout_traps(tmp_path, git_home, mgr)
    _git("worktree", "add", "-q", str(tmp_path / "ctl"), "fix/issue-1", cwd=bare)
    assert _fired(marks) == ["fired-hook", "fired-smudge"], "control"
    _git("worktree", "remove", "--force", str(tmp_path / "ctl"), cwd=bare)
    _clear(marks)

    wt = await mgr.create_worktree(REPO, "s3", branch="fix/issue-1")
    assert _fired(marks) == []
    assert (wt / "x.bin").read_text() == "agent data\n"


# --- 2. the exclude write follows no link --------------------------------


def _linked_worktree(tmp_path: Path) -> tuple[WorktreeManager, Path, Path]:
    mgr = _mgr(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    (src / "a").write_text("a\n")
    _git("add", "a", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    bare = mgr._get_bare_repo_path(REPO)
    _git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "run", str(wt), cwd=bare)
    gitdir = mgr._get_gitdir(wt)
    assert gitdir.parent.name == "worktrees", gitdir
    return mgr, wt, gitdir


def _context(tmp_path: Path) -> Path:
    ctx = tmp_path / "ctx" / "CLAUDE.md"
    ctx.parent.mkdir()
    ctx.write_text("context\n")
    return ctx


def test_a_plain_exclude_file_gets_the_line(tmp_path: Path, git_home) -> None:
    """Control for the two below: the write itself still happens."""
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    (gitdir / "info").mkdir()
    (gitdir / "info" / "exclude").write_text("# x\n")
    mgr.symlink_context(wt, _context(tmp_path))
    assert (gitdir / "info" / "exclude").read_text() == "# x\nCLAUDE.md\n"
    assert (wt / "CLAUDE.md").is_symlink()


def test_a_link_at_exclude_is_not_followed(tmp_path: Path, git_home) -> None:
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("operator file\n")
    (gitdir / "info").mkdir()
    (gitdir / "info" / "exclude").symlink_to(victim)

    mgr.symlink_context(wt, _context(tmp_path))

    assert victim.read_text() == "operator file\n"
    # The context link is still placed; only the exclude line is refused.
    assert (wt / "CLAUDE.md").is_symlink()


def test_a_link_at_info_is_not_followed(tmp_path: Path, git_home) -> None:
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "exclude").write_text("other\n")
    (gitdir / "info").symlink_to(elsewhere)

    mgr.symlink_context(wt, _context(tmp_path))

    assert (elsewhere / "exclude").read_text() == "other\n"
    assert (wt / "CLAUDE.md").is_symlink()


def test_no_exclude_file_creates_none(tmp_path: Path, git_home) -> None:
    """Behaviour kept from before, not a guard: no plant is involved."""
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    mgr.symlink_context(wt, _context(tmp_path))
    assert not (gitdir / "info").exists()


# --- 3. the review base comes from GitHub, not the bare clone ------------


def _agent_steered_clone(tmp_path: Path, mgr: WorktreeManager):
    """A bare clone at the manager's path, where the agent's branch holds
    an `evil.sh` commit then a `benign.txt` commit, and the agent has
    moved the clone's `main` onto its own evil commit and `HEAD` onto its
    own branch. Returns (bare, head sha, GitHub's main sha)."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    (src / "a").write_text("a\n")
    _git("add", "a", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    real_main = _git("rev-parse", "HEAD", cwd=src).strip()
    bare = mgr._get_bare_repo_path(REPO)
    _git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    wt = tmp_path / "agent-wt"
    _git("worktree", "add", "-q", "-b", "fix/x", str(wt), cwd=bare)
    (wt / "evil.sh").write_text("bad\n")
    _git("add", "evil.sh", cwd=wt)
    _git("commit", "-qm", "evil", cwd=wt)
    evil = _git("rev-parse", "HEAD", cwd=wt).strip()
    (wt / "benign.txt").write_text("ok\n")
    _git("add", "benign.txt", cwd=wt)
    _git("commit", "-qm", "benign", cwd=wt)
    head = _git("rev-parse", "HEAD", cwd=wt).strip()
    # What the agent can do from inside the sandbox: both are files in
    # the shared clone.
    _git("update-ref", "refs/heads/main", evil, cwd=wt)
    (bare / "HEAD").write_text("ref: refs/heads/fix/x\n")
    return bare, head, real_main


def _changed(copy: Path, base: str) -> list[str]:
    return sorted(
        _git("diff", "--name-only", f"{base}...HEAD", cwd=copy).split()
    )


@pytest.mark.asyncio
async def test_the_review_base_is_pinned_to_githubs_sha(
    tmp_path: Path, git_home: Path,
) -> None:
    mgr = _mgr(tmp_path)
    bare, head, real_main = _agent_steered_clone(tmp_path, mgr)

    # Control: what the reviewer was given before. The clone's HEAD named
    # the agent's own branch as the base (an empty diff), and the clone's
    # `main` hides evil.sh.
    ctl = tmp_path / "ctl"
    _git("clone", "-q", "--shared", "--no-checkout", str(bare), str(ctl),
         cwd=tmp_path)
    _git("checkout", "-q", "--detach", head, cwd=ctl)
    assert _git("symbolic-ref", "HEAD", cwd=bare).strip() == "refs/heads/fix/x"
    assert _changed(ctl, "fix/x") == [], "control"
    assert _changed(ctl, "origin/main") == ["benign.txt"], "control"

    copy = await mgr.make_review_copy(
        REPO, head, base_branch="main", base_sha=real_main,
    )
    try:
        assert _git("rev-parse", "HEAD", cwd=copy).strip() == head
        for ref in ("main", "origin/main", "origin/HEAD", "main@{upstream}"):
            assert _git("rev-parse", ref, cwd=copy).strip() == real_main, ref
        assert _changed(copy, "main") == ["benign.txt", "evil.sh"]
    finally:
        await mgr.remove_review_copy(copy)


@pytest.mark.asyncio
async def test_a_base_the_clone_lacks_is_fetched_into_the_copy_only(
    tmp_path: Path, git_home: Path, monkeypatch,
) -> None:
    mgr = _mgr(tmp_path)
    bare, head, _ = _agent_steered_clone(tmp_path, mgr)
    src = tmp_path / "src"
    (src / "later").write_text("later\n")
    _git("add", "later", cwd=src)
    _git("commit", "-qm", "later", cwd=src)
    newer = _git("rev-parse", "HEAD", cwd=src).strip()
    # Control: the clone really does not have it.
    assert subprocess.run(
        ["git", "cat-file", "-e", newer], cwd=bare, capture_output=True,
    ).returncode != 0
    monkeypatch.setattr(mgr, "_clone_url", lambda repo: str(src))

    copy = await mgr.make_review_copy(
        REPO, head, base_branch="main", base_sha=newer,
    )
    try:
        assert _git("rev-parse", "main", cwd=copy).strip() == newer
    finally:
        await mgr.remove_review_copy(copy)
    assert subprocess.run(
        ["git", "cat-file", "-e", newer], cwd=bare, capture_output=True,
    ).returncode != 0


@pytest.mark.asyncio
async def test_an_unusable_base_is_refused(
    tmp_path: Path, git_home: Path, monkeypatch,
) -> None:
    mgr = _mgr(tmp_path)
    _bare, head, real_main = _agent_steered_clone(tmp_path, mgr)
    monkeypatch.setattr(mgr, "_clone_url", lambda repo: str(tmp_path / "src"))
    # Control: a good base is accepted.
    ok = await mgr.make_review_copy(
        REPO, head, base_branch="main", base_sha=real_main,
    )
    await mgr.remove_review_copy(ok)
    for branch, sha in (
        ("main", "main"),
        ("--upload-pack=x", real_main),
        ("a..b", real_main),
        ("main", "f" * 40),  # not in the clone, not on the remote
    ):
        with pytest.raises(WorktreeError):
            await mgr.make_review_copy(
                REPO, head, base_branch=branch, base_sha=sha,
            )
    assert list((tmp_path / "review").iterdir()) == []


def test_the_comment_names_both_commits() -> None:
    outcome = CodeReviewOutcome(status=ReviewStatus.REVIEWED, output="fine")
    body = format_review_comment(
        outcome, head_sha="a" * 40, base_branch="main", base_sha="b" * 40,
    )
    assert f"`{'a' * 40}` against `main` at `{'b' * 40}`" in body
    assert "against" not in format_review_comment(outcome)


async def _dev_run(tmp_path: Path, monkeypatch, *, ref_sha: str):
    from ctrlrelay.core.pr_verifier import VerificationResult
    from ctrlrelay.pipelines import dev as dev_mod

    seen: list[dict] = []

    async def fake_review(**kw):
        seen.append(kw)
        return CodeReviewOutcome(
            status=ReviewStatus.REVIEWED, output="ok",
            evidence=("git diff main...HEAD",),
        )

    monkeypatch.setattr(dev_mod, "run_code_review", fake_review)
    worktree = AsyncMock()
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    # What the agent-written bare clone says: its own branch.
    worktree.get_default_branch.return_value = "fix/issue-1"
    worktree.push_run_branch.return_value = "c" * 40
    worktree.make_review_copy.return_value = tmp_path / "ctrlrelay-review-x" / "repo"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = _done_state(7)
    github = AsyncMock()
    fake_relay_publish(github, pr_number=7, repo=REPO, head_sha="c" * 40)
    github.get_ref_sha = AsyncMock(return_value=ref_sha)
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
        code_review=SimpleNamespace(method="cli", comment_on_pr=True),
    )
    return result, worktree, github, seen


@pytest.mark.asyncio
async def test_the_dev_run_reviews_against_githubs_base(
    tmp_path: Path, monkeypatch,
) -> None:
    result, worktree, github, seen = await _dev_run(
        tmp_path, monkeypatch, ref_sha="b" * 40 + "\n",
    )
    assert result.success, result
    github.get_ref_sha.assert_awaited_once_with(REPO, "refs/heads/main")
    worktree.make_review_copy.assert_awaited_once_with(
        REPO, "c" * 40, base_branch="main", base_sha="b" * 40,
    )
    assert [kw["base_branch"] for kw in seen] == ["main"]
    body = github.comment_on_pr.await_args.args[2]
    assert f"`{'c' * 40}` against `main` at `{'b' * 40}`" in body


@pytest.mark.asyncio
async def test_a_base_github_cannot_name_is_not_reviewed(
    tmp_path: Path, monkeypatch,
) -> None:
    result, worktree, _github, seen = await _dev_run(
        tmp_path, monkeypatch, ref_sha="",
    )
    assert seen == []
    worktree.make_review_copy.assert_not_awaited()
    assert result.review.status == "base_unknown"


def _pre_fix_exclude_write(worktree_path: Path) -> None:
    """The exclude write as it was before #263, for the controls below."""
    dot_git = worktree_path / ".git"
    gitdir = Path(dot_git.read_text().strip()[len("gitdir: "):])
    exclude_file = gitdir / "info" / "exclude"
    if exclude_file.exists():
        content = exclude_file.read_text()
        if "CLAUDE.md" not in content:
            exclude_file.write_text(content.rstrip() + "\nCLAUDE.md\n")


def _decoy(tmp_path: Path, name: str) -> Path:
    decoy = tmp_path / name
    (decoy / "info").mkdir(parents=True)
    (decoy / "info" / "exclude").write_text("decoy\n")
    return decoy


def test_a_rewritten_dot_git_pointer_is_not_followed(
    tmp_path: Path, git_home,
) -> None:
    """Without the sandbox the agent can rewrite the worktree's `.git`
    file to name any directory."""
    mgr, wt, _gitdir = _linked_worktree(tmp_path)
    ctl = _decoy(tmp_path, "control-decoy")
    (wt / ".git").write_text(f"gitdir: {ctl}\n")
    _pre_fix_exclude_write(wt)
    assert (ctl / "info" / "exclude").read_text() == "decoy\nCLAUDE.md\n", (
        "control"
    )

    decoy = _decoy(tmp_path, "decoy")
    (wt / ".git").write_text(f"gitdir: {decoy}\n")
    mgr.symlink_context(wt, _context(tmp_path))
    assert (decoy / "info" / "exclude").read_text() == "decoy\n"
    assert (wt / "CLAUDE.md").is_symlink()


def test_a_pointer_to_another_sessions_git_dir_is_not_followed(
    tmp_path: Path, git_home,
) -> None:
    """Inside the clone's `worktrees/`, but not this worktree's: its own
    `gitdir` file points at the other worktree."""
    mgr, wt, _gitdir = _linked_worktree(tmp_path)
    bare = mgr._get_bare_repo_path(REPO)
    other = tmp_path / "other"
    _git("worktree", "add", "-q", "-b", "other", str(other), cwd=bare)
    other_dir = mgr._get_gitdir(other)
    (other_dir / "info").mkdir()
    (other_dir / "info" / "exclude").write_text("other\n")
    (wt / ".git").write_text(f"gitdir: {other_dir}\n")
    _pre_fix_exclude_write(wt)
    assert (other_dir / "info" / "exclude").read_text() == "other\nCLAUDE.md\n", (
        "control"
    )
    (other_dir / "info" / "exclude").write_text("other\n")
    # This worktree's own directory, found from the path the relay made.
    (_gitdir / "info").mkdir()
    (_gitdir / "info" / "exclude").write_text("own\n")

    mgr.symlink_context(wt, _context(tmp_path))
    assert (other_dir / "info" / "exclude").read_text() == "other\n"
    assert (_gitdir / "info" / "exclude").read_text() == "own\nCLAUDE.md\n"


def test_a_link_at_the_worktree_git_dir_is_not_followed(
    tmp_path: Path, git_home,
) -> None:
    """The name is right but the directory at it is a link elsewhere."""
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    moved = tmp_path / "moved-admin"
    gitdir.rename(moved)
    gitdir.symlink_to(moved)
    (moved / "info").mkdir()
    (moved / "info" / "exclude").write_text("moved\n")
    _pre_fix_exclude_write(wt)
    assert (moved / "info" / "exclude").read_text() == "moved\nCLAUDE.md\n", (
        "control"
    )
    (moved / "info" / "exclude").write_text("moved\n")

    mgr.symlink_context(wt, _context(tmp_path))
    assert (moved / "info" / "exclude").read_text() == "moved\n"


def test_a_hard_linked_exclude_is_not_appended_to(
    tmp_path: Path, git_home,
) -> None:
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("operator file\n")
    (gitdir / "info").mkdir()
    os.link(victim, gitdir / "info" / "exclude")
    _pre_fix_exclude_write(wt)
    assert victim.read_text() == "operator file\nCLAUDE.md\n", "control"
    victim.write_text("operator file\n")

    mgr.symlink_context(wt, _context(tmp_path))
    assert victim.read_text() == "operator file\n"
    assert (wt / "CLAUDE.md").is_symlink()


def test_only_the_back_pointer_stops_a_same_named_git_dir(
    tmp_path: Path, git_home,
) -> None:
    """A directory with this worktree's name in another clone's
    `worktrees/`: a real directory, no links, a readable pointer. Only
    the pointer's target differs."""
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    shutil.rmtree(gitdir)  # this worktree's own directory is gone
    other_bare = tmp_path / "bare" / "o-other.git"
    _git("clone", "-q", "--bare", str(tmp_path / "src"), str(other_bare),
         cwd=tmp_path)
    twin = tmp_path / "elsewhere" / wt.name
    twin.parent.mkdir()
    _git("worktree", "add", "-q", "-b", "twin", str(twin), cwd=other_bare)
    twin_dir = other_bare / "worktrees" / wt.name
    assert twin_dir.is_dir() and not twin_dir.is_symlink()
    (twin_dir / "info").mkdir()
    (twin_dir / "info" / "exclude").write_text("twin\n")
    # Control: reached by name, the plant is written.
    (wt / ".git").write_text(f"gitdir: {twin_dir}\n")
    _pre_fix_exclude_write(wt)
    assert (twin_dir / "info" / "exclude").read_text() == "twin\nCLAUDE.md\n", (
        "control"
    )
    (twin_dir / "info" / "exclude").write_text("twin\n")

    mgr.symlink_context(wt, _context(tmp_path))
    assert (twin_dir / "info" / "exclude").read_text() == "twin\n"


def test_a_missing_clone_dir_is_logged_not_raised(
    tmp_path: Path, git_home, caplog,
) -> None:
    mgr, wt, _gitdir = _linked_worktree(tmp_path)
    mgr.bare_repos_dir = tmp_path / "no-such-dir"
    # Control: the open really fails.
    with pytest.raises(OSError):
        os.open(mgr.bare_repos_dir, os.O_RDONLY | os.O_DIRECTORY)
    caplog.set_level("INFO")
    mgr.symlink_context(wt, _context(tmp_path))
    assert (wt / "CLAUDE.md").is_symlink()
    assert "worktree.context.exclude_refused" in caplog.text


def test_a_back_pointer_that_is_not_utf8_is_logged_not_raised(
    tmp_path: Path, git_home, caplog,
) -> None:
    mgr, wt, gitdir = _linked_worktree(tmp_path)
    (gitdir / "gitdir").write_bytes(b"\xff\xfe/not utf-8\n")
    # Control: strict decoding of it fails.
    with pytest.raises(UnicodeDecodeError):
        (gitdir / "gitdir").read_bytes().decode("utf-8")
    caplog.set_level("INFO")
    mgr.symlink_context(wt, _context(tmp_path))
    assert (wt / "CLAUDE.md").is_symlink()
    assert "worktree.context.exclude_refused" in caplog.text
