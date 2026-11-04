"""Runs whose process died without settling them (#312)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.claims import release_marker
from ctrlrelay.core.orphans import (
    owner_is_alive,
    reap_orphaned_sessions,
    worktree_in_use,
)
from ctrlrelay.core.poller import IssuePoller
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager
from ctrlrelay.web.status import build_status

REPO = "owner/repo"
BRANCH = "fix/issue-7"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout


@pytest.fixture
def node(tmp_path: Path):
    """A node's state as a dead dev run leaves it: a real bare clone with
    the run's worktree on the issue branch holding unsaved work, a
    ``running`` row naming a dead process, the issue seen, a repo lock."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    (src / "README").write_text("hi\n")
    _git("add", "README", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)

    bare_dir = tmp_path / "repos"
    wt_dir = tmp_path / "worktrees"
    bare_dir.mkdir()
    wt_dir.mkdir()
    bare = bare_dir / "owner-repo.git"
    _git("clone", "-q", "--bare", str(src), str(bare))
    wt = wt_dir / "owner-repo-dead1"
    _git("worktree", "add", "-q", "-b", BRANCH, str(wt), "main", cwd=bare)
    (wt / "half-done.py").write_text("print('the agent got this far')\n")

    db = StateDB(tmp_path / "state.db")
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, worktree_path, status,
           started_at, issue_number, owner_pid, claim_id)
           VALUES ('dead1', 'dev', ?, ?, 'running', ?, 7, 999999, 'c-123')""",
        (REPO, str(wt), int(time.time()) - 3600),
    )
    db.commit()
    assert db.acquire_lock(REPO, "dead1")

    poller = IssuePoller(
        github=MagicMock(), username="u", repos=[REPO],
        state_file=tmp_path / "poller_state.json",
    )
    poller.mark_seen(REPO, 7)

    github = MagicMock()
    github.list_prs = AsyncMock(return_value=[])
    github.comment_on_issue = AsyncMock()

    worktree = WorktreeManager(worktrees_dir=wt_dir, bare_repos_dir=bare_dir)
    yield {
        "tmp": tmp_path, "db": db, "poller": poller, "github": github,
        "worktree": worktree, "bare": bare, "wt": wt,
    }
    db.close()


async def _reap(n: dict, is_alive=lambda pid: False):
    return await reap_orphaned_sessions(
        n["db"], poller=n["poller"], github=n["github"],
        worktree=n["worktree"], is_alive=is_alive,
    )


def _row(n: dict, sid: str = "dead1") -> dict:
    return dict(n["db"].execute(
        "SELECT * FROM sessions WHERE id = ?", (sid,)
    ).fetchone())


def _comment(n: dict) -> str:
    n["github"].comment_on_issue.assert_awaited_once()
    return n["github"].comment_on_issue.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_a_dead_run_is_settled_and_its_issue_handed_back(node) -> None:
    settled = await _reap(node)

    row = _row(node)
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    assert "stopped before the run ended" in row["summary"]
    assert node["db"].get_lock_holder(REPO) is None

    # Handed back on disk, not only in memory: the next poller process
    # reads the file.
    on_disk = json.loads((node["tmp"] / "poller_state.json").read_text())
    assert 7 not in on_disk["seen_issues"].get(REPO, [])

    body = _comment(node)
    assert release_marker("c-123") in body
    assert str(node["wt"]) in body
    assert "picks this issue up again" in body

    assert settled == [{
        "session_id": "dead1", "repo": REPO, "issue_number": 7,
        "worktree_path": str(node["wt"]), "branch_freed": BRANCH,
        "pr_number": None, "handed_back": True,
    }]


@pytest.mark.asyncio
async def test_the_worktree_is_detached_and_its_unsaved_work_kept(
    node,
) -> None:
    head_before = _git("rev-parse", "HEAD", cwd=node["wt"])
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=node["wt"]) == (
        BRANCH + "\n"
    )
    await _reap(node)
    # Detached, at the same commit, with the agent's file untouched.
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=node["wt"]) == "HEAD\n"
    assert _git("rev-parse", "HEAD", cwd=node["wt"]) == head_before
    assert (node["wt"] / "half-done.py").read_text() == (
        "print('the agent got this far')\n"
    )
    assert "?? half-done.py" in _git("status", "--porcelain", cwd=node["wt"])


@pytest.mark.asyncio
async def test_the_next_run_can_check_the_branch_out(node, tmp_path) -> None:
    """What the retry does: a new worktree on the issue's branch. While
    the dead run's worktree holds the branch, git refuses it."""
    second = tmp_path / "worktrees" / "owner-repo-next"
    before = subprocess.run(
        ["git", "worktree", "add", "-q", str(second), BRANCH],
        cwd=node["bare"], capture_output=True, text=True,
    )
    assert before.returncode != 0
    assert "already" in before.stderr

    await _reap(node)
    _git("worktree", "add", "-q", str(second), BRANCH, cwd=node["bare"])
    assert (second / "README").exists()


@pytest.mark.asyncio
async def test_the_status_page_stops_listing_it_as_running(node) -> None:
    def running() -> list:
        out = build_status(
            db_path=node["tmp"] / "state.db", waiting={"issues": []},
            server=lambda: {}, version="9", host="h", home=node["tmp"],
        )
        return [c.get("session_id") or c.get("id") for c in out["running"]]

    assert "dead1" in running()
    await _reap(node)
    assert "dead1" not in running()


@pytest.mark.asyncio
async def test_a_live_owner_is_left_alone(node) -> None:
    settled = await _reap(node, is_alive=lambda pid: True)
    assert settled == []
    assert _row(node)["status"] == "running"
    assert node["db"].get_lock_holder(REPO) == "dead1"
    assert 7 in node["poller"].seen_issues[REPO]
    node["github"].comment_on_issue.assert_not_awaited()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=node["wt"]) == (
        BRANCH + "\n"
    )


@pytest.mark.asyncio
async def test_a_row_from_before_the_owner_was_recorded_is_left_alone(
    node,
) -> None:
    node["db"].execute("UPDATE sessions SET owner_pid = NULL")
    node["db"].commit()
    assert await _reap(node) == []
    assert _row(node)["status"] == "running"
    node["github"].comment_on_issue.assert_not_awaited()
    assert node["db"].get_lock_holder(REPO) == "dead1"
    assert 7 in node["poller"].seen_issues[REPO]
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=node["wt"]) == (
        BRANCH + "\n"
    )


@pytest.mark.asyncio
async def test_another_sessions_lock_on_the_same_repo_is_not_taken(
    node,
) -> None:
    """The lock is dropped by session id. A live run that took the repo
    lock after the dead one's was cleared must keep it."""
    node["db"].release_lock(REPO, "dead1")
    assert node["db"].acquire_lock(REPO, "live2")
    await _reap(node)
    assert node["db"].get_lock_holder(REPO) == "live2"


@pytest.mark.asyncio
async def test_an_open_pull_request_keeps_the_issue_seen(node) -> None:
    node["github"].list_prs = AsyncMock(return_value=[{
        "number": 41,
        "headRepositoryOwner": {"login": "owner"},
        "headRepository": {"name": "repo"},
    }])
    settled = await _reap(node)
    assert settled[0]["pr_number"] == 41
    assert settled[0]["handed_back"] is False
    assert 7 in node["poller"].seen_issues[REPO]
    assert "#41" in _comment(node)
    assert node["github"].list_prs.await_args.kwargs["head"] == BRANCH


@pytest.mark.asyncio
async def test_a_secops_row_is_failed_without_touching_any_issue(
    node,
) -> None:
    # The issue number is left on the row: it is the pipeline, not a
    # missing number, that keeps the issue out of it.
    node["db"].execute(
        "UPDATE sessions SET pipeline = 'secops', claim_id = NULL"
    )
    node["db"].commit()
    await _reap(node)
    assert _row(node)["status"] == "failed"
    assert 7 in node["poller"].seen_issues[REPO]
    node["github"].comment_on_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_comment_does_not_undo_the_cleanup(node) -> None:
    node["github"].comment_on_issue = AsyncMock(side_effect=RuntimeError("gh"))
    await _reap(node)
    assert _row(node)["status"] == "failed"
    assert 7 not in node["poller"].seen_issues.get(REPO, set())


@pytest.mark.asyncio
async def test_a_missing_worktree_still_settles_the_run(node) -> None:
    _git("worktree", "remove", "--force", str(node["wt"]), cwd=node["bare"])
    settled = await _reap(node)
    assert _row(node)["status"] == "failed"
    assert settled[0]["branch_freed"] is None
    assert 7 not in node["poller"].seen_issues.get(REPO, set())
    body = _comment(node)
    assert "was not found on that machine" in body
    assert "deleted nothing" not in body
    assert "still on that machine" not in body
    # The branch is unknown, so whether a pull request exists is too, and
    # the comment does not claim there is none.
    assert "no pull request" not in body
    node["github"].list_prs.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_pull_request_lookup_hands_back_without_a_claim(
    node,
) -> None:
    node["github"].list_prs = AsyncMock(side_effect=RuntimeError("gh down"))
    settled = await _reap(node)
    assert settled[0]["handed_back"] is True
    assert "no pull request" not in _comment(node)


@pytest.mark.asyncio
async def test_no_open_pull_request_is_not_reported_as_none_opened(
    node,
) -> None:
    """Only open pull requests are looked for; one may have been opened
    and closed, so the comment does not say none was."""
    await _reap(node)
    assert "no pull request" not in _comment(node)


@pytest.mark.asyncio
async def test_a_row_this_process_wrote_is_left_alone(node) -> None:
    """A scheduled sweep can start before the reaper runs. Its row names
    this process, whose pid the default check would call dead."""
    from ctrlrelay.core.claims import PROCESS_RUN

    node["db"].execute(
        "UPDATE sessions SET owner_pid = ?, owner_run = ?",
        (os.getpid(), PROCESS_RUN),
    )
    node["db"].commit()
    assert await reap_orphaned_sessions(
        node["db"], poller=node["poller"], github=node["github"],
        worktree=node["worktree"],
    ) == []
    assert _row(node)["status"] == "running"


@pytest.mark.asyncio
async def test_this_pid_from_an_earlier_process_is_reaped(node) -> None:
    """The same pid with another run id: an earlier process whose pid was
    handed on to this one, as pid 1 is in a container."""
    node["db"].execute(
        "UPDATE sessions SET owner_pid = ?, owner_run = 'earlier1'",
        (os.getpid(),),
    )
    node["db"].commit()
    await reap_orphaned_sessions(
        node["db"], poller=node["poller"], github=node["github"],
        worktree=node["worktree"],
    )
    assert _row(node)["status"] == "failed"


@pytest.mark.asyncio
async def test_a_real_live_relay_process_is_left_alone(node) -> None:
    proc = TestOwnerIsAlive._started("ctrlrelay", "run", "dev")
    try:
        node["db"].execute(
            "UPDATE sessions SET owner_pid = ?, owner_run = 'other'",
            (proc.pid,),
        )
        node["db"].commit()
        assert await reap_orphaned_sessions(
            node["db"], poller=node["poller"], github=node["github"],
            worktree=node["worktree"],
        ) == []
    finally:
        proc.kill()
        proc.wait()
    assert _row(node)["status"] == "running"
    assert BRANCH in _git("worktree", "list", "--porcelain", cwd=node["bare"])


@pytest.mark.asyncio
async def test_a_linked_head_file_is_not_written_through(node, tmp_path) -> None:
    """An agent with the sandbox off can write the bare clone. A HEAD
    file swapped for a link must not carry the write somewhere else."""
    admin = node["worktree"]._resolve_admin_dir(node["bare"], str(node["wt"]))
    outside = tmp_path / "outside"
    outside.write_text(f"ref: refs/heads/{BRANCH}\n")
    (admin / "HEAD").unlink()
    (admin / "HEAD").symlink_to(outside)
    settled = await _reap(node)
    assert settled[0]["branch_freed"] is None
    assert outside.read_text() == f"ref: refs/heads/{BRANCH}\n"
    assert (admin / "HEAD").is_symlink()


class TestOwnerIsAlive:
    def test_a_process_that_exited_is_dead(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert owner_is_alive(proc.pid) is False

    def test_this_process_is_never_the_owner(self) -> None:
        assert owner_is_alive(os.getpid()) is False

    @staticmethod
    def _started(*extra: str) -> subprocess.Popen:
        """A child that has exec'd, so its command line is its own and not
        the forking test runner's for a moment."""
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import time; print('up', flush=True); time.sleep(30)", *extra],
            stdout=subprocess.PIPE, text=True,
        )
        assert proc.stdout is not None and proc.stdout.readline() == "up\n"
        return proc

    def test_a_reused_pid_running_something_else_is_dead(self) -> None:
        proc = self._started("--not-it")
        try:
            assert owner_is_alive(proc.pid) is False
        finally:
            proc.kill()
            proc.wait()

    def test_a_live_console_script_is_alive(self, tmp_path: Path) -> None:
        """The shape a real install runs: the interpreter, then the
        ``ctrlrelay`` entry script by path, then the subcommand."""
        script = tmp_path / "bin" / "ctrlrelay"
        script.parent.mkdir()
        script.write_text(
            "import time\nprint('up', flush=True)\ntime.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, str(script), "poller", "start"],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert proc.stdout is not None and proc.stdout.readline() == "up\n"
            assert owner_is_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()

    def test_a_live_module_run_is_alive(self) -> None:
        proc = self._started("-m", "ctrlrelay.cli", "poller", "start")
        try:
            assert owner_is_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()

    def test_a_live_ctrlrelay_process_is_alive(self) -> None:
        proc = self._started("ctrlrelay", "poller", "start")
        try:
            assert owner_is_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()


def test_an_old_database_gains_the_owner_columns(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (id TEXT PRIMARY KEY, pipeline TEXT NOT NULL,
           repo TEXT NOT NULL, issue_number INTEGER, worktree_path TEXT,
           status TEXT NOT NULL, blocked_question TEXT,
           started_at INTEGER NOT NULL, ended_at INTEGER,
           claude_exit_code INTEGER, summary TEXT)"""
    )
    conn.commit()
    conn.close()
    db = StateDB(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    db.close()
    assert {"owner_pid", "owner_run", "claim_id"} <= cols


@pytest.mark.asyncio
async def test_a_row_that_settles_while_being_checked_is_not_touched(
    node,
) -> None:
    """The run finished between the list and the update: its own final
    status stands, and nothing of the issue's is changed."""
    def finishes_now(pid: int) -> bool:
        node["db"].execute(
            "UPDATE sessions SET status = 'done', summary = 'ok' "
            "WHERE id = 'dead1'"
        )
        node["db"].commit()
        return False

    assert await _reap(node, is_alive=finishes_now) == []
    row = _row(node)
    assert (row["status"], row["summary"]) == ("done", "ok")
    assert node["db"].get_lock_holder(REPO) == "dead1"
    assert 7 in node["poller"].seen_issues[REPO]
    node["github"].comment_on_issue.assert_not_awaited()
    # And the worktree still holds its branch: nothing was detached.
    assert BRANCH in _git("worktree", "list", "--porcelain", cwd=node["bare"])


@pytest.mark.asyncio
async def test_a_fifo_head_does_not_hang_startup(node) -> None:
    admin = node["worktree"]._resolve_admin_dir(node["bare"], str(node["wt"]))
    (admin / "HEAD").unlink()
    os.mkfifo(admin / "HEAD")
    # On its own daemon thread, so a regression fails this test in ten
    # seconds instead of hanging the whole run on the blocked open.
    import threading
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(
            node["worktree"].detach_orphaned_worktree(REPO, node["wt"])
        ),
        daemon=True,
    )
    t.start()
    t.join(10)
    assert not t.is_alive(), "detach blocked on a FIFO"
    assert out == [None]


@pytest.mark.asyncio
async def test_an_agent_still_working_in_the_worktree_is_left_alone(
    node,
) -> None:
    """The relay was killed on its own and its agent lives on."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import time; print('up', flush=True); time.sleep(30)"],
        cwd=node["wt"], stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline() == "up\n"
        assert worktree_in_use(str(node["wt"])) is True
        assert await _reap(node) == []
    finally:
        proc.kill()
        proc.wait()
    assert _row(node)["status"] == "running"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=node["wt"]) == (
        BRANCH + "\n"
    )
    assert worktree_in_use(str(node["wt"])) is False


@pytest.mark.asyncio
async def test_a_fork_pull_request_on_the_same_branch_name_does_not_count(
    node,
) -> None:
    node["github"].list_prs = AsyncMock(return_value=[{
        "number": 99,
        "headRepositoryOwner": {"login": "someone-else"},
        "headRepository": {"name": "repo"},
    }])
    settled = await _reap(node)
    assert settled[0]["pr_number"] is None
    assert settled[0]["handed_back"] is True


def test_an_attached_module_flag_is_a_relay(tmp_path: Path) -> None:
    proc = TestOwnerIsAlive._started("-mctrlrelay.cli", "poller", "start")
    try:
        assert owner_is_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_a_worktree_that_cannot_be_checked_is_left_alone(node) -> None:
    """Could not tell whether an agent is still there: errs towards alive."""
    assert await reap_orphaned_sessions(
        node["db"], poller=node["poller"], github=node["github"],
        worktree=node["worktree"], is_alive=lambda pid: False,
        in_use=lambda path: None,
    ) == []
    assert _row(node)["status"] == "running"


def test_a_scan_that_reads_nothing_cannot_tell(monkeypatch, tmp_path) -> None:
    """This process always has a working directory, so an empty scan is
    a scan that did not work, not proof that nothing is there."""
    import ctrlrelay.core.orphans as orphans

    def unreadable(path):
        raise PermissionError(path)

    monkeypatch.setattr(orphans.os, "readlink", unreadable)
    assert worktree_in_use(str(tmp_path)) is None
