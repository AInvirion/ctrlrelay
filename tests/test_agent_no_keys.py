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
) -> tuple[dict[str, str], bool, list[str], bool]:
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
        r = env.get("XDG_RUNTIME_DIR")
        seen["run_empty"] = bool(
            r and Path(r).is_dir() and not any(Path(r).iterdir())
        )
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        return proc

    d = ClaudeDispatcher(usage_store=None, claude_binary="/bin/true")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await d.spawn_session(
            session_id="s-219",
            prompt="p",
            working_dir=tmp_path,
            state_file=tmp_path / "state.json",
            github_access=github_access,
        )
    return seen["env"], seen["gh_dir_empty"], seen["argv"], seen["run_empty"]


class TestTheAgentEnvironmentCarriesNoGitHubCredentials:
    @pytest.mark.asyncio
    async def test_token_vars_ssh_and_relay_secrets_are_gone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        env, gh_dir_empty, argv, seen_runtime_empty = await _spawn_and_capture_env(
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
        assert seen_runtime_empty, "XDG_RUNTIME_DIR must be an existing EMPTY dir"
        assert not Path(env["XDG_RUNTIME_DIR"]).exists()
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
        env, _, _, _ = await _spawn_and_capture_env(
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
        run = tmp_path / "empty-run"
        run.mkdir()
        agent, _ = build_agent_env(operator, gh_config_dir=empty, runtime_dir=run)
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
    def test_no_session_bus_is_reachable_by_address_or_by_xdg(
        self, tmp_path: Path
    ) -> None:
        """gh falls back to the desktop keyring over the D-Bus session bus
        when its config has no token (measured on a laptop host,
        2026-10-31: it printed the operator's token with GH_CONFIG_DIR
        empty). gh has no switch to turn the keyring off.

        Two fake session buses (unix sockets that count connections):
        A at an explicit DBUS_SESSION_BUS_ADDRESS, B at
        $XDG_RUNTIME_DIR/bus. Three clients: gh itself, a reference
        client that follows the D-Bus rule (address if set, else
        $XDG_RUNTIME_DIR/bus), and one that only looks under
        $XDG_RUNTIME_DIR. Controls: under operator envs the python
        clients reach A and B, and gh reaches A. Claim: under the agent
        env built from each, no client reaches either bus.

        What this proves: the agent env routes both the address and the
        XDG lookup away from anything the operator env points at. What it
        cannot show: gh's client, when no address is set, dials
        /run/user/<uid>/bus by uid and ignores XDG_RUNTIME_DIR (measured;
        that is why gh never reaches B even under the operator env). That
        path is closed only because the agent env always sets an
        unconnectable address, which the env test above pins. No real
        keyring is involved in this test either way.
        """
        import socket
        import sys
        import threading

        from ctrlrelay.core.agent_env import build_agent_env

        xdg = tmp_path / "xdg"
        xdg.mkdir()
        buses = {"A": tmp_path / "bus-a", "B": xdg / "bus"}
        hits = {"A": 0, "B": 0}
        servers = []
        stop = threading.Event()

        def serve(name: str, srv: socket.socket) -> None:
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    continue
                hits[name] += 1
                conn.close()

        for name, path in buses.items():
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(str(path))
            srv.listen(8)
            srv.settimeout(0.1)
            servers.append(srv)
            threading.Thread(target=serve, args=(name, srv), daemon=True).start()

        ref_client = (
            "import os, socket\n"
            "a = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')\n"
            "p = a[len('unix:path='):] if a.startswith('unix:path=') else "
            "os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/nonexistent'), 'bus')\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "try:\n    s.connect(p)\nexcept OSError:\n    pass\n"
        )

        # Some clients (sd-bus's user bus, for one) look only at
        # $XDG_RUNTIME_DIR/bus. This one does, so the XDG override is
        # tested on its own and not only behind the address.
        xdg_only_client = (
            "import os, socket\n"
            "p = os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/nonexistent'), 'bus')\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "try:\n    s.connect(p)\nexcept OSError:\n    pass\n"
        )

        def dial(env: dict) -> dict:
            before = dict(hits)
            for client in (ref_client, xdg_only_client):
                subprocess.run([sys.executable, "-c", client], env=env,
                               capture_output=True, timeout=30)
            subprocess.run(["gh", "auth", "token"], env=env,
                           capture_output=True, timeout=30)
            import time as _t
            _t.sleep(0.3)
            return {k: hits[k] - before[k] for k in hits}

        try:
            empty = tmp_path / "gh-empty"
            empty.mkdir()
            base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                    "GH_CONFIG_DIR": str(empty)}
            by_address = {**base,
                          "DBUS_SESSION_BUS_ADDRESS": f"unix:path={buses['A']}",
                          "XDG_RUNTIME_DIR": str(xdg)}
            by_xdg = {**base, "XDG_RUNTIME_DIR": str(xdg)}
            control_a = dial(by_address)
            control_b = dial(by_xdg)
            agent_results = []
            for i, operator in enumerate((by_address, by_xdg)):
                gh_dir = tmp_path / f"gh-agent-{i}"
                run_dir = tmp_path / f"run-agent-{i}"
                gh_dir.mkdir()
                run_dir.mkdir()
                agent, _ = build_agent_env(
                    operator, gh_config_dir=gh_dir, runtime_dir=run_dir
                )
                agent_results.append(dial(agent))
        finally:
            stop.set()
            for srv in servers:
                srv.close()
        # by address: the rule client and gh reach A, the XDG-only
        # client reaches B. By XDG: both python clients reach B (gh goes
        # to /run/user/<uid>/bus, see the docstring).
        assert control_a == {"A": 2, "B": 1}
        assert control_b == {"A": 0, "B": 2}
        assert agent_results == [{"A": 0, "B": 0}, {"A": 0, "B": 0}]


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


@pytest.fixture
def push_to_local_origin(monkeypatch):
    """Let the relay's push reach a local origin instead of GitHub.

    The ONLY relaxations, both test-side: the relay-written global
    config maps the explicit GitHub URL to the local origin, and
    ``protocol.file.allow=never`` is dropped from the command line so a
    local path is reachable at all. Everything else (fresh private git
    dir, hooks and fsmonitor off, no system config, no inherited GIT_*)
    is the production path.
    """
    import ctrlrelay.core.worktree as wt

    def install(origin: Path) -> None:
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

    return install


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
                relay_pushed_shas=frozenset(), session_id="s-1",
            )
        assert _git("for-each-ref", cwd=origin) == before
        [rec] = [r for r in caplog.records if r.msg == "publish.push.refused"]
        assert rec.branch == target and rec.session_id == "s-1"

    @pytest.mark.asyncio
    async def test_the_run_branch_is_pushed_and_hooks_do_not_run(
        self, tmp_path: Path, push_to_local_origin
    ) -> None:
        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
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
                relay_pushed_shas=frozenset(),
        )
        assert pushed == sha
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == sha
        assert not marker.exists(), "an agent-written hook ran as the relay"

    @pytest.mark.asyncio
    async def test_agent_written_config_in_the_shared_clone_has_no_effect(
        self, tmp_path: Path, push_to_local_origin
    ) -> None:
        """The agent can write the bare clone's config from its worktree.
        Plant every redirect we know of there, each one pointing at an
        attacker origin or a marker-writing command, and check the push
        honoured none: the commit lands in the real origin, the attacker
        origin is untouched, no marker exists."""
        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        sha = _commit_on(bare, "fix/issue-7", tmp_path)
        evil = tmp_path / "evil.git"
        _git("init", "-q", "--bare", str(evil))
        marker = tmp_path / "honoured"
        cmd = tmp_path / "evil.sh"
        cmd.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {marker}\nexit 1\n")
        cmd.chmod(0o755)
        included = tmp_path / "included.cfg"
        included.write_text(f"[credential]\n\thelper = {cmd}\n")
        for key, value in [
            ("core.sshCommand", str(cmd)),
            ("credential.helper", str(cmd)),
            ("core.askPass", str(cmd)),
            ("core.fsmonitor", str(cmd)),
            ("core.hooksPath", str(tmp_path)),
            ("remote.origin.url", str(evil)),
            ("remote.origin.pushurl", str(evil)),
            (f"url.{evil}.insteadOf", "https://github.com/"),
            (f"url.{evil}.pushInsteadOf", "https://github.com/"),
            ("include.path", str(included)),
        ]:
            _git("config", "--add", key, value, cwd=bare)
        for hook in ("pre-push", "reference-transaction", "post-checkout"):
            h = bare / "hooks" / hook
            h.write_text(f"#!/bin/sh\ntouch {marker}\n")
            h.chmod(0o755)

        pushed = await _manager(tmp_path).push_run_branch(
            "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
            default_branch="main", expected_remote_sha=None,
                relay_pushed_shas=frozenset(),
        )
        assert pushed == sha
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == sha
        assert _git("for-each-ref", cwd=evil) == ""
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_every_credentialed_call_uses_relay_config_and_an_explicit_url(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """argv and env of each git call the push makes. None may name
        `origin`, read the shared clone's config for a network operation,
        or inherit a GIT_* variable from the relay's own environment."""
        from ctrlrelay.core.worktree import WorktreeManager

        monkeypatch.setenv("GIT_SSH_COMMAND", "/tmp/should-not-be-inherited")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/operator-gitconfig")
        calls: list[tuple[tuple[str, ...], dict]] = []

        async def fake_run_git(self, *args, cwd=None, timeout=None, env=None, **kw):
            calls.append((args, dict(env or {})))
            if args[0] == "init":
                (Path(args[-1]) / "objects" / "info").mkdir(parents=True)
            if "rev-list" in args:
                return "1\n"
            return ""

        monkeypatch.setattr(WorktreeManager, "_run_git", fake_run_git)
        heads = tmp_path / "repos" / "owner-repo.git" / "refs" / "heads"
        (heads / "fix").mkdir(parents=True)
        (heads / "fix" / "issue-7").write_text("a" * 40 + "\n")
        (heads / "main").write_text("b" * 40 + "\n")
        await _manager(tmp_path).push_run_branch(
            "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
            default_branch="main", expected_remote_sha=None,
                relay_pushed_shas=frozenset(),
        )
        bare = str(tmp_path / "repos" / "owner-repo.git")
        flags = [
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
        ]
        network = [a for a, _ in calls if "ls-remote" in a or "push" in a]
        assert [a[a.index("ls-remote") if "ls-remote" in a else a.index("push"):]
                for a in network] == [
            ("ls-remote", "--heads", "https://github.com/owner/repo.git",
             "refs/heads/fix/issue-7"),
            ("push", "--no-verify", "--porcelain",
             "--force-with-lease=refs/heads/fix/issue-7:",
             "https://github.com/owner/repo.git",
             "a" * 40 + ":refs/heads/fix/issue-7"),
        ]
        for args, env in calls:
            assert "origin" not in args
            if args[0] != "init":
                assert list(args[2:2 + len(flags)]) == flags, args
            assert env["GIT_CONFIG_NOSYSTEM"] == "1"
            assert Path(env["GIT_CONFIG_GLOBAL"]).parent.parent == (
                tmp_path / "push"
            )
            assert "GIT_SSH_COMMAND" not in env
        # No git process runs in the shared clone at all: its heads are
        # read from the ref files, and every call uses the private dir.
        for args, _ in calls:
            if args[0] == "init":
                continue
            gitdir = args[args.index("--git-dir") + 1]
            assert gitdir != bare and gitdir.startswith(str(tmp_path / "push"))
        assert not any((tmp_path / "push").iterdir()), "private dir left behind"

    @pytest.mark.asyncio
    async def test_a_remote_branch_ctrlrelay_did_not_push_is_not_overwritten(
        self, tmp_path: Path, push_to_local_origin, caplog
    ) -> None:
        from ctrlrelay.core.publish import PushRefusedError

        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        _commit_on(bare, "fix/issue-7", tmp_path)
        # Somebody else's branch of the same name is already on GitHub.
        other = _commit_on(bare, "someone", tmp_path)
        _git("push", "-q", str(origin), f"{other}:refs/heads/fix/issue-7",
             cwd=bare)
        caplog.set_level(logging.ERROR, logger="ctrlrelay.core.worktree")
        with pytest.raises(PushRefusedError, match="ctrlrelay did not push"):
            await _manager(tmp_path).push_run_branch(
                "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
                default_branch="main", expected_remote_sha=None,
                relay_pushed_shas=frozenset({"0" * 40}),
            )
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == other
        assert any(r.msg == "publish.push.refused" for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_remote_head_ctrlrelay_pushed_is_leased_and_replaced(
        self, tmp_path: Path, push_to_local_origin
    ) -> None:
        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        old = _commit_on(bare, "older", tmp_path)
        _git("push", "-q", str(origin), f"{old}:refs/heads/fix/issue-7",
             cwd=bare)
        new = _commit_on(bare, "fix/issue-7", tmp_path)
        pushed = await _manager(tmp_path).push_run_branch(
            "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
            default_branch="main", expected_remote_sha=None,
            relay_pushed_shas=frozenset({old}),
        )
        assert pushed == new
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == new

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
                relay_pushed_shas=frozenset(),
            )
        assert "fix/issue-7" not in _git("for-each-ref", cwd=origin)


class TestARejectedPushReportsWhyTheRemoteRefusedIt:
    """#269: diagnosing two stranded issues took about an hour, almost
    all of it spent recovering a reason git had already printed. The
    log line for each failure was

        git failed: error: failed to push some refs to '<url>'

    which is git's summary line. The push runs with ``--porcelain``, so
    the line naming the rejected ref and the remote's reason went to
    STDOUT, and ``_run_git`` raised with stderr only.

    The surviving line names the URL, which invited the conclusion that
    the transport or the credential was at fault. Neither was: pushes
    to the same URL with the same credential succeeded minutes before
    and after.
    """

    @pytest.mark.asyncio
    async def test_a_stale_lease_rejection_names_the_ref_and_the_reason(
        self, tmp_path: Path, push_to_local_origin, caplog
    ) -> None:
        """The reported case exactly: stderr carries only the summary
        line, and the reason exists on stdout or nowhere."""
        from ctrlrelay.core.worktree import PushRejectedError

        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        # The branch is on origin at `other`, and we lease on a sha it
        # is not at -- what a resumed session sees when somebody else
        # moved the branch. Real git answers `[rejected] (stale info)`.
        other = _commit_on(bare, "someone", tmp_path)
        _git("push", "-q", str(origin), f"{other}:refs/heads/fix/issue-7",
             cwd=bare)
        _commit_on(bare, "fix/issue-7", tmp_path)

        caplog.set_level(logging.ERROR, logger="ctrlrelay.core.worktree")
        with pytest.raises(PushRejectedError) as caught:
            await _manager(tmp_path).push_run_branch(
                "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
                default_branch="main", expected_remote_sha="0" * 40,
                relay_pushed_shas=frozenset({"0" * 40}),
            )
        message = str(caught.value)
        assert "stale info" in message, "the reason is still being dropped"
        assert "refs/heads/fix/issue-7" in message, "the ref is not named"
        # Keep the url: the complaint was never that it is there, only
        # that it was all that was there.
        assert "https://github.com/owner/repo.git" in message
        # And the reason is its own field, not something a reader has
        # to parse back out of a capped message.
        assert "stale info" in (caught.value.reason or "")
        [rec] = [
            r for r in caplog.records if r.msg == "publish.push.rejected"
        ]
        assert "stale info" in rec.reason
        assert rec.branch == "fix/issue-7"
        # Origin did not move.
        assert _git("rev-parse", "refs/heads/fix/issue-7", cwd=origin) == other

    @pytest.mark.asyncio
    async def test_the_reason_was_on_stdout_and_nowhere_else(
        self, tmp_path: Path, push_to_local_origin
    ) -> None:
        """A negative control for the test above: if the reason were on
        stderr, including stdout would have been unnecessary and this
        test would pass with the fix reverted. Run the same push as
        plain git and read the two streams apart."""
        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        other = _commit_on(bare, "someone", tmp_path)
        _git("push", "-q", str(origin), f"{other}:refs/heads/fix/issue-7",
             cwd=bare)
        sha = _commit_on(bare, "fix/issue-7", tmp_path)

        proc = subprocess.run(
            ["git", "push", "--porcelain",
             "--force-with-lease=refs/heads/fix/issue-7:" + "0" * 40,
             str(origin), f"{sha}:refs/heads/fix/issue-7"],
            cwd=bare, capture_output=True, text=True,
        )
        assert proc.returncode != 0
        assert "stale info" in proc.stdout
        assert "stale info" not in proc.stderr
        assert "failed to push some refs" in proc.stderr

    @pytest.mark.asyncio
    async def test_a_remote_that_explains_itself_keeps_both_streams(
        self, tmp_path: Path, push_to_local_origin
    ) -> None:
        """A hook rejection puts the summary on stdout and the remote's
        own words on stderr behind `remote:`. Both are the reason, so
        both are kept."""
        from ctrlrelay.core.worktree import PushRejectedError

        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        _commit_on(bare, "fix/issue-7", tmp_path)
        hook = origin / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "echo 'refusing: required status check has not run' >&2\n"
            "exit 1\n"
        )
        hook.chmod(0o755)

        with pytest.raises(PushRejectedError) as caught:
            await _manager(tmp_path).push_run_branch(
                "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
                default_branch="main", expected_remote_sha=None,
                relay_pushed_shas=frozenset(),
            )
        message = str(caught.value)
        assert "pre-receive hook declined" in message      # stdout
        assert "required status check has not run" in message  # stderr
        assert "fix/issue-7" not in _git("for-each-ref", cwd=origin)

    @pytest.mark.asyncio
    async def test_a_push_that_never_got_a_per_ref_answer_is_not_a_rejection(
        self, tmp_path: Path, push_to_local_origin
    ) -> None:
        """No reachable remote means no `!` line, and stderr is the
        whole story. Reporting that as a rejection would invent a
        remote answer that was never given."""
        from ctrlrelay.core.worktree import GitCommandError, PushRejectedError

        origin, bare, _ = _origin_and_bare(tmp_path)
        push_to_local_origin(origin)
        _commit_on(bare, "fix/issue-7", tmp_path)
        shutil.rmtree(origin)

        with pytest.raises(GitCommandError) as caught:
            await _manager(tmp_path).push_run_branch(
                "owner/repo", "fix/issue-7", run_branch="fix/issue-7",
                default_branch="main", expected_remote_sha=None,
                relay_pushed_shas=frozenset(),
            )
        assert not isinstance(caught.value, PushRejectedError)
        assert "git failed:" in str(caught.value)


class TestRefFilesAreReadOnlyWhenSafe:
    """The push reads the clone's branch heads from its ref files. The
    agent can write those files, so a symlink anywhere on the way, or a
    name that walks out of refs/, must be refused, not followed."""

    A = "a" * 40
    B = "b" * 40

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "owner-repo.git"
        (repo / "refs" / "heads").mkdir(parents=True)
        return repo

    def test_control_plain_loose_and_packed_refs_are_read(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.worktree import _read_branch_head

        repo = self._repo(tmp_path)
        (repo / "refs" / "heads" / "fix").mkdir()
        (repo / "refs" / "heads" / "fix" / "issue-7").write_text(self.A + "\n")
        (repo / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{self.B} refs/heads/main\n"
        )
        assert _read_branch_head(repo, "fix/issue-7") == self.A
        assert _read_branch_head(repo, "main") == self.B

    def test_a_symlinked_parent_dir_is_refused(self, tmp_path: Path) -> None:
        from ctrlrelay.core.publish import PushRefusedError
        from ctrlrelay.core.worktree import _read_branch_head

        repo = self._repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "issue-7").write_text(self.A + "\n")
        (repo / "refs" / "heads" / "fix").symlink_to(outside)
        with pytest.raises(PushRefusedError, match="symlink"):
            _read_branch_head(repo, "fix/issue-7")

    def test_a_symlinked_packed_refs_is_refused(self, tmp_path: Path) -> None:
        from ctrlrelay.core.publish import PushRefusedError
        from ctrlrelay.core.worktree import _read_branch_head

        repo = self._repo(tmp_path)
        outside = tmp_path / "packed"
        outside.write_text(f"{self.A} refs/heads/fix/issue-7\n")
        (repo / "packed-refs").symlink_to(outside)
        with pytest.raises(PushRefusedError, match="symlink"):
            _read_branch_head(repo, "fix/issue-7")

    @pytest.mark.parametrize(
        "name", ["fix/../../../outside", "../x", "fix/.hidden", "a//b"],
    )
    def test_a_name_that_walks_out_is_refused(
        self, tmp_path: Path, name: str
    ) -> None:
        from ctrlrelay.core.publish import PushRefusedError
        from ctrlrelay.core.worktree import _read_branch_head

        repo = self._repo(tmp_path)
        (tmp_path / "outside").write_text(self.A + "\n")
        with pytest.raises(PushRefusedError, match="not a safe ref name"):
            _read_branch_head(repo, name)

    def test_an_unsafe_loose_ref_does_not_fall_back_to_packed(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.publish import PushRefusedError
        from ctrlrelay.core.worktree import _read_branch_head

        repo = self._repo(tmp_path)
        outside = tmp_path / "elsewhere"
        outside.write_text(self.A + "\n")
        (repo / "refs" / "heads" / "issue-7").symlink_to(outside)
        (repo / "packed-refs").write_text(f"{self.B} refs/heads/issue-7\n")
        with pytest.raises(PushRefusedError, match="symlink"):
            _read_branch_head(repo, "issue-7")


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
        assert build_pr_title(
            "", issue_number=7, issue_title="Please investigate @octocat"
        ) == "Please investigate @\u200boctocat (#7)"
        long = build_pr_title("y" * 500, issue_number=7, issue_title="t")
        assert len(long) == PR_TITLE_MAX_CHARS and long.endswith("...")


# --- a whole dev run -------------------------------------------------------


class TestADevRunEndsWithTheRelayPublishing:
    @pytest.mark.asyncio
    async def test_relay_pushes_the_branch_and_opens_the_pr(
        self, tmp_path: Path, push_to_local_origin
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
        push_to_local_origin(origin)
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
                claims=None,
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
                claims=None,
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


def _publisher(tmp_path: Path, github, worktree, state_db, session_id="s-1"):
    from ctrlrelay.pipelines.dev import _Publisher

    return _Publisher(
        github=github, worktree=worktree, state_db=state_db,
        repo="owner/repo", branch="fix/issue-7", issue_number=7,
        issue_title="Login broken", session_id=session_id,
    )


def _done(**outputs):
    from ctrlrelay.pipelines.base import PipelineResult

    return PipelineResult(
        success=True, session_id="s-1", summary="done", outputs=outputs,
    )


class TestThePublisher:
    @pytest.mark.asyncio
    async def test_a_resumed_session_leases_on_what_it_pushed_before(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        github = AsyncMock()
        fake_relay_publish(github)
        worktree = AsyncMock()
        worktree.push_run_branch.side_effect = ["sha-1", "sha-2"]
        try:
            first = await _publisher(tmp_path, github, worktree, db).publish(
                _done()
            )
            assert first.success
            # A fresh publisher, as resume_dev_from_pending builds one.
            again = await _publisher(tmp_path, github, worktree, db).publish(
                _done()
            )
            assert again.success
            kwargs = [c.kwargs for c in worktree.push_run_branch.await_args_list]
            assert [k["expected_remote_sha"] for k in kwargs] == [None, "sha-1"]
            assert kwargs[1]["relay_pushed_shas"] == frozenset({"sha-1"})
            assert db.relay_pushed_shas(
                repo="owner/repo", branch="fix/issue-7"
            ) == frozenset({"sha-1", "sha-2"})
        finally:
            db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("owner", "base", "reused"),
        [
            ("owner", "main", True),
            ("forker", "main", False),
            ("owner", "release", False),
        ],
        ids=["same-repo-default-base", "fork-head", "other-base"],
    )
    async def test_an_open_pr_is_reused_only_when_it_is_ours(
        self, tmp_path: Path, owner, base, reused
    ) -> None:
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        github = AsyncMock()
        fake_relay_publish(github)
        github.list_prs.return_value = [{
            "number": 5, "headRefName": "fix/issue-7", "baseRefName": base,
            "headRepositoryOwner": {"login": owner},
            "headRepository": {"name": "repo"},
        }]
        worktree = AsyncMock()
        worktree.push_run_branch.return_value = "sha-1"
        try:
            result = await _publisher(tmp_path, github, worktree, db).publish(
                _done()
            )
        finally:
            db.close()
        github.create_pr.assert_not_awaited()
        if reused:
            assert result.success and result.outputs["pr_number"] == 5
        else:
            assert not result.success
            assert "refusing to reuse PR #5" in (result.error or "")
            assert "pr_number" not in result.outputs

    @pytest.mark.asyncio
    async def test_a_rejected_push_puts_the_remotes_reason_in_the_event(
        self, tmp_path: Path, caplog
    ) -> None:
        """#269 asked for the `!` line's reason in `dev.publish.failed`
        as its own field. The message field is cut at 300 characters, so
        a reason that only lives in the message is a reason that can be
        cut out of it."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import PushRejectedError

        reason = (
            "refs/heads/fix/issue-7:refs/heads/fix/issue-7 "
            "[remote rejected] (push declined due to repository rule "
            "violations)"
        )
        db = StateDB(tmp_path / "state.db")
        github = AsyncMock()
        fake_relay_publish(github)
        worktree = AsyncMock()
        worktree.push_run_branch.side_effect = PushRejectedError(
            f"push rejected by https://github.com/owner/repo.git: {reason}",
            reason=reason,
        )
        caplog.set_level(logging.ERROR, logger="ctrlrelay.pipelines.dev")
        try:
            result = await _publisher(tmp_path, github, worktree, db).publish(
                _done()
            )
        finally:
            db.close()
        assert not result.success
        assert result.outputs["publish_failed"] is True
        [rec] = [r for r in caplog.records if r.msg == "dev.publish.failed"]
        assert rec.push_rejection == reason
        assert rec.refused is False, "a remote refusal is not the relay's"
        # And the message still names the url and the reason.
        assert "repository rule violations" in (result.error or "")
        assert "github.com/owner/repo" in (result.error or "")

    @pytest.mark.asyncio
    async def test_any_other_publish_failure_leaves_the_field_empty(
        self, tmp_path: Path, caplog
    ) -> None:
        """The field is None for a failure that is not a rejection,
        rather than carrying whatever `.reason` some other exception
        type happens to define."""
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        github = AsyncMock()
        fake_relay_publish(github)
        worktree = AsyncMock()
        worktree.push_run_branch.side_effect = OSError("no route to host")
        caplog.set_level(logging.ERROR, logger="ctrlrelay.pipelines.dev")
        try:
            result = await _publisher(tmp_path, github, worktree, db).publish(
                _done()
            )
        finally:
            db.close()
        assert not result.success
        [rec] = [r for r in caplog.records if r.msg == "dev.publish.failed"]
        assert rec.push_rejection is None


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
                claims=None,
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
