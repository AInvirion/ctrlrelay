"""The third agent backend: `opencode` against a self-hosted model (#286).

What these tests are about, and what they deliberately are NOT about.

They assert the ADAPTER: that `agent.type: "opencode"` builds something,
that the something satisfies the `AgentAdapter` protocol, that a session
spawned through it turns a checkpoint file into a `SessionResult`, that a
forced timeout comes back as a result rather than a hang, and that a
`claude` deployment is untouched.

They assert nothing about whether a local model does good work. That
depends on the model, the adapter does not control it, and a test
asserting it would let a correct adapter be blocked by weak weights.

The subprocess is always a mock. `opencode` is not installed on any fleet
machine, and a test that needs it installed is a test that does not run.
"""

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    proc = AsyncMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


def _done_checkpoint(state_file: Path, session_id: str = "dev-x-y-1-abc") -> None:
    state_file.write_text(json.dumps({
        "version": "1",
        "status": "DONE",
        "session_id": session_id,
        "timestamp": "2026-11-02T12:00:00Z",
        "summary": "patched the module",
        "outputs": {"pr_title": "fix: a thing", "pr_body": "because"},
    }))


def _adapter(**kw):
    from ctrlrelay.core.dispatcher import OpenCodeDispatcher

    kw.setdefault("usage_store", None)
    kw.setdefault("opencode_binary", "opencode")
    return OpenCodeDispatcher(**kw)


class TestRegistration:
    """Done-when 1 and 2: the daemon starts, and the factory knows it."""

    def test_opencode_type_builds_an_adapter_instead_of_raising(self) -> None:
        """Fails with the change reverted: `make_agent_dispatcher` ends
        its NotImplementedError with "Supported today: 'claude'."."""
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import (
            OpenCodeDispatcher,
            make_agent_dispatcher,
        )

        cfg = AgentConfig(type="opencode")
        adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        assert isinstance(adapter, OpenCodeDispatcher)

    def test_spawn_session_signature_matches_the_protocol(self) -> None:
        """The protocol is not `runtime_checkable`, so `isinstance` would
        pass for anything. Compare the signatures instead: that is the
        claim "satisfies the AgentAdapter protocol" actually makes, and
        it catches a renamed or dropped keyword."""
        from ctrlrelay.core.dispatcher import AgentAdapter, OpenCodeDispatcher

        want = inspect.signature(AgentAdapter.spawn_session)
        got = inspect.signature(OpenCodeDispatcher.spawn_session)
        assert got == want

    def test_opencode_binary_comes_from_its_own_config_block(self) -> None:
        """`agent.binary` is claude's and most deployed configs set it to
        the literal "claude". Reading it for the opencode adapter would
        exec `claude` with opencode's argv — so the opencode block owns
        its own binary and timeout."""
        from ctrlrelay.core.config import AgentConfig, OpenCodeConfig
        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        cfg = AgentConfig(
            type="opencode",
            binary="claude",
            default_timeout_seconds=1800,
            opencode=OpenCodeConfig(
                binary="/opt/bin/opencode",
                model="local/qwen3-coder:30b",
                default_timeout_seconds=600,
            ),
        )
        adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        assert adapter.opencode_binary == "/opt/bin/opencode"
        assert adapter.model == "local/qwen3-coder:30b"
        assert adapter.default_timeout == 600

    def test_opencode_timeout_falls_back_to_the_shared_default(self) -> None:
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        cfg = AgentConfig(type="opencode", default_timeout_seconds=123)
        adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        assert adapter.default_timeout == 123


class TestClaudeIsUntouched:
    """Done-when 5."""

    def test_type_still_defaults_to_claude(self) -> None:
        from ctrlrelay.core.config import AgentConfig

        assert AgentConfig().type == "claude"

    def test_default_config_still_builds_the_claude_dispatcher(self) -> None:
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, make_agent_dispatcher

        adapter = make_agent_dispatcher(
            AgentConfig(), outcome_hook=None, usage_store=None
        )
        assert isinstance(adapter, ClaudeDispatcher)

    def test_a_still_unimplemented_type_still_raises(self) -> None:
        """Adding one backend must not turn the loud failure into a
        silent fallback for the others the docstring names."""
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        for unknown in ("codex", "hermes", "aider", "clade"):
            with pytest.raises(NotImplementedError) as excinfo:
                make_agent_dispatcher(
                    AgentConfig(type=unknown), outcome_hook=None, usage_store=None
                )
            assert unknown in str(excinfo.value)

    def test_the_hint_names_every_type_that_now_works(self) -> None:
        """The error told operators "Supported today: 'claude'." A second
        backend that is not in that sentence is a backend nobody finds."""
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        with pytest.raises(NotImplementedError) as excinfo:
            make_agent_dispatcher(
                AgentConfig(type="nope"), outcome_hook=None, usage_store=None
            )
        msg = str(excinfo.value)
        assert "'claude'" in msg
        assert "'opencode'" in msg


class TestArgv:
    @pytest.mark.asyncio
    async def test_runs_opencode_non_interactively_with_the_prompt_last(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
            await adapter.spawn_session(
                session_id="s1",
                prompt="do the work",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=False,
            )
        argv = list(ex.call_args.args)
        assert argv[0].endswith("opencode")
        assert argv[1] == "run"
        # `--format json` is what carries the session id; `--auto`
        # approves the tool calls an unattended run cannot answer.
        assert argv[argv.index("--format") + 1] == "json"
        assert "--auto" in argv
        # `opencode run [message..]` takes the message as a variadic
        # positional, so it has to be last or a flag swallows it.
        assert argv[-1] == "do the work"

    @pytest.mark.asyncio
    async def test_model_is_passed_only_when_configured(
        self, tmp_path: Path
    ) -> None:
        for model, expected in (("local/qwen3-coder:30b", True), (None, False)):
            adapter = _adapter(model=model)
            with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    github_access=False,
                )
            argv = list(ex.call_args.args)
            assert ("--model" in argv) is expected
            if expected:
                assert argv[argv.index("--model") + 1] == model

    @pytest.mark.asyncio
    async def test_resume_continues_the_agents_own_session(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
            await adapter.spawn_session(
                session_id="s1",
                prompt="and now the answer is yes",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                resume_session_id="ses_abc123",
                github_access=False,
            )
        argv = list(ex.call_args.args)
        assert argv[argv.index("--session") + 1] == "ses_abc123"

    @pytest.mark.asyncio
    async def test_runs_in_the_worktree(self, tmp_path: Path) -> None:
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
            await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=False,
            )
        assert ex.call_args.kwargs["cwd"] == tmp_path


class TestCheckpointIsTheResult:
    """Done-when 3: asserted by the presence and SHAPE of the artifact.

    Not by the absence of an error string — a run that wrote nothing and
    a run that wrote a DONE checkpoint both produce no error string.
    """

    @pytest.mark.asyncio
    async def test_the_state_file_becomes_the_session_result(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.checkpoint import CheckpointStatus

        state_file = tmp_path / "state.json"
        _done_checkpoint(state_file, session_id="dev-AInvirion-ctrlrelay-286-aa")
        adapter = _adapter()

        with patch("asyncio.create_subprocess_exec", return_value=_proc()):
            result = await adapter.spawn_session(
                session_id="dev-AInvirion-ctrlrelay-286-aa",
                prompt="p",
                working_dir=tmp_path,
                state_file=state_file,
                github_access=False,
            )

        assert result.state is not None
        assert result.state.status is CheckpointStatus.DONE
        assert result.state.session_id == "dev-AInvirion-ctrlrelay-286-aa"
        assert result.state.summary == "patched the module"
        assert result.state.outputs["pr_title"] == "fix: a thing"
        assert result.success is True
        assert result.exit_code == 0
        # Read once and removed, exactly as the claude adapter does, so a
        # resume cannot re-read the previous round's answer.
        assert not state_file.exists()

    @pytest.mark.asyncio
    async def test_a_blocked_checkpoint_comes_back_as_blocked(
        self, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "BLOCKED_NEEDS_INPUT",
            "session_id": "s1",
            "question": "which backend?",
        }))
        adapter = _adapter()

        with patch("asyncio.create_subprocess_exec", return_value=_proc()):
            result = await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=state_file,
                github_access=False,
            )

        assert result.blocked is True
        assert result.state is not None
        assert result.state.question == "which backend?"

    @pytest.mark.asyncio
    async def test_no_state_file_is_a_failure_even_on_a_zero_exit(
        self, tmp_path: Path
    ) -> None:
        """A weaker model exiting 0 having signalled nothing is the
        expected failure here, not an edge case."""
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()):
            result = await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=False,
            )
        assert result.state is None
        assert result.failed is True
        assert result.exit_code == 0


class TestTimeout:
    """Done-when 4."""

    @pytest.mark.asyncio
    async def test_a_forced_timeout_returns_a_result_and_kills_the_child(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(default_timeout=1)
        proc = AsyncMock()
        proc.communicate.side_effect = asyncio.TimeoutError()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                timeout=1,
                github_access=False,
            )

        assert result.exit_code == -1
        assert result.state is None
        assert "timed out" in result.stderr
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_cancel_kills_the_child_before_re_raising(
        self, tmp_path: Path
    ) -> None:
        """Daemon shutdown must not leave a local-model run chewing the
        worktree after the relay has gone."""
        adapter = _adapter(default_timeout=60)
        proc = AsyncMock()
        proc.returncode = None
        proc.communicate.side_effect = asyncio.CancelledError()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(asyncio.CancelledError):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    github_access=False,
                )
        proc.kill.assert_called_once()


class TestAgentSessionId:
    @pytest.mark.asyncio
    async def test_read_from_the_json_event_lines(self, tmp_path: Path) -> None:
        stdout = b"\n".join([
            b'{"type":"step_start","timestamp":1,"sessionID":"ses_7f3a"}',
            b'{"type":"text","timestamp":2,"sessionID":"ses_7f3a","text":"hi"}',
            b"",
        ])
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(stdout)):
            result = await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=False,
            )
        assert result.agent_session_id == "ses_7f3a"

    @pytest.mark.asyncio
    async def test_unreadable_output_yields_none_rather_than_a_guess(
        self, tmp_path: Path
    ) -> None:
        """The event schema has changed between opencode versions. A
        `None` here means "start fresh", which is recoverable; a wrong
        id would resume somebody else's session."""
        for stdout in (
            b"",
            b"opencode: command panicked\n",
            b"{not json at all}\n",
            b'{"type":"text","text":"no id here"}\n',
            b"\xff\xfe not utf-8 \n",
        ):
            adapter = _adapter()
            with patch("asyncio.create_subprocess_exec", return_value=_proc(stdout)):
                result = await adapter.spawn_session(
                    session_id="s1",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    github_access=False,
                )
            assert result.agent_session_id is None, stdout

    @pytest.mark.asyncio
    async def test_snake_case_and_nested_spellings_are_accepted(
        self, tmp_path: Path
    ) -> None:
        for line, want in (
            (b'{"session_id":"ses_1"}\n', "ses_1"),
            (b'{"sessionId":"ses_2"}\n', "ses_2"),
            (b'{"info":{"sessionID":"ses_3"}}\n', "ses_3"),
        ):
            adapter = _adapter()
            with patch("asyncio.create_subprocess_exec", return_value=_proc(line)):
                result = await adapter.spawn_session(
                    session_id="s1",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    github_access=False,
                )
            assert result.agent_session_id == want


class TestEnvironment:
    @pytest.mark.asyncio
    async def test_the_checkpoint_contract_is_in_the_childs_environment(
        self, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state.json"
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
            await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=state_file,
                github_access=False,
            )
        env = ex.call_args.kwargs["env"]
        assert env["CTRLRELAY_SESSION_ID"] == "s1"
        assert env["CTRLRELAY_STATE_FILE"] == str(state_file)

    @pytest.mark.asyncio
    async def test_github_credentials_are_stripped_without_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#219 is not claude-specific. A new backend that inherited the
        operator's token would be a credential regression hiding behind
        a feature."""
        monkeypatch.setenv("GH_TOKEN", "ghp_secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_other")
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
            await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=False,
            )
        env = ex.call_args.kwargs["env"]
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env

    @pytest.mark.asyncio
    async def test_with_access_the_environment_is_handed_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_secret")
        adapter = _adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as ex:
            await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=True,
            )
        assert ex.call_args.kwargs["env"]["GH_TOKEN"] == "ghp_secret"


class TestSandboxIsRefusedNotIgnored:
    def test_bwrap_is_refused_at_construction(self) -> None:
        """`agent.sandbox: "bwrap"` binds `~/.claude/*` writable and
        copies `~/.claude.json`: the confinement is written for one CLI.
        Accepting the setting and running unconfined is the silent
        opt-out an operator cannot see. Refusing names it at startup."""
        with pytest.raises(ValueError) as excinfo:
            _adapter(sandbox="bwrap")
        msg = str(excinfo.value)
        assert "bwrap" in msg
        assert "opencode" in msg

    def test_the_factory_refuses_the_same_combination(self) -> None:
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        cfg = AgentConfig(type="opencode", sandbox="bwrap")
        with pytest.raises(ValueError):
            make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)

    def test_sandbox_none_is_fine(self) -> None:
        assert _adapter(sandbox="none") is not None


class TestUsage:
    @pytest.mark.asyncio
    async def test_a_run_does_not_touch_the_usage_store(
        self, tmp_path: Path
    ) -> None:
        """A self-hosted model has no subscription window. Marking the
        store unknown after every opencode run would erase the reading
        the claude backend holds and un-pause a machine that is at its
        limit — so this backend writes nothing at all."""
        from ctrlrelay.core.usage import UsageReading, UsageStore, Window

        store = UsageStore(tmp_path / "usage.json")
        store.write(UsageReading(
            status="allowed",
            windows={"five_hour": Window(used=0.42, resets_at=1762000000)},
            read_at=1761999000,
        ))
        before = (tmp_path / "usage.json").read_text()

        adapter = _adapter(usage_store=store)
        with patch("asyncio.create_subprocess_exec", return_value=_proc()):
            await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                github_access=False,
            )

        assert (tmp_path / "usage.json").read_text() == before
        reading = store.read()
        assert reading is not None
        assert reading.windows["five_hour"].used == 0.42


class TestDispatchHealth:
    @pytest.mark.asyncio
    async def test_every_spawn_is_reported_to_the_outcome_hook(
        self, tmp_path: Path
    ) -> None:
        """#204 covers every spawn by construction. A backend that
        skipped the hook would be silently exempt from the alert."""
        from ctrlrelay.core.dispatcher import DispatchOutcome

        seen: list[DispatchOutcome] = []

        async def hook(outcome: DispatchOutcome) -> None:
            seen.append(outcome)

        adapter = _adapter(outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_proc(b"", b"boom", returncode=2),
        ):
            await adapter.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                repo="AInvirion/ctrlrelay",
                issue_number=286,
                github_access=False,
            )

        assert len(seen) == 1
        assert seen[0].exit_code == 2
        assert seen[0].ok is False
        assert seen[0].repo == "AInvirion/ctrlrelay"
        assert seen[0].issue_number == 286

    @pytest.mark.asyncio
    async def test_a_binary_that_cannot_be_exec_d_is_reported_with_no_exit_code(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.dispatcher import DispatchOutcome

        seen: list[DispatchOutcome] = []

        async def hook(outcome: DispatchOutcome) -> None:
            seen.append(outcome)

        adapter = _adapter(outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("no opencode"),
        ):
            with pytest.raises(FileNotFoundError):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    github_access=False,
                )

        assert len(seen) == 1
        # NOT 0: `ok` is `exit_code == 0`, and a dispatch whose status
        # nobody read must not clear the alert.
        assert seen[0].exit_code is None
        assert seen[0].ok is False


class TestSharedMachineryIsShared:
    def test_both_adapters_use_one_copy_of_the_run_loop(self) -> None:
        """The run loop carries #204's reporting on every exit path, the
        guarded `communicate()` and the replacing decode. Two copies
        means the next backend's dispatch failures stop being counted,
        which is the defect #204 was filed for."""
        from ctrlrelay.core.dispatcher import (
            ClaudeDispatcher,
            CliAgentAdapter,
            OpenCodeDispatcher,
        )

        assert issubclass(ClaudeDispatcher, CliAgentAdapter)
        assert issubclass(OpenCodeDispatcher, CliAgentAdapter)
        assert ClaudeDispatcher._run_session is CliAgentAdapter._run_session
        assert OpenCodeDispatcher._run_session is CliAgentAdapter._run_session
        assert (
            ClaudeDispatcher._report_outcome is CliAgentAdapter._report_outcome
        )
        assert (
            OpenCodeDispatcher._report_outcome is CliAgentAdapter._report_outcome
        )

    def test_each_adapter_names_its_own_backend(self) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, OpenCodeDispatcher

        assert ClaudeDispatcher.agent_type == "claude"
        assert OpenCodeDispatcher.agent_type == "opencode"

    def test_the_binary_is_resolved_like_every_other_tool(self) -> None:
        """#197: under systemd the unit's PATH is minimal, so a bare
        name has to be searched in the user-install directories too."""
        from ctrlrelay.core.dispatcher import OpenCodeDispatcher

        with patch(
            "ctrlrelay.core.dispatcher.resolve_binary",
            return_value="/home/x/.local/bin/opencode",
        ) as resolve:
            adapter = OpenCodeDispatcher(usage_store=None)
        assert adapter.opencode_binary == "/home/x/.local/bin/opencode"
        assert resolve.call_args.args[0] == "opencode"

    def test_an_explicit_path_is_not_second_guessed(self) -> None:
        from ctrlrelay.core.dispatcher import OpenCodeDispatcher

        with patch("ctrlrelay.core.dispatcher.resolve_binary") as resolve:
            adapter = OpenCodeDispatcher(
                usage_store=None, opencode_binary="/opt/oc/opencode"
            )
        resolve.assert_not_called()
        assert adapter.opencode_binary == "/opt/oc/opencode"


class TestThePipelineConsumesIt:
    """Done-when 3, end to end rather than at the adapter's edge.

    The checkpoint is NOT written by ctrlrelay. The prompt tells the
    agent to run a shell command that writes it, and the agent learns
    where from ``CTRLRELAY_STATE_FILE`` in its environment. So the fake
    child here does exactly that — reads the variable, writes the file —
    and nothing else about the dispatcher is stubbed. What is asserted
    is that the dev pipeline reaches a successful `SessionResult`
    carrying the agent's own outputs, through a real
    `OpenCodeDispatcher`.

    Mocking the dispatcher, as the older integration test does, cannot
    answer this: it asserts the pipeline given a good `SessionResult`,
    not that this adapter produces one.
    """

    @pytest.mark.asyncio
    async def test_a_dev_run_on_the_opencode_backend_reaches_done(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import AsyncMock as _AsyncMock

        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue
        from tests._publish_fakes import fake_relay_publish

        state_db = StateDB(tmp_path / "state.db")
        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)
        (bare_path / "HEAD").write_text("ref: refs/heads/main\n")

        github = _AsyncMock(spec=GitHubCLI)
        fake_relay_publish(github)
        github.get_issue.return_value = {
            "number": 123,
            "title": "Fix the login bug",
            "body": "Users cannot log in when...",
        }
        github.get_pr_checks.return_value = [
            {"name": "ci", "status": "completed", "conclusion": "success"},
        ]
        github.get_pr_state.return_value = {
            "number": 42,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        verifier = _AsyncMock()
        verifier.verify.return_value = VerificationResult(ready=True)

        spawned: list[list[str]] = []

        def fake_exec(*argv, **kwargs):
            """An agent that signals the way the prompt tells it to.

            The pipeline also shells out to git around the session, and
            this patch catches those too — hence the argv check rather
            than assuming the first spawn is the agent.
            """
            argv = list(argv)
            spawned.append(argv)
            if not argv or "opencode" not in argv[0]:
                return _proc()
            env = kwargs["env"]
            target = Path(env["CTRLRELAY_STATE_FILE"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": env["CTRLRELAY_SESSION_ID"],
                "timestamp": "2026-11-02T12:00:00Z",
                "summary": "PR #42 opened for issue #123",
                "outputs": {
                    "pr_url": "https://github.com/owner/repo/pull/42",
                    "pr_number": 42,
                },
            }))
            return _proc(
                b'{"type":"step_finish","sessionID":"ses_dev1"}\n'
            )

        dispatcher = _adapter(model="local/qwen3-coder:30b")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree,
                "create_worktree_with_new_branch",
                new_callable=AsyncMock,
            ) as create,
            patch.object(worktree, "remove_worktree", new_callable=AsyncMock),
            patch.object(worktree, "symlink_context"),
            patch.object(
                worktree,
                "push_run_branch",
                new_callable=AsyncMock,
                return_value="c0ffee",
            ),
            patch.object(worktree, "remove_context_symlink"),
        ):
            worktree_path = tmp_path / "worktrees" / "wt"
            worktree_path.mkdir(parents=True)
            create.return_value = (worktree_path, True)

            result = await run_dev_issue(
                claims=None,
                repo="owner/repo",
                issue_number=123,
                branch_template="fix/issue-{n}",
                dispatcher=dispatcher,
                github=github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
                pr_verifier=verifier,
            )

        assert result.success
        assert result.outputs["pr_number"] == 42
        # It really went through opencode, not some fallback.
        agent_runs = [a for a in spawned if a and "opencode" in a[0]]
        assert len(agent_runs) == 1
        assert agent_runs[0][1] == "run"
        assert "--model" in agent_runs[0]
        assert agent_runs[0][-1].startswith("You are working on issue #123")
        rows = state_db.execute("SELECT * FROM sessions").fetchall()
        assert [r["status"] for r in rows] == ["done"]
        state_db.close()
