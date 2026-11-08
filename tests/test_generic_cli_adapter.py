"""Isolated contract tests for the generic coding-CLI adapter.

No test invokes a real coding agent. Construction is deliberately independent
of the registration factory so this module can be tested on its own.
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


def _adapter(**kw):
    from ctrlrelay.core.generic_cli import GenericCliDispatcher

    kw.setdefault("agent_type", "example")
    kw.setdefault("binary", "/opt/example-agent")
    kw.setdefault("usage_store", None)
    return GenericCliDispatcher(**kw)


async def _spawn(adapter, tmp_path: Path, *, proc=None, **overrides):
    options = {
        "session_id": "s1",
        "prompt": "do the work",
        "working_dir": tmp_path,
        "state_file": tmp_path / "state.json",
        "github_access": False,
    }
    options.update(overrides)
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=proc if proc is not None else _proc(),
    ) as ex:
        result = await adapter.spawn_session(**options)
    return result, ex


def _done_checkpoint(state_file: Path, session_id: str = "s1") -> None:
    state_file.write_text(json.dumps({
        "version": "1",
        "status": "DONE",
        "session_id": session_id,
        "timestamp": "2026-11-07T12:00:00Z",
        "summary": "generic adapter completed the run",
        "outputs": {"answer": "done"},
    }))


class TestPluginContract:
    def test_spawn_session_signature_exactly_matches_the_protocol(self) -> None:
        from ctrlrelay.core.dispatcher import AgentAdapter
        from ctrlrelay.core.generic_cli import GenericCliDispatcher

        assert inspect.signature(GenericCliDispatcher.spawn_session) == inspect.signature(
            AgentAdapter.spawn_session
        )

    def test_identity_and_capabilities_come_from_the_instance(self) -> None:
        adapter = _adapter(
            agent_type="other",
            plugin_version="2.4",
            model="model-x",
            provider="provider-y",
        )

        assert adapter.agent_type == "other"
        assert adapter.plugin_id == "generic:other"
        assert adapter.agent_binary == "/opt/example-agent"
        assert adapter.agent_identity.plugin_id == "generic:other"
        assert adapter.agent_identity.plugin_version == "2.4"
        assert adapter.agent_identity.model == "model-x"
        assert adapter.agent_identity.provider == "provider-y"
        assert adapter.capabilities.resume is False
        assert adapter.capabilities.structured_events is False
        assert adapter.capabilities.usage is False
        assert adapter.capabilities.sandbox is False

    def test_resume_capability_requires_args_and_a_session_path(self) -> None:
        adapter = _adapter(
            resume_args=("resume", "{resume_session_id}"),
            session_id_path="info.session.id",
        )
        assert adapter.capabilities.resume is True

    def test_bare_binary_uses_the_shared_resolver(self) -> None:
        with patch(
            "ctrlrelay.core.generic_cli.resolve_binary",
            return_value="/home/user/.local/bin/example",
        ) as resolve:
            adapter = _adapter(binary="example")

        assert adapter.binary == "/home/user/.local/bin/example"
        resolve.assert_called_once()
        assert resolve.call_args.args[0] == "example"

    def test_relative_binary_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            _adapter(binary="relative/bin/example")

    def test_manifest_fingerprint_covers_invocation_identity(self) -> None:
        first = _adapter(args=("run", "{prompt}"))
        same = _adapter(args=("run", "{prompt}"))
        different = _adapter(args=("other", "{prompt}"))
        assert first.agent_identity.fingerprint
        assert first.agent_identity.fingerprint == same.agent_identity.fingerprint
        assert first.agent_identity.fingerprint != different.agent_identity.fingerprint


class TestConstructionValidation:
    @pytest.mark.parametrize("binary", ["", "   "])
    def test_empty_binary_is_refused(self, binary: str) -> None:
        with pytest.raises(ValueError, match="binary"):
            _adapter(binary=binary)

    def test_unknown_prompt_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="prompt_mode"):
            _adapter(prompt_mode="file")

    def test_sandbox_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _adapter(sandbox="bwrap")
        message = str(excinfo.value)
        assert "bwrap" in message
        assert "example" in message
        assert "none" in message

    def test_resume_args_require_a_session_id_path(self) -> None:
        with pytest.raises(ValueError, match="session_id_path"):
            _adapter(resume_args=("resume", "{resume_session_id}"))

    @pytest.mark.parametrize("path", ["", "   "])
    def test_empty_session_id_path_is_refused(self, path: str) -> None:
        with pytest.raises(ValueError, match="session_id_path"):
            _adapter(session_id_path=path)

    @pytest.mark.parametrize(
        "args",
        [("{resume_session_id}",), ("--resume={resume_session_id}",)],
    )
    def test_resume_placeholder_is_forbidden_in_base_args(self, args: tuple[str, ...]) -> None:
        with pytest.raises(ValueError, match="resume_args"):
            _adapter(args=args)

    def test_resume_args_must_name_the_native_session(self) -> None:
        with pytest.raises(ValueError, match="resume_session_id"):
            _adapter(
                resume_args=("resume", "--last"),
                session_id_path="session_id",
            )

    @pytest.mark.parametrize("use_resume_args", [False, True])
    def test_stdin_mode_forbids_a_prompt_placeholder(self, use_resume_args: bool) -> None:
        options = {
            "args": ("run",) if use_resume_args else ("run", "{prompt}"),
            "prompt_mode": "stdin",
        }
        if use_resume_args:
            options.update({
                "resume_args": ("resume", "{resume_session_id}", "{prompt}"),
                "session_id_path": "session_id",
            })
        with pytest.raises(ValueError, match="prompt"):
            _adapter(**options)
    @pytest.mark.parametrize(
        "args",
        [("run", "{promt}"), ("run", "--id={session}"), ("run", "{}")],
    )
    def test_unknown_placeholders_are_refused(self, args: tuple[str, ...]) -> None:
        with pytest.raises(ValueError, match="placeholder"):
            _adapter(args=args)


class TestArgvAndPromptDelivery:
    @pytest.mark.asyncio
    async def test_whole_argument_placeholders_are_substituted(
        self, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state with spaces.json"
        adapter = _adapter(
            args=(
                "run",
                "{prompt}",
                "{session_id}",
                "{state_file}",
                "{model}",
                "{provider}",
            ),
            model="model-x",
            provider="provider-y",
        )
        prompt = "preserve {model}, {provider}, and {resume_session_id} literally"

        _, ex = await _spawn(
            adapter,
            tmp_path,
            prompt=prompt,
            session_id="session-123",
            state_file=state_file,
        )

        assert list(ex.call_args.args) == [
            "/opt/example-agent",
            "run",
            prompt,
            "session-123",
            str(state_file),
            "model-x",
            "provider-y",
        ]
        assert ex.call_args.kwargs["cwd"] == tmp_path
        assert "shell" not in ex.call_args.kwargs

    @pytest.mark.asyncio
    async def test_embedded_placeholders_do_not_require_a_shell(
        self, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state.json"
        adapter = _adapter(
            args=(
                "run",
                "--prompt={prompt}",
                "--context={session_id}:{state_file}",
                "--model={model}",
                "--provider={provider}",
            ),
            model="m",
            provider="p",
        )

        _, ex = await _spawn(adapter, tmp_path, prompt="two words", state_file=state_file)

        assert list(ex.call_args.args) == [
            "/opt/example-agent",
            "run",
            "--prompt=two words",
            f"--context=s1:{state_file}",
            "--model=m",
            "--provider=p",
        ]

    @pytest.mark.asyncio
    async def test_arg_mode_appends_prompt_only_when_no_placeholder_exists(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(args=("run", "--json"))

        _, ex = await _spawn(adapter, tmp_path, prompt="one argument\nwith a newline")

        argv = list(ex.call_args.args)
        assert argv[-1] == "one argument\nwith a newline"
        assert argv.count("one argument\nwith a newline") == 1

    @pytest.mark.asyncio
    async def test_stdin_mode_writes_prompt_without_adding_it_to_argv(
        self, tmp_path: Path
    ) -> None:
        proc = _proc()
        adapter = _adapter(args=("run", "--json"), prompt_mode="stdin")
        prompt = "first line\nsecond line"

        _, ex = await _spawn(adapter, tmp_path, proc=proc, prompt=prompt)

        argv = list(ex.call_args.args)
        assert argv == ["/opt/example-agent", "run", "--json"]
        assert ex.call_args.kwargs["stdin"] is asyncio.subprocess.PIPE
        proc.communicate.assert_awaited_once_with(input=prompt.encode())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["model", "provider"])
    async def test_missing_model_or_provider_placeholder_fails_before_spawn(
        self, tmp_path: Path, missing: str
    ) -> None:
        adapter = _adapter(args=("run", f"--value={{{missing}}}"))
        with patch("asyncio.create_subprocess_exec") as ex:
            with pytest.raises(ValueError, match=missing):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    github_access=False,
                )
        ex.assert_not_called()
        assert adapter.active == 0
        assert adapter.started == 0


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_uses_only_resume_args_and_recovers_native_id(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(
            args=("fresh", "{prompt}"),
            resume_args=("resume", "--id={resume_session_id}", "{prompt}"),
            session_id_path="info.session.id",
        )
        proc = _proc(b'{"info":{"session":{"id":"ses_next"}}}\n')

        result, ex = await _spawn(
            adapter,
            tmp_path,
            proc=proc,
            prompt="continue the work",
            resume_session_id="ses_previous",
        )

        assert list(ex.call_args.args) == [
            "/opt/example-agent",
            "resume",
            "--id=ses_previous",
            "continue the work",
        ]
        assert result.agent_session_id == "ses_next"

    @pytest.mark.asyncio
    async def test_resume_without_resume_args_never_starts_a_fresh_session(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(args=("fresh", "{prompt}"))
        with patch("asyncio.create_subprocess_exec") as ex:
            with pytest.raises(ValueError, match="refusing to start a fresh session"):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="continue",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    resume_session_id="ses_previous",
                    github_access=False,
                )
        ex.assert_not_called()
        assert adapter.active == 0
        assert adapter.started == 0
    @pytest.mark.asyncio
    async def test_logical_resume_without_a_native_id_fails_closed(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(
            args=("fresh", "{prompt}"),
            resume_args=("resume", "{resume_session_id}", "{prompt}"),
            session_id_path="session_id",
        )
        with patch("asyncio.create_subprocess_exec") as ex:
            with pytest.raises(ValueError, match="native session id"):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="continue",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    resume_requested=True,
                    github_access=False,
                )
        ex.assert_not_called()

    @pytest.mark.asyncio
    async def test_logical_resume_requires_resume_args_even_without_native_id(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(args=("fresh", "{prompt}"))
        with patch("asyncio.create_subprocess_exec") as ex:
            with pytest.raises(ValueError, match="cannot resume"):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="continue",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    resume_requested=True,
                    github_access=False,
                )
        ex.assert_not_called()

    @pytest.mark.asyncio
    async def test_option_like_native_session_id_is_refused_before_spawn(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(
            resume_args=("resume", "{resume_session_id}"),
            session_id_path="session_id",
        )
        with patch("asyncio.create_subprocess_exec") as ex:
            with pytest.raises(ValueError, match="native session id"):
                await adapter.spawn_session(
                    session_id="s1",
                    prompt="continue",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    resume_session_id="--config=/tmp/evil",
                    github_access=False,
                )
        ex.assert_not_called()


class TestSessionResult:
    @pytest.mark.asyncio
    async def test_checkpoint_is_converted_and_removed(self, tmp_path: Path) -> None:
        from ctrlrelay.core.checkpoint import CheckpointStatus

        state_file = tmp_path / "state.json"
        _done_checkpoint(state_file)
        adapter = _adapter()

        result, _ = await _spawn(adapter, tmp_path, state_file=state_file)

        assert result.exit_code == 0
        assert result.success is True
        assert result.state is not None
        assert result.state.status is CheckpointStatus.DONE
        assert result.state.summary == "generic adapter completed the run"
        assert result.state.outputs == {"answer": "done"}
        assert not state_file.exists()
        assert adapter.active == 0
        assert adapter.started == 1

    @pytest.mark.asyncio
    async def test_timeout_kills_child_and_cleans_up_counters(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(default_timeout=1)
        proc = AsyncMock()
        proc.communicate.side_effect = asyncio.TimeoutError()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        result, _ = await _spawn(adapter, tmp_path, proc=proc, timeout=1)

        assert result.exit_code == -1
        assert result.timed_out is True
        assert result.state is None
        assert "timed out" in result.stderr
        proc.kill.assert_called_once()
        assert adapter.active == 0
        assert adapter.started == 1


class TestSessionIdPath:
    def test_jsonl_uses_the_first_valid_configured_path(self) -> None:
        adapter = _adapter(session_id_path="info.session.id")
        stdout = "\n".join([
            "not json",
            "{broken json}",
            '{"info":{"session":{"id":""}}}',
            '{"info":{"session":{"id":17}}}',
            '{"info":{"session":{"id":"   "}}}',
            '{"info":{"session":{}}}',
            '{"info":{"session":{"id":"ses_right"}}}',
            '{"info":{"session":{"id":"ses_later"}}}',
        ])
        assert adapter._parse_agent_session_id(stdout) == "ses_right"

    def test_root_path_and_whole_pretty_printed_json_object_work(self) -> None:
        adapter = _adapter(session_id_path="session_id")
        stdout = '{\n  "session_id": "ses_pretty",\n  "done": true\n}'
        assert adapter._parse_agent_session_id(stdout) == "ses_pretty"

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "plain text",
            "{not json}",
            '["session_id", "ses_array"]',
            '{"session_id":null}',
            '{"session_id":false}',
            '{"session_id":"--config=/tmp/evil"}',
            '{"session_id":"contains whitespace"}',
            '{"session_id":"../../other"}',
            '{"other":"ses_wrong"}',
        ],
    )
    def test_invalid_or_missing_values_return_none(self, stdout: str) -> None:
        adapter = _adapter(session_id_path="session_id")
        assert adapter._parse_agent_session_id(stdout) is None

    def test_no_configured_path_never_guesses_a_session_id(self) -> None:
        adapter = _adapter()
        assert adapter._parse_agent_session_id('{"session_id":"ses_guess"}') is None


class TestEnvironmentAndIsolation:
    @pytest.mark.asyncio
    async def test_credentials_are_stripped_and_private_root_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "secret")
        monkeypatch.setenv("GITHUB_TOKEN", "other-secret")
        state_file = tmp_path / "state.json"
        adapter = _adapter(extra_env={"EXAMPLE_SETTING": "configured"})

        _, ex = await _spawn(adapter, tmp_path, state_file=state_file)

        env = ex.call_args.kwargs["env"]
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert env["EXAMPLE_SETTING"] == "configured"
        assert env["CTRLRELAY_SESSION_ID"] == "s1"
        assert env["CTRLRELAY_STATE_FILE"] == str(state_file)
        private_root = Path(env["GH_CONFIG_DIR"]).parent
        assert not private_root.exists()

    @pytest.mark.asyncio
    async def test_github_access_preserves_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "secret")
        adapter = _adapter()

        _, ex = await _spawn(adapter, tmp_path, github_access=True)

        assert ex.call_args.kwargs["env"]["GH_TOKEN"] == "secret"

    @pytest.mark.asyncio
    async def test_credential_event_matches_builtin_adapters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "secret")
        adapter = _adapter()
        with patch("ctrlrelay.core.generic_cli.log_event") as log:
            await _spawn(
                adapter,
                tmp_path,
                repo="owner/repo",
                issue_number=379,
            )

        calls = [
            call for call in log.call_args_list
            if call.args[1] == "dispatcher.session.credentials"
        ]
        assert len(calls) == 1
        fields = calls[0].kwargs
        assert fields["session_id"] == "s1"
        assert fields["repo"] == "owner/repo"
        assert fields["issue_number"] == 379
        assert fields["github_access"] is False
        assert "GH_TOKEN" in fields["stripped_env"]
        assert fields["sandbox"] == "none"


class TestCopilotManifest:
    """The documented Copilot manifest against Copilot CLI's real output.

    The JSONL below is trimmed from a Copilot CLI 1.0.107 run: many events
    carry an ``id``, only the final ``result`` event carries ``sessionId``.
    """

    ARGS = (
        "-p", "{prompt}", "--allow-all-tools", "--disable-builtin-mcps",
        "--output-format", "json",
    )

    def _copilot(self):
        return _adapter(
            agent_type="copilot",
            binary="/opt/copilot",
            args=self.ARGS,
            resume_args=(*self.ARGS, "--resume", "{resume_session_id}"),
            session_id_path="sessionId",
            instructions_files=(".github/copilot-instructions.md", "AGENTS.md"),
        )

    def test_the_session_id_comes_from_the_result_event(self) -> None:
        stdout = "\n".join([
            '{"type":"session.mcp_servers_loaded","data":{},"ephemeral":true,'
            '"id":"59ffeac8-d98f-4a25-adee-897873d7cde0"}',
            '{"type":"assistant.message","data":{"content":"OK"},'
            '"id":"0eddbbb8-b1f6-45bd-a578-7ab4fa608fd1"}',
            '{"type":"result","timestamp":"2026-11-07T18:44:59.436Z",'
            '"sessionId":"8e34dff3-65de-4e73-bd7b-5b56f3b8c8c7","exitCode":0,'
            '"usage":{"premiumRequests":1}}',
        ])
        assert self._copilot()._parse_agent_session_id(stdout) == (
            "8e34dff3-65de-4e73-bd7b-5b56f3b8c8c7"
        )

    def test_fresh_and_resume_argv_keep_the_github_mcp_off(self, tmp_path) -> None:
        adapter = self._copilot()
        fresh = adapter._build_argv(
            prompt="do it", session_id="s1", state_file=tmp_path / "s.json",
            resume_session_id=None,
        )
        resumed = adapter._build_argv(
            prompt="answer", session_id="s1", state_file=tmp_path / "s.json",
            resume_session_id="8e34dff3-65de-4e73-bd7b-5b56f3b8c8c7",
        )
        assert fresh[:3] == ["/opt/copilot", "-p", "do it"]
        assert resumed[-2:] == ["--resume", "8e34dff3-65de-4e73-bd7b-5b56f3b8c8c7"]
        for argv in (fresh, resumed):
            assert "--disable-builtin-mcps" in argv
        assert adapter.capabilities.resume is True


class TestTheAgentNeverInheritsTheDaemonsStdin:
    """A CLI that appends piped stdin to its prompt waits for end-of-file.

    ``codex exec`` does: with stdin an open pipe it never starts, and the
    run sits until the timeout. Every spawn without a stdin prompt gets
    /dev/null instead of the daemon's stdin.
    """

    @pytest.mark.asyncio
    async def test_arg_mode_spawns_with_devnull(self, tmp_path: Path) -> None:
        adapter = _adapter(args=("run", "{prompt}"))
        _, ex = await _spawn(adapter, tmp_path)
        assert ex.call_args.kwargs["stdin"] is asyncio.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_builtins_spawn_with_devnull(self, tmp_path: Path) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, OpenCodeDispatcher

        for adapter in (
            ClaudeDispatcher(claude_binary="/bin/echo", usage_store=None),
            OpenCodeDispatcher(opencode_binary="/bin/echo", usage_store=None),
        ):
            with patch(
                "asyncio.create_subprocess_exec", return_value=_proc(),
            ) as ex:
                await adapter.spawn_session(
                    session_id="s1", prompt="p", working_dir=tmp_path,
                    state_file=tmp_path / "state.json", github_access=False,
                )
            assert ex.call_args.kwargs["stdin"] is asyncio.subprocess.DEVNULL, adapter

    @pytest.mark.asyncio
    async def test_a_cli_that_reads_stdin_finishes_under_an_open_pipe(
        self, tmp_path: Path,
    ) -> None:
        """End to end: the parent's stdin is a pipe that never closes."""
        import os

        script = tmp_path / "reads-stdin"
        script.write_text(
            "#!/bin/sh\n"
            # The shell's own read, not `cat`: one process the timeout can
            # kill, so a regression fails in seconds instead of hanging.
            "while read -r _; do :; done\n"
            "mkdir -p \"$(dirname \"$CTRLRELAY_STATE_FILE\")\"\n"
            "printf '{\"version\":\"1\",\"status\":\"DONE\",\"session_id\":\"s1\","
            "\"timestamp\":\"2026-01-01T00:00:00Z\"}' > \"$CTRLRELAY_STATE_FILE\"\n"
        )
        script.chmod(0o755)
        adapter = _adapter(binary=str(script), args=("{prompt}",))
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            result = await adapter.spawn_session(
                session_id="s1", prompt="p", working_dir=tmp_path,
                state_file=tmp_path / ".ctrlrelay" / "state.json",
                timeout=5, github_access=False,
            )
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        assert not result.timed_out
        assert result.state is not None and result.state.status.value == "DONE"

