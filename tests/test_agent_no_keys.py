"""The coding agent gets no GitHub keys; the relay does the GitHub side (#219).

Four claims, each tested through the thing that enforces it rather than
a neighbour of it:

1. A dev/task dispatch hands the child an environment with no GitHub
   token, an empty ``GH_CONFIG_DIR``, no credential helper and no SSH.
   Checked on the env the dispatcher actually passes to
   ``create_subprocess_exec``, and then by running real ``git`` and
   ``gh`` under that env against credentials planted where they would
   find them, with a control that the same credentials ARE found under
   the operator's env.
2. A dev run that ends DONE ends with the relay pushing the run branch
   to origin (a real git remote) and opening the pull request (a fake
   ``gh``), with the agent's own PR number ignored.
3. A push to main, the default branch or a foreign branch is refused
   and logged, by the push code, and origin does not move.
4. The pull request body the agent wrote is length-capped and cannot
   mention anyone or close an issue the relay did not choose.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._publish_fakes import fake_relay_publish

_SECRET_ENV = {
    "GH_TOKEN": "gh-token-must-not-leak",
    "GITHUB_TOKEN": "github-token-must-not-leak",
    "GH_ENTERPRISE_TOKEN": "ghe-token-must-not-leak",
    "GITHUB_ENTERPRISE_TOKEN": "ghe2-token-must-not-leak",
    "SSH_AUTH_SOCK": "/run/user/0/fake-agent.sock",
    "CTRLRELAY_TELEGRAM_TOKEN": "bot-token-must-not-leak",
    "GIT_ASKPASS": "/usr/bin/fake-askpass",
}


def _git(*args: str, cwd: Path | None = None, env=None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True,
        capture_output=True, text=True,
    )
    return out.stdout.strip()


async def _spawn_and_capture_env(
    tmp_path: Path, monkeypatch, *, github_access: bool
) -> tuple[dict[str, str], bool, list[str]]:
    """Run a real ClaudeDispatcher spawn with the exec faked.

    Returns ``(env, gh_dir_was_empty_dir, argv)`` as seen at exec time,
    when the child would have started.
    """
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    for k, v in _SECRET_ENV.items():
        monkeypatch.setenv(k, v)

    seen: dict = {}

    async def fake_exec(*argv, **kwargs):
        env = kwargs["env"]
        seen["env"] = dict(env)
        seen["argv"] = list(argv)
        d = env.get("GH_CONFIG_DIR")
        seen["gh_dir_empty"] = bool(
            d and Path(d).is_dir() and not any(Path(d).iterdir())
        )
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        return proc

    d = ClaudeDispatcher(claude_binary="/bin/true")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await d.spawn_session(
            session_id="s-219",
            prompt="p",
            working_dir=tmp_path,
            state_file=tmp_path / "state.json",
            github_access=github_access,
        )
    return seen["env"], seen["gh_dir_empty"], seen["argv"]


class TestTheAgentEnvironmentCarriesNoGitHubCredentials:
    @pytest.mark.asyncio
    async def test_token_vars_ssh_and_relay_secrets_are_gone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        env, gh_dir_empty, argv = await _spawn_and_capture_env(
            tmp_path, monkeypatch, github_access=False
        )
        leaked = sorted(set(_SECRET_ENV) & set(env))
        assert leaked == []
        assert not any(v in env.values() for v in _SECRET_ENV.values())
        assert gh_dir_empty, "GH_CONFIG_DIR must be an existing EMPTY dir"
        assert not Path(env["GH_CONFIG_DIR"]).exists(), (
            "the empty gh config dir is per-spawn and removed afterwards"
        )
        assert env["GIT_SSH_COMMAND"] == "false"
        assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/nonexistent/")
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        count = int(env["GIT_CONFIG_COUNT"])
        pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(count)
        }
        assert pairs["credential.helper"] == ""
        # The checkpoint protocol still works: only these two relay
        # variables are put back.
        assert env["CTRLRELAY_SESSION_ID"] == "s-219"
        assert sorted(k for k in env if k.startswith("CTRLRELAY_")) == [
            "CTRLRELAY_SESSION_ID", "CTRLRELAY_STATE_FILE",
        ]
        # What the agent needs to run at all is untouched.
        assert env["PATH"] == os.environ["PATH"]
        assert env["HOME"] == os.environ["HOME"]
        assert "--dangerously-skip-permissions" in argv

    @pytest.mark.asyncio
    async def test_control_secops_access_keeps_the_operator_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Negative control: the seeded secrets DO reach a child spawned
        with access, so the test above is not passing because they were
        never there."""
        env, _, _ = await _spawn_and_capture_env(
            tmp_path, monkeypatch, github_access=True
        )
        assert set(_SECRET_ENV) <= set(env)
        assert "GH_CONFIG_DIR" not in env or env["GH_CONFIG_DIR"] != ""

    def test_spawn_session_has_no_default_for_github_access(self) -> None:
        """A lane added later must say which it is. A default would hand
        it whichever answer was written first."""
        import inspect

        from ctrlrelay.core.dispatcher import AgentAdapter, ClaudeDispatcher

        for cls in (ClaudeDispatcher, AgentAdapter):
            p = inspect.signature(cls.spawn_session).parameters["github_access"]
            assert p.default is inspect.Parameter.empty
            assert p.kind is inspect.Parameter.KEYWORD_ONLY

    @pytest.mark.asyncio
    async def test_each_lane_asks_for_what_it_needs(self, tmp_path: Path) -> None:
        """dev and task run without access; secops is the one lane that
        still has it, explicitly."""
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline
        from ctrlrelay.pipelines.secops import SecopsPipeline
        from ctrlrelay.pipelines.task import TaskPipeline

        seen = {}
        for name, cls in (
            ("dev", DevPipeline), ("task", TaskPipeline),
            ("secops", SecopsPipeline),
        ):
            dispatcher = AsyncMock()
            dispatcher.spawn_session.return_value = MagicMock(
                agent_session_id=None, state=None, stderr="", exit_code=1,
            )
            p = cls(
                dispatcher=dispatcher, github=AsyncMock(),
                worktree=AsyncMock(), dashboard=None,
                state_db=MagicMock(), transport=None,
            )
            ctx = PipelineContext(
                session_id="s", repo="o/r", worktree_path=tmp_path,
                context_path=tmp_path / "c", state_file=tmp_path / "s.json",
                issue_number=1,
            )
            await p._spawn(ctx, "prompt", resume=False)
            seen[name] = dispatcher.spawn_session.await_args.kwargs[
                "github_access"
            ]
        assert seen == {"dev": False, "task": False, "secops": True}


class TestRealGitAndGhFindNothingUnderTheAgentEnv:
    """The env above, handed to the real tools. Each case plants a
    credential where the tool looks and runs the same command twice:
    under the operator's env (it must find it - the control) and under
    the agent's env (it must not)."""

    def _envs(self, tmp_path: Path) -> tuple[dict, dict]:
        from ctrlrelay.core.agent_env import build_agent_env

        marker = tmp_path / "helper-ran"
        helper = tmp_path / "helper.sh"
        helper.write_text(
            f"#!/bin/sh\ntouch {marker}\necho username=x\n"
            "echo password=planted-secret\n"
        )
        helper.chmod(0o755)
        ssh = tmp_path / "ssh.sh"
        ssh.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ssh-ran'}\nexit 1\n")
        ssh.chmod(0o755)
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text(
            f"[credential]\n\thelper = {helper}\n"
            f"[core]\n\tsshCommand = {ssh}\n"
        )
        # gh's default location under HOME, which is where the agent's
        # real HOME would lead it.
        ghdir = tmp_path / ".config" / "gh"
        ghdir.mkdir(parents=True)
        (ghdir / "hosts.yml").write_text(
            "github.com:\n    oauth_token: planted-gh-token\n"
            "    user: someone\n    git_protocol: https\n"
        )
        operator = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        empty = tmp_path / "empty-gh"
        empty.mkdir()
        agent, _ = build_agent_env(operator, gh_config_dir=empty)
        return operator, agent

    def test_git_credential_helpers_are_not_consulted(
        self, tmp_path: Path
    ) -> None:
        operator, agent = self._envs(tmp_path)
        req = "protocol=https\nhost=github.com\n\n"
        ok = subprocess.run(
            ["git", "credential", "fill"], input=req, env=operator,
            capture_output=True, text=True,
        )
        assert ok.returncode == 0 and "planted-secret" in ok.stdout
        (tmp_path / "helper-ran").unlink()

        denied = subprocess.run(
            ["git", "credential", "fill"], input=req, env=agent,
            capture_output=True, text=True,
        )
        assert denied.returncode != 0
        assert "planted-secret" not in denied.stdout
        assert not (tmp_path / "helper-ran").exists()

    def test_git_over_ssh_never_starts_ssh(self, tmp_path: Path) -> None:
        operator, agent = self._envs(tmp_path)
        url = "git@github.com:AInvirion/does-not-matter.git"
        subprocess.run(
            ["git", "ls-remote", url], env=operator, capture_output=True,
            timeout=30,
        )
        assert (tmp_path / "ssh-ran").exists(), "control: config ssh runs"
        (tmp_path / "ssh-ran").unlink()

        run = subprocess.run(
            ["git", "ls-remote", url], env=agent, capture_output=True,
            timeout=30,
        )
        assert run.returncode != 0
        assert not (tmp_path / "ssh-ran").exists()

    @pytest.mark.skipif(shutil.which("gh") is None, reason="gh not installed")
    def test_gh_has_no_stored_login(self, tmp_path: Path) -> None:
        """Only booleans reach the assertions: a failing assert must not
        print whatever token `gh` did find."""
        operator, agent = self._envs(tmp_path)
        ok = subprocess.run(
            ["gh", "auth", "token"], env=operator, capture_output=True,
            text=True, timeout=30,
        )
        control_found_planted = ok.stdout.strip() == "planted-gh-token"
        assert control_found_planted
        denied = subprocess.run(
            ["gh", "auth", "token"], env=agent, capture_output=True,
            text=True, timeout=30,
        )
        agent_got_a_token = denied.returncode == 0 and bool(denied.stdout.strip())
        assert not agent_got_a_token

    @pytest.mark.skipif(shutil.which("gh") is None, reason="gh not installed")
    def test_gh_cannot_reach_the_desktop_keyring(self, tmp_path: Path) -> None:
        """Measured on a laptop host, 2026-10-31: with GH_CONFIG_DIR empty,
        `gh auth token` still returned the operator's token from the
        Secret Service keyring, because the D-Bus client dials
        /run/user/<uid>/bus on its own. Run against the REAL session
        environment, so on a host with a keyring token this is the
        incident; on a host without one it passes for that reason, which
        the skip-free control in the test above already covers."""
        from ctrlrelay.core.agent_env import build_agent_env

        empty = tmp_path / "empty-gh"
        empty.mkdir()
        agent, _ = build_agent_env(dict(os.environ), gh_config_dir=empty)
        run = subprocess.run(
            ["gh", "auth", "token"], env=agent, capture_output=True,
            text=True, timeout=30,
        )
        agent_got_a_token = run.returncode == 0 and bool(run.stdout.strip())
        assert not agent_got_a_token


# --- push guard -----------------------------------------------------------


def _origin_and_bare(tmp_path: Path) -> tuple[Path, Path, str]:
    """A real origin with `main`, and the relay's bare clone of it."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    }
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed, env=env)
    origin = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(seed), str(origin))
    bare = tmp_path / "repos" / "owner-repo.git"
    bare.parent.mkdir()
    _git("clone", "-q", "--bare", str(origin), str(bare))
    main_sha = _git("rev-parse", "refs/heads/main", cwd=origin)
    return origin, bare, main_sha


def _commit_on(bare: Path, branch: str, tmp_path: Path) -> str:
    """Make `branch` in the bare clone one commit ahead of main."""
    wt = tmp_path / f"wt-{branch.replace('/', '-')}"
    _git("worktree", "add", "-q", "-b", branch, str(wt), "main", cwd=bare)
    (wt / "b.txt").write_text(branch + "\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    }
    _git("add", "b.txt", cwd=wt)
    _git("commit", "-q", "-m", "work", cwd=wt, env=env)
    return _git("rev-parse", "HEAD", cwd=wt)


def _manager(tmp_path: Path):
    from ctrlrelay.core.worktree import WorktreeManager

    return WorktreeManager(
        worktrees_dir=tmp_path / "worktrees", bare_repos_dir=tmp_path / "repos",
    )


class TestThePushGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "run_branch", "default", "why"),
        [
            ("main", "main", "main", "protected"),
            ("master", "master", "trunk", "protected"),
            ("trunk", "trunk", "trunk", "default branch"),
            ("fix/issue-9", "fix/issue-7", "main", "not this run's branch"),
        ],
        ids=["main", "master", "default", "foreign"],
    )
    async def test_refused_logged_and_origin_untouched(
        self, tmp_path: Path, caplog, target, run_branch, default, why
    ) -> None:
        from ctrlrelay.core.publish import PushRefusedError

        origin, bare, _ = _origin_and_bare(tmp_path)
        if target != "main":
            _commit_on(bare, target, tmp_path)
        else:
            # Give the bare clone's main a commit origin lacks, so the
            # push would have changed something had it run.
            _commit_on(bare, "scratch", tmp_path)
            _git("update-ref", "refs/heads/main", "refs/heads/scratch", cwd=bare)
        before = _git("for-each-ref", cwd=origin)

        caplog.set_level(logging.ERROR, logger="ctrlrelay.core.publish")
        with pytest.raises(PushRefusedError, match=why):
            await _manager(tmp_path).push_run_branch(
                "owner/repo", target, run_branch=run_branch,
                default_branch=default, expected_remote_sha=None,
                session_id="s-1",
            )
        assert _git("for-each-ref", cwd=origin) == before
        [rec] = [r for r in caplog.records if r.msg == "publish.push.refused"]
        assert rec.branch == target and rec.session_id == "s-1"

    @pytest.mark.asyncio
    async def test_the_run_branch_is_pushed_and_hooks_do_not_run(
        self, tmp_path: Path
    ) -> None:
        origin, bare, _ = _origin_and_bare(tmp_path)
        sha = _commit_on(bare, "fix/issue-7", tmp_path)
        marker = tmp_path / "hook-ran"
        hook = bare / "hooks" / "pre-push"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        # Control: the hook is live for an ordinary push.
        _commit_on(bare, "other", tmp_path)
        _git("push", "-q", "origin", "other", cwd=bare)
        assert marker.exists()
        marker.unlink()

        pushed = await _manager(tmp_path).push_run_branch(
            "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
            default_branch="main", expected_remote_sha=None,
        )
        assert pushed == sha
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == sha
        assert not marker.exists(), "an agent-written hook ran as the relay"

    @pytest.mark.asyncio
    async def test_a_branch_with_no_new_commits_is_not_pushed(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.publish import PushRefusedError

        origin, bare, _ = _origin_and_bare(tmp_path)
        _git("branch", "fix/issue-7", "main", cwd=bare)
        with pytest.raises(PushRefusedError, match="no commits"):
            await _manager(tmp_path).push_run_branch(
                "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
                default_branch="main", expected_remote_sha=None,
            )
        assert "fix/issue-7" not in _git("for-each-ref", cwd=origin)


# --- untrusted PR text -----------------------------------------------------


class TestThePullRequestBodyIsTreatedAsUntrusted:
    def test_length_capped_with_the_drop_named(self) -> None:
        from ctrlrelay.core.publish import PR_BODY_MAX_CHARS, build_pr_body

        body = build_pr_body("x" * 50_000, issue_number=7, session_id="s")
        agent_part, footer = body.split("\n\n---\n", 1)
        assert agent_part == (
            "x" * PR_BODY_MAX_CHARS
            + f"\n\n[truncated by ctrlrelay: {50_000 - PR_BODY_MAX_CHARS} "
            "characters dropped]"
        )
        assert footer.startswith("Closes #7\n")

    def test_under_the_cap_is_unchanged(self) -> None:
        from ctrlrelay.core.publish import build_pr_body

        body = build_pr_body("Short and plain.", issue_number=7, session_id="s")
        assert body.split("\n\n---\n", 1)[0] == "Short and plain."

    def test_mentions_and_foreign_closing_keywords_are_disabled(self) -> None:
        from ctrlrelay.core.publish import build_pr_body

        body = build_pr_body(
            "Thanks @octocat and @org/team.\nFixes #999, closes owner/x#3.\n"
            "Email a@b.example stays.",
            issue_number=7, session_id="s",
        )
        agent_part = body.split("\n\n---\n", 1)[0]
        assert agent_part == (
            "Thanks @​octocat and @​org/team.\n"
            "Fixes​ #999, closes​ owner/x#3.\n"
            "Email a@​b.example stays."
        )

    def test_title_is_one_capped_line_with_a_fallback(self) -> None:
        from ctrlrelay.core.publish import PR_TITLE_MAX_CHARS, build_pr_title

        assert build_pr_title(
            "Fix it\nsecond line", issue_number=7, issue_title="t"
        ) == "Fix it"
        assert build_pr_title(
            None, issue_number=7, issue_title="Login broken"
        ) == "Login broken (#7)"
        long = build_pr_title("y" * 500, issue_number=7, issue_title="t")
        assert len(long) == PR_TITLE_MAX_CHARS and long.endswith("...")


# --- a whole dev run -------------------------------------------------------


class TestADevRunEndsWithTheRelayPublishing:
    @pytest.mark.asyncio
    async def test_relay_pushes_the_branch_and_opens_the_pr(
        self, tmp_path: Path
    ) -> None:
        """Real git for the clone, worktree and push; fake GitHub for the
        API. The agent commits and reports PR #999, which does not exist:
        the result must carry the relay's PR, not the agent's claim."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.publish import PR_BODY_MAX_CHARS
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        origin, bare, main_sha = _origin_and_bare(tmp_path)
        github = AsyncMock(spec=GitHubCLI)
        fake_relay_publish(github, pr_number=77)
        github.get_issue.return_value = {
            "number": 7, "title": "Login broken", "body": "...",
            "comments": [],
        }
        commits: dict[str, str] = {}

        async def agent(**kw):
            assert kw["github_access"] is False
            wt = kw["working_dir"]
            (wt / "fix.txt").write_text("fixed\n")
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x",
                "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x",
            }
            _git("add", "fix.txt", cwd=wt)
            _git("commit", "-q", "-m", "fix", cwd=wt, env=env)
            commits["sha"] = _git("rev-parse", "HEAD", cwd=wt)
            sf = kw["state_file"]
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(json.dumps({
                "version": "1", "status": "DONE",
                "session_id": kw["session_id"],
                "timestamp": "2026-10-31T00:00:00Z",
                "summary": "done",
                "outputs": {
                    "pr_title": "Fix login",
                    "pr_body": "@octocat " + "z" * 30_000,
                    "pr_number": 999,
                    "pr_url": "https://github.com/owner/repo/pull/999",
                },
            }))
            return SessionResult(
                session_id=kw["session_id"], exit_code=0,
                state=read_checkpoint(sf),
            )

        dispatcher = AsyncMock()
        dispatcher.spawn_session.side_effect = agent
        verifier = AsyncMock()
        verifier.verify.return_value = VerificationResult(ready=True)
        state_db = StateDB(tmp_path / "state.db")
        try:
            result = await run_dev_issue(
                repo="owner/repo", issue_number=7,
                branch_template="fix/issue-{n}", dispatcher=dispatcher,
                github=github, worktree=_manager(tmp_path), dashboard=None,
                state_db=state_db, transport=None,
                contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
                code_review=MagicMock(method="off"),
            )
        finally:
            state_db.close()

        assert result.success, result
        assert result.outputs["pr_number"] == 77
        assert result.outputs["pr_url"] == "https://github.com/owner/repo/pull/77"
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == (
            commits["sha"]
        )
        assert _git("rev-parse", "refs/heads/main", cwd=origin) == main_sha
        github.create_pr.assert_awaited_once()
        kw = github.create_pr.await_args.kwargs
        assert (kw["head"], kw["base"], kw["title"]) == (
            "fix/issue-7", "main", "Fix login",
        )
        agent_part = kw["body"].split("\n\n---\n", 1)[0]
        assert agent_part.startswith("@​octocat zzz")
        assert len(agent_part) < PR_BODY_MAX_CHARS + 100
        assert "Closes #7" in kw["body"]
        verifier.verify.assert_awaited_once_with("owner/repo", 77)

    @pytest.mark.asyncio
    async def test_a_refused_publish_fails_the_run_and_keeps_the_work(
        self, tmp_path: Path
    ) -> None:
        """branch_template pointing at main: the run fails, nothing is
        pushed, no PR is opened, and the agent's commits are not deleted
        with the branch."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        github = AsyncMock(spec=GitHubCLI)
        fake_relay_publish(github)
        github.get_issue.return_value = {"number": 7, "title": "t", "body": ""}
        worktree = AsyncMock()
        worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "wt", True,
        )
        worktree.branch_exists_on_remote.return_value = False
        worktree.symlink_context = MagicMock()
        worktree.remove_context_symlink = MagicMock()
        real = _manager(tmp_path)
        worktree.push_run_branch.side_effect = real.push_run_branch

        dispatcher = AsyncMock()
        dispatcher.spawn_session.return_value = SessionResult(
            session_id="x", exit_code=0,
            state=CheckpointState(
                status=CheckpointStatus.DONE, session_id="x",
                outputs={"pr_title": "t", "pr_number": 999},
            ),
        )
        state_db = StateDB(tmp_path / "state.db")
        try:
            result = await run_dev_issue(
                repo="owner/repo", issue_number=7, branch_template="main",
                dispatcher=dispatcher, github=github, worktree=worktree,
                dashboard=None, state_db=state_db, transport=None,
                contexts_dir=tmp_path / "contexts",
                code_review=MagicMock(method="off"),
            )
        finally:
            state_db.close()

        assert not result.success
        assert "push refused" in (result.error or "")
        # The agent's claimed PR is not carried into the failure, where
        # the claim-retraction comment would cite it as real.
        assert "pr_number" not in result.outputs
        github.create_pr.assert_not_awaited()
        worktree.delete_branch.assert_not_awaited()


class TestAFixRoundIsPushedByTheRelay:
    @pytest.mark.asyncio
    async def test_fix_round_gets_logs_and_is_pushed_leased_on_the_last_push(
        self, tmp_path: Path
    ) -> None:
        """CI fails once. The relay fetches the failed job's log for the
        agent (which cannot), resumes it with local-only instructions,
        and pushes the fix leased against the SHA it pushed first. A
        fix nobody pushed would be re-verified against the old head."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        github = AsyncMock()
        fake_relay_publish(github, pr_number=77)
        github.get_issue.return_value = {"number": 7, "title": "t", "body": ""}
        github.get_failed_job_log.return_value = "line1\nE   assert 1 == 2\n"
        worktree = AsyncMock()
        worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "wt", True,
        )
        worktree.symlink_context = MagicMock()
        worktree.remove_context_symlink = MagicMock()
        worktree.push_run_branch.side_effect = ["sha-1", "sha-2"]

        prompts: list[str] = []

        async def agent(**kw):
            prompts.append(kw["prompt"])
            return SessionResult(
                session_id=kw["session_id"], exit_code=0,
                state=CheckpointState(
                    status=CheckpointStatus.DONE, session_id="x",
                    outputs={"pr_title": "t", "pr_body": "b"},
                ),
            )

        dispatcher = AsyncMock()
        dispatcher.spawn_session.side_effect = agent
        verifier = AsyncMock()
        verifier.verify.side_effect = [
            VerificationResult(
                ready=False, reason="checks failed",
                failing_checks=[{
                    "name": "tests", "state": "FAILURE",
                    "link": "https://github.com/owner/repo/actions/runs/1/job/555",
                }],
            ),
            VerificationResult(ready=True),
        ]
        state_db = StateDB(tmp_path / "state.db")
        try:
            result = await run_dev_issue(
                repo="owner/repo", issue_number=7,
                branch_template="fix/issue-{n}", dispatcher=dispatcher,
                github=github, worktree=worktree, dashboard=None,
                state_db=state_db, transport=None,
                contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
                code_review=MagicMock(method="off"),
            )
        finally:
            state_db.close()

        assert result.success, result
        assert result.outputs["pr_number"] == 77
        pushes = worktree.push_run_branch.await_args_list
        assert [c.kwargs["expected_remote_sha"] for c in pushes] == [
            None, "sha-1",
        ]
        github.create_pr.assert_awaited_once()
        github.get_failed_job_log.assert_awaited_once()
        assert github.get_failed_job_log.await_args.args[:2] == (
            "owner/repo", "555",
        )
        fix_prompt = prompts[1]
        assert "E   assert 1 == 2" in fix_prompt
        assert "Do not push or fetch" in fix_prompt
        assert "gh run view" not in fix_prompt


def test_the_bwrap_prefix_hides_only_the_named_paths(tmp_path: Path) -> None:
    from ctrlrelay.core.agent_env import bwrap_prefix

    d = tmp_path / "ssh"
    d.mkdir()
    f = tmp_path / "netrc"
    f.write_text("machine x")
    argv = bwrap_prefix(
        "/usr/bin/bwrap", [str(d), str(f), str(tmp_path / "missing")]
    )
    assert argv == [
        "/usr/bin/bwrap", "--dev-bind", "/", "/", "--die-with-parent",
        "--tmpfs", str(d), "--ro-bind", "/dev/null", str(f), "--",
    ]


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_under_bwrap_the_hidden_dir_reads_empty(tmp_path: Path) -> None:
    from ctrlrelay.core.agent_env import bwrap_prefix

    works = subprocess.run(
        [shutil.which("bwrap"), "--dev-bind", "/", "/", "true"],
        capture_output=True, text=True, timeout=30,
    )
    if works.returncode != 0:
        pytest.skip(f"bwrap cannot run on this host: {works.stderr.strip()}")
    secret = tmp_path / "dotssh"
    secret.mkdir()
    (secret / "id_ed25519").write_text("planted key")
    probe = subprocess.run(
        [*bwrap_prefix(shutil.which("bwrap"), [str(secret)]),
         "ls", "-A", str(secret)],
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == ""
    assert (secret / "id_ed25519").read_text() == "planted key"
