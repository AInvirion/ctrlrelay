"""A run that times out keeps its work on a wip branch (#311)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests._publish_fakes import fake_relay_publish

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
}
WIP = "wip/issue-7-ab12cd34"
# Root reads a mode-0 file, so these cannot make one unreadable.
_NOT_ROOT = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read any file",
)


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _drop_flag(flags: tuple[str, ...], value: str) -> tuple[str, ...]:
    out: list[str] = []
    i = 0
    while i < len(flags):
        if flags[i] == "-c" and flags[i + 1] == value:
            i += 2
            continue
        out.append(flags[i])
        i += 1
    return tuple(out)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    """An origin with `main`, the relay's bare clone of it, and a run's
    worktree on `fix/issue-7`. The relay's push reaches the local origin:
    the only relaxations are the URL mapping in the relay-written config
    and `protocol.file.allow`, both test-side."""
    import ctrlrelay.core.worktree as wt
    from ctrlrelay.core.worktree import WorktreeManager

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    origin = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(seed), str(origin))
    bare = tmp_path / "repos" / "owner-repo.git"
    bare.parent.mkdir()
    _git("clone", "-q", "--bare", str(origin), str(bare))
    work = tmp_path / "worktrees" / "owner-repo-dev-7-ab12cd34"
    _git("worktree", "add", "-q", "-b", "fix/issue-7", str(work), "main", cwd=bare)

    monkeypatch.setattr(
        wt, "_relay_push_gitconfig",
        lambda: (
            f'[url "{origin}"]\n'
            "\tinsteadOf = https://github.com/owner/repo.git\n"
        ),
    )
    monkeypatch.setattr(
        wt, "_RELAY_GIT_FLAGS",
        _drop_flag(wt._RELAY_GIT_FLAGS, "protocol.file.allow=never"),
    )
    manager = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "repos",
    )
    return {"origin": origin, "bare": bare, "work": work, "manager": manager,
            "main": _git("rev-parse", "main", cwd=origin), "tmp": tmp_path}


async def _save(s: dict, **kw):
    args = {
        "wip_branch": WIP, "message": "wip: timed out", "default_branch": "main",
        "relay_pushed_shas": frozenset(), "session_id": "dev-7-ab12cd34",
    }
    args.update(kw)
    return await s["manager"].save_unfinished_work("owner/repo", s["work"], **args)


def _remote_heads(origin: Path) -> dict[str, str]:
    out = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=origin)
    return dict(reversed(line.split(" ", 1)) for line in out.splitlines())


class TestSaveUnfinishedWork:
    @pytest.mark.asyncio
    async def test_uncommitted_edits_land_on_the_wip_branch(self, setup) -> None:
        s = setup
        (s["work"] / "a.txt").write_text("changed\n")
        (s["work"] / "new.py").write_text("print('half')\n")
        sha = await _save(s)

        heads = _remote_heads(s["origin"])
        assert heads[sha] == f"refs/heads/{WIP}"
        assert _git("show", f"{sha}:new.py", cwd=s["origin"]) == "print('half')"
        assert _git("show", f"{sha}:a.txt", cwd=s["origin"]) == "changed"
        assert _git("rev-parse", f"{sha}^", cwd=s["origin"]) == s["main"]
        # The run's own branch was not pushed, and main did not move.
        assert "refs/heads/fix/issue-7" not in heads.values()
        assert _git("rev-parse", "main", cwd=s["origin"]) == s["main"]
        # The worktree itself is as the agent left it.
        assert "M a.txt" in _git("status", "--porcelain", cwd=s["work"])

    @pytest.mark.asyncio
    async def test_unpushed_commits_land_on_the_wip_branch(self, setup) -> None:
        s = setup
        (s["work"] / "b.txt").write_text("b\n")
        _git("add", "b.txt", cwd=s["work"])
        _git("commit", "-q", "-m", "agent", cwd=s["work"])
        head = _git("rev-parse", "HEAD", cwd=s["work"])
        assert await _save(s) == head
        assert _remote_heads(s["origin"])[head] == f"refs/heads/{WIP}"

    @pytest.mark.asyncio
    async def test_nothing_to_save_pushes_nothing(self, setup) -> None:
        s = setup
        before = _remote_heads(s["origin"])
        assert await _save(s) is None
        assert _remote_heads(s["origin"]) == before

    @pytest.mark.asyncio
    async def test_without_a_default_branch_a_clean_commit_is_saved(
        self, setup,
    ) -> None:
        """GitHub could not say which branch is the default. The clone's
        HEAD is writable by the agent, so it is not asked: the commit is
        saved, at worst as a needless wip branch."""
        s = setup
        assert await _save(s, default_branch=None) == s["main"]

    @_NOT_ROOT
    @pytest.mark.asyncio
    async def test_an_unreadable_file_saves_the_rest_and_says_so(
        self, setup,
    ) -> None:
        from ctrlrelay.core.worktree import PartialSaveError

        s = setup
        (s["work"] / "kept.py").write_text("x\n")
        locked = s["work"] / "locked.txt"
        locked.write_text("secret\n")
        locked.chmod(0)
        try:
            with pytest.raises(PartialSaveError) as caught:
                await _save(s)
        finally:
            locked.chmod(0o600)
        sha = caught.value.sha
        assert _git("show", f"{sha}:kept.py", cwd=s["origin"]) == "x"

    @pytest.mark.asyncio
    async def test_a_commit_the_relay_already_pushed_is_not_saved_again(
        self, setup,
    ) -> None:
        s = setup
        (s["work"] / "b.txt").write_text("b\n")
        _git("add", "b.txt", cwd=s["work"])
        _git("commit", "-q", "-m", "agent", cwd=s["work"])
        head = _git("rev-parse", "HEAD", cwd=s["work"])
        before = _remote_heads(s["origin"])
        assert await _save(s, relay_pushed_shas=frozenset({head})) is None
        assert _remote_heads(s["origin"]) == before

    @pytest.mark.asyncio
    async def test_an_existing_wip_branch_is_not_overwritten(self, setup) -> None:
        s = setup
        from ctrlrelay.core.worktree import GitCommandError

        _git("update-ref", f"refs/heads/{WIP}", s["main"], cwd=s["origin"])
        (s["work"] / "new.py").write_text("x\n")
        with pytest.raises(GitCommandError, match="stale info|rejected"):
            await _save(s)
        assert _git("rev-parse", WIP, cwd=s["origin"]) == s["main"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["fix/issue-7", "main", "wip/../main"])
    async def test_only_a_wip_branch_is_pushed(self, setup, name) -> None:
        from ctrlrelay.core.publish import PushRefusedError

        s = setup
        (s["work"] / "new.py").write_text("x\n")
        before = _remote_heads(s["origin"])
        with pytest.raises(PushRefusedError):
            await _save(s, wip_branch=name)
        assert _remote_heads(s["origin"]) == before

    @pytest.mark.asyncio
    async def test_the_agents_filters_and_hooks_never_run(self, setup) -> None:
        """The agent can write the clone's config (shared with its
        worktree) and the worktree's .gitattributes. Reading its files
        must not run what they name."""
        s = setup
        marker = s["tmp"] / "ran"
        _git("config", "filter.evil.clean", f"touch {marker}; cat",
             cwd=s["bare"])
        _git("config", "core.hooksPath", str(s["tmp"] / "hooks"), cwd=s["bare"])
        hooks = s["tmp"] / "hooks"
        hooks.mkdir()
        for hook in ("pre-commit", "post-commit", "pre-push", "reference-transaction"):
            (hooks / hook).write_text(f"#!/bin/sh\ntouch {marker}\n")
            (hooks / hook).chmod(0o755)
        (s["work"] / ".gitattributes").write_text("* filter=evil\n")
        (s["work"] / "new.py").write_text("x\n")
        # Both traps are live: git run the usual way in that tree trips
        # the filter on add and the hook on commit.
        subprocess.run(["git", "add", "new.py"], cwd=s["work"], env=_ENV,
                       capture_output=True)
        assert marker.exists()
        marker.unlink()
        subprocess.run(["git", "-c", "filter.evil.clean=cat", "commit", "-qm",
                        "probe"], cwd=s["work"], env=_ENV, capture_output=True)
        assert marker.exists()
        _git("-c", "filter.evil.clean=cat", "reset", "-q", "HEAD~1",
             cwd=s["work"])
        marker.unlink()

        sha = await _save(s)
        assert sha is not None
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_the_agents_attributes_do_not_change_the_saved_bytes(
        self, setup,
    ) -> None:
        s = setup
        (s["work"] / ".gitattributes").write_text("* text eol=crlf ident\n")
        raw = b"line one\r\n$Id: kept $\r\n"
        (s["work"] / "kept.txt").write_bytes(raw)
        sha = await _save(s)
        blob = subprocess.run(
            ["git", "cat-file", "blob", f"{sha}:kept.txt"], cwd=s["origin"],
            capture_output=True, check=True,
        ).stdout
        assert blob == raw

    @_NOT_ROOT
    @pytest.mark.asyncio
    async def test_only_unreadable_changes_are_not_nothing_to_save(
        self, setup,
    ) -> None:
        from ctrlrelay.core.worktree import WorktreeError

        s = setup
        locked = s["work"] / "locked.txt"
        locked.write_text("secret\n")
        locked.chmod(0)
        try:
            with pytest.raises(WorktreeError, match="could not read"):
                await _save(s)
        finally:
            locked.chmod(0o600)

    @pytest.mark.asyncio
    async def test_a_missing_worktree_raises(self, setup) -> None:
        s = setup
        from ctrlrelay.core.worktree import WorktreeError

        _git("worktree", "remove", "--force", str(s["work"]), cwd=s["bare"])
        with pytest.raises(WorktreeError, match="not a directory"):
            await _save(s)


class TestTheDevRunOnTimeout:
    """Through `run_dev_issue`: the cleanup a timed-out run goes through."""

    async def _run(self, tmp_path: Path, save):
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue

        state_db = StateDB(tmp_path / "state.db")
        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "repos",
        )
        github = AsyncMock(spec=GitHubCLI)
        fake_relay_publish(github)
        github.get_issue.return_value = {"number": 7, "title": "t", "body": "b"}
        github.get_default_branch.return_value = "main"
        dispatcher = AsyncMock(spec=ClaudeDispatcher)

        async def spawn(**kwargs):
            return SessionResult(
                session_id=kwargs["session_id"], exit_code=-1, state=None,
                stderr="Session timed out", timed_out=True,
            )

        dispatcher.spawn_session.side_effect = spawn
        wt_path = tmp_path / "worktrees" / "wt-7"
        wt_path.mkdir(parents=True)
        verifier = AsyncMock()
        verifier.verify.return_value = VerificationResult(ready=True)
        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(worktree, "create_worktree_with_new_branch",
                         new_callable=AsyncMock, return_value=(wt_path, True)),
            patch.object(worktree, "remove_worktree",
                         new_callable=AsyncMock) as remove,
            patch.object(worktree, "delete_branch",
                         new_callable=AsyncMock) as delete,
            patch.object(worktree, "branch_exists_on_remote",
                         new_callable=AsyncMock, return_value=False),
            patch.object(worktree, "branch_exists_locally",
                         new_callable=AsyncMock, return_value=False),
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink") as unlink_ctx,
            patch.object(worktree, "save_unfinished_work",
                         new_callable=AsyncMock, side_effect=save) as saved,
        ):
            self.unlink_ctx = unlink_ctx
            result = await run_dev_issue(
                claims=None, repo="owner/repo", issue_number=7,
                branch_template="fix/issue-{n}", dispatcher=dispatcher,
                github=github, worktree=worktree, dashboard=None,
                state_db=state_db, transport=None,
                contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
            )
        state_db.close()
        comments = [
            c.kwargs.get("body", "") for c in github.comment_on_issue.await_args_list
        ]
        return result, remove, delete, saved, comments

    @pytest.mark.asyncio
    async def test_saved_work_is_named_on_the_issue(self, tmp_path) -> None:
        async def save(repo, path, **kw):
            # The relay's context link is gone before the files are read,
            # or it would be committed with them.
            assert self.unlink_ctx.called
            return "c0ffee"

        result, remove, delete, saved, comments = await self._run(tmp_path, save)
        assert not result.success and result.timed_out
        saved.assert_awaited_once()
        wip = saved.await_args.kwargs["wip_branch"]
        assert wip.startswith("wip/issue-7-")
        assert "fix/issue-7" not in wip
        remove.assert_awaited_once()
        assert any(f"`{wip}`" in c and "timed out" in c for c in comments)

    @pytest.mark.asyncio
    async def test_a_partial_save_keeps_the_worktree_and_says_so(
        self, tmp_path,
    ) -> None:
        from ctrlrelay.core.worktree import PartialSaveError

        async def save(repo, path, **kw):
            raise PartialSaveError("some unread", sha="c0ffee")

        result, remove, delete, saved, comments = await self._run(tmp_path, save)
        remove.assert_not_awaited()
        assert any(
            "except for files that could not be read" in c and "wt-7" in c
            and "wip/issue-7-" in c
            for c in comments
        )

    @pytest.mark.asyncio
    async def test_a_failed_save_keeps_the_worktree_and_branch(
        self, tmp_path,
    ) -> None:
        async def save(repo, path, **kw):
            raise RuntimeError("push rejected")

        result, remove, delete, saved, comments = await self._run(tmp_path, save)
        saved.assert_awaited_once()
        remove.assert_not_awaited()
        delete.assert_not_awaited()
        assert any("could not be saved" in c and "wt-7" in c for c in comments)

    @pytest.mark.asyncio
    async def test_a_wip_name_equal_to_the_run_branch_is_not_pushed(
        self, tmp_path,
    ) -> None:
        """A branch template could produce a `wip/` run branch; the save
        must never push to the run's own branch."""
        from ctrlrelay.pipelines.dev import save_timed_out_work

        worktree = AsyncMock()
        keep, note = await save_timed_out_work(
            worktree, AsyncMock(), AsyncMock(),
            repo="owner/repo", issue_number=7, session_id="dev-7-ab12cd34",
            worktree_path=tmp_path, branch_name="wip/issue-7-ab12cd34",
        )
        assert keep is True
        assert str(tmp_path) in note
        worktree.save_unfinished_work.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nothing_to_save_removes_the_worktree_and_keeps_the_branch(
        self, tmp_path,
    ) -> None:
        async def save(repo, path, **kw):
            return None

        result, remove, delete, saved, comments = await self._run(tmp_path, save)
        remove.assert_awaited_once()
        # The branch is kept after a timeout: it can hold commits the
        # worktree's HEAD does not reach.
        delete.assert_not_awaited()
        assert not any("wip/" in c for c in comments)
