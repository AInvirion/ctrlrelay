"""Plugin identity, assignment selection, and manifest registration (#379)."""

import pytest

from ctrlrelay.core.agent_plugins import (
    AgentIdentity,
    AgentIdentityConflictError,
    AgentSelectionError,
)
from ctrlrelay.core.config import AgentConfig, Config
from ctrlrelay.core.dispatcher import (
    ClaudeDispatcher,
    OpenCodeDispatcher,
    RepoAgentRouter,
    make_agent_dispatcher,
    make_repo_agent_dispatcher,
    select_agent,
)
from ctrlrelay.core.generic_cli import GenericCliDispatcher


class _FakeAdapter:
    def __init__(self, agent_type: str, *, model: str | None = None) -> None:
        self.agent_type = agent_type
        self.agent_identity = AgentIdentity(
            agent_type=agent_type,
            plugin_id=agent_type,
            model=model,
        )
        self.active = 0
        self.started = 0


def _router(*, assignable=()) -> RepoAgentRouter:
    claude = _FakeAdapter("claude")
    opencode = _FakeAdapter("opencode")
    return RepoAgentRouter(
        default=claude,
        by_repo={"owner/repo": opencode},
        by_type={"claude": claude, "opencode": opencode},
        assignable_types=frozenset(assignable),
    )


class TestAssignmentSelection:
    def test_no_request_preserves_the_repo_default(self) -> None:
        selected = select_agent(_router(), repo="owner/repo")
        assert selected.identity.agent_type == "opencode"

    def test_an_allowlisted_assignment_selects_that_plugin(self) -> None:
        selected = select_agent(
            _router(assignable={"claude"}),
            repo="owner/repo",
            requested_type="claude",
        )
        assert selected.identity.agent_type == "claude"

    def test_an_enrolled_but_unallowlisted_override_is_refused(self) -> None:
        with pytest.raises(AgentSelectionError, match="assignable_types"):
            select_agent(
                _router(), repo="owner/repo", requested_type="claude",
            )

    def test_an_unknown_assignment_is_refused(self) -> None:
        with pytest.raises(AgentSelectionError, match="not enrolled"):
            select_agent(_router(), repo="owner/repo", requested_type="aider")

    def test_explicitly_requesting_the_default_still_requires_allowlisting(self) -> None:
        with pytest.raises(AgentSelectionError, match="assignable_types"):
            select_agent(
                _router(), repo="owner/repo", requested_type="opencode",
            )

    def test_an_allowlisted_default_can_be_explicitly_selected(self) -> None:
        selected = select_agent(
            _router(assignable={"opencode"}),
            repo="owner/repo",
            requested_type="opencode",
        )
        assert selected.identity.agent_type == "opencode"

    def test_a_pin_wins_over_a_changed_repo_default(self) -> None:
        pinned = AgentIdentity(agent_type="claude", plugin_id="claude")
        selected = select_agent(_router(), repo="owner/repo", pinned=pinned)
        assert selected.identity.agent_type == "claude"

    def test_a_conflicting_request_and_pin_are_refused(self) -> None:
        pinned = AgentIdentity(agent_type="claude", plugin_id="claude")
        with pytest.raises(AgentIdentityConflictError, match="conflicts"):
            select_agent(
                _router(assignable={"opencode"}),
                repo="owner/repo",
                requested_type="opencode",
                pinned=pinned,
            )

    def test_a_model_change_cannot_rebind_a_pinned_session(self) -> None:
        router = RepoAgentRouter(
            default=_FakeAdapter("example", model="new-model"),
            by_type={"example": _FakeAdapter("example", model="new-model")},
        )
        pinned = AgentIdentity(
            agent_type="example", plugin_id="example", model="old-model",
        )
        with pytest.raises(AgentIdentityConflictError, match="model"):
            select_agent(router, repo="owner/repo", pinned=pinned)

    def test_a_bare_adapter_cannot_be_rebound(self) -> None:
        with pytest.raises(AgentSelectionError, match="only 'claude'"):
            select_agent(
                _FakeAdapter("claude"),
                repo="owner/repo",
                requested_type="opencode",
            )
    def test_a_bare_adapter_rejects_explicit_assignment_even_of_its_default(self) -> None:
        with pytest.raises(AgentSelectionError, match="assignable_types"):
            select_agent(
                _FakeAdapter("claude"),
                repo="owner/repo",
                requested_type="claude",
            )
    def test_builtin_binary_changes_change_the_identity_fingerprint(self) -> None:
        claude_a = ClaudeDispatcher(claude_binary="/opt/a/claude", usage_store=None)
        claude_b = ClaudeDispatcher(claude_binary="/opt/b/claude", usage_store=None)
        opencode_a = OpenCodeDispatcher(opencode_binary="/opt/a/opencode", usage_store=None)
        opencode_b = OpenCodeDispatcher(opencode_binary="/opt/b/opencode", usage_store=None)
        assert claude_a.agent_identity.fingerprint != claude_b.agent_identity.fingerprint
        assert opencode_a.agent_identity.fingerprint != opencode_b.agent_identity.fingerprint

    def test_a_builtin_self_update_keeps_the_same_pin(self, tmp_path) -> None:
        binary = tmp_path / "claude"
        binary.write_bytes(b"first build")
        first = ClaudeDispatcher(claude_binary=str(binary), usage_store=None)
        fingerprint = first.agent_identity.fingerprint

        binary.write_bytes(b"second build, different bytes")
        second = ClaudeDispatcher(claude_binary=str(binary), usage_store=None)

        assert second.agent_identity.fingerprint == fingerprint

    def test_relative_builtin_binary_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute or a bare name"):
            ClaudeDispatcher(claude_binary="./claude", usage_store=None)

    def test_a_versioned_launcher_self_update_keeps_the_same_pin(
        self, tmp_path,
    ) -> None:
        launcher = tmp_path / "bin" / "claude"
        launcher.parent.mkdir()
        first = tmp_path / "versions" / "1" / "claude"
        second = tmp_path / "versions" / "2" / "claude"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(b"v1")
        second.write_bytes(b"v2")
        launcher.symlink_to(first)

        pinned = ClaudeDispatcher(
            claude_binary=str(launcher), usage_store=None,
        ).agent_identity.fingerprint

        launcher.unlink()
        launcher.symlink_to(second)
        self_updated = ClaudeDispatcher(
            claude_binary=str(launcher), usage_store=None,
        ).agent_identity.fingerprint

        assert self_updated == pinned


class TestGenericManifestRegistration:
    def test_factory_builds_a_named_generic_plugin(self) -> None:
        cfg = AgentConfig(
            type="example",
            plugins={
                "example": {
                    "binary": "/opt/example-agent",
                    "args": ["run", "{prompt}"],
                    "resume_args": ["resume", "{resume_session_id}"],
                    "session_id_path": "session.id",
                    "model": "model-x",
                    "provider": "provider-y",
                },
            },
        )
        adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        assert isinstance(adapter, GenericCliDispatcher)
        identity = adapter.agent_identity
        assert identity.agent_type == "example"
        assert identity.plugin_id == "generic:example"
        assert identity.plugin_version == "1"
        assert identity.model == "model-x"
        assert identity.provider == "provider-y"
        assert identity.fingerprint
        assert adapter.capabilities.resume is True
        assert adapter.capabilities.usage is False
        assert adapter.instructions_files == ("AGENTS.md",)

    def test_factory_passes_the_manifest_instruction_filenames(self) -> None:
        cfg = AgentConfig(
            type="copilot",
            plugins={
                "copilot": {
                    "binary": "/opt/copilot",
                    "instructions_files": [
                        ".github/copilot-instructions.md",
                        "AGENTS.md",
                    ],
                },
            },
        )
        adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        assert adapter.instructions_files == (
            ".github/copilot-instructions.md",
            "AGENTS.md",
        )

    @pytest.mark.parametrize("name", ["/abs/AGENTS.md", "../AGENTS.md", "."])
    def test_manifest_instruction_filenames_must_stay_in_the_worktree(
        self, name,
    ) -> None:
        with pytest.raises(ValueError, match="safe relative path"):
            AgentConfig(
                plugins={
                    "example": {
                        "binary": "/opt/example",
                        "instructions_files": [name],
                    },
                },
            )

    @pytest.mark.parametrize("name", ["claude", "opencode", "Bad Name", "9lives"])
    def test_plugin_names_cannot_replace_builtins_or_be_ambiguous(self, name) -> None:
        with pytest.raises(ValueError, match="agent.plugins"):
            AgentConfig(plugins={name: {"binary": "/opt/example"}})

    def test_an_assignment_cannot_name_an_unenrolled_plugin(self) -> None:
        with pytest.raises(ValueError, match="unenrolled"):
            AgentConfig(assignable_types=["missing"])

    def test_resume_manifest_requires_a_way_to_recover_the_native_id(self) -> None:
        with pytest.raises(ValueError, match="session_id_path"):
            AgentConfig(
                plugins={
                    "example": {
                        "binary": "/opt/example",
                        "resume_args": ["resume", "{resume_session_id}"],
                    },
                },
            )

    def test_an_assignment_only_plugin_is_enrolled_without_a_repo_override(self) -> None:
        cfg = Config.model_validate({
            "node_id": "testbox",
            "paths": {
                "state_db": "/tmp/ctrlrelay-test/state.db",
                "worktrees": "/tmp/ctrlrelay-test/worktrees",
                "bare_repos": "/tmp/ctrlrelay-test/bare",
                "contexts": "/tmp/ctrlrelay-test/contexts",
                "skills": "/tmp/ctrlrelay-test/skills",
                "repo_root": "/tmp/ctrlrelay-test/projects",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {
                    "inbox": "/tmp/ctrlrelay-test/inbox.txt",
                    "outbox": "/tmp/ctrlrelay-test/outbox.txt",
                },
            },
            "agent": {
                "type": "claude",
                "plugins": {"example": {"binary": "/opt/example"}},
                "assignable_types": ["example"],
            },
            "repos": [{"name": "owner/repo"}],
        })
        router = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None,
        )
        assert isinstance(router, RepoAgentRouter)
        selected = select_agent(
            router, repo="owner/repo", requested_type="example",
        )
        assert isinstance(selected.dispatcher, GenericCliDispatcher)
        assert selected.identity.plugin_id == "generic:example"

    def test_generic_pin_survives_removal_from_assignment_allowlist(self) -> None:
        cfg = Config.model_validate({
            "node_id": "testbox",
            "paths": {
                "state_db": "/tmp/ctrlrelay-test/state.db",
                "worktrees": "/tmp/ctrlrelay-test/worktrees",
                "bare_repos": "/tmp/ctrlrelay-test/bare",
                "contexts": "/tmp/ctrlrelay-test/contexts",
                "skills": "/tmp/ctrlrelay-test/skills",
                "repo_root": "/tmp/ctrlrelay-test/projects",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {
                    "inbox": "/tmp/ctrlrelay-test/inbox.txt",
                    "outbox": "/tmp/ctrlrelay-test/outbox.txt",
                },
            },
            "agent": {
                "type": "claude",
                "plugins": {"example": {"binary": "/opt/example"}},
                "assignable_types": [],
            },
            "repos": [{"name": "owner/repo"}],
        })
        dispatcher = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None,
        )
        # Listed but not routed: built only when a pinned run needs it.
        fingerprint = dispatcher.resume_factories["example"]().agent_identity.fingerprint
        selected = select_agent(
            dispatcher,
            repo="owner/repo",
            pinned=AgentIdentity(
                agent_type="example",
                plugin_id="generic:example",
                fingerprint=fingerprint,
            ),
        )
        assert selected.identity.agent_type == "example"
        assert isinstance(selected.dispatcher, GenericCliDispatcher)

    def test_a_listed_plugin_that_cannot_run_under_bwrap_does_not_stop_startup(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fake_bwrap(monkeypatch)
        cfg = _config_with_agent({
            "type": "claude",
            "sandbox": "bwrap",
            "plugins": {"example": {"binary": "/opt/example"}},
        })
        dispatcher = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None,
        )
        assert dispatcher.agent_type == "claude"
        # Selecting it later is a clean refusal, not an unhandled ValueError.
        with pytest.raises(AgentSelectionError, match="cannot be built"):
            select_agent(
                dispatcher,
                repo="owner/repo",
                pinned=AgentIdentity(
                    agent_type="example", plugin_id="generic:example",
                ),
            )

    def test_a_routed_plugin_that_cannot_run_under_bwrap_stops_startup(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fake_bwrap(monkeypatch)
        cfg = _config_with_agent({
            "type": "claude",
            "sandbox": "bwrap",
            "plugins": {"example": {"binary": "/opt/example"}},
            "assignable_types": ["example"],
        })
        with pytest.raises(ValueError, match="agent.assignable_types"):
            make_repo_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)

    def test_a_router_turns_a_lazy_build_failure_into_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fake_bwrap(monkeypatch)
        cfg = _config_with_agent({
            "type": "claude",
            "sandbox": "bwrap",
            "plugins": {"example": {"binary": "/opt/example"}},
        })
        bare = make_repo_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        router = RepoAgentRouter(
            default=bare, resume_factories=bare.resume_factories,
        )
        with pytest.raises(AgentSelectionError, match="cannot be built"):
            router.select(
                "owner/repo",
                pinned=AgentIdentity(
                    agent_type="example", plugin_id="generic:example",
                ),
            )


def _fake_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the claude default accept bwrap on a machine without it."""
    monkeypatch.setattr(
        "ctrlrelay.core.dispatcher.resolve_bwrap", lambda: "/usr/bin/bwrap",
    )


def _config_with_agent(agent: dict) -> Config:
    return Config.model_validate({
        "node_id": "testbox",
        "paths": {
            "state_db": "/tmp/ctrlrelay-test/state.db",
            "worktrees": "/tmp/ctrlrelay-test/worktrees",
            "bare_repos": "/tmp/ctrlrelay-test/bare",
            "contexts": "/tmp/ctrlrelay-test/contexts",
            "skills": "/tmp/ctrlrelay-test/skills",
            "repo_root": "/tmp/ctrlrelay-test/projects",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": "/tmp/ctrlrelay-test/inbox.txt",
                "outbox": "/tmp/ctrlrelay-test/outbox.txt",
            },
        },
        "agent": agent,
        "repos": [{"name": "owner/repo"}],
    })


def selected_fingerprint(dispatcher: RepoAgentRouter) -> str:
    identity = dispatcher.by_type["example"].agent_identity
    assert identity.fingerprint is not None
    return identity.fingerprint


class TestAgentProgramIsNeverTakenFromTheWorktree:
    """The child's PATH has no relative entries, so no lookup reaches the worktree."""

    def _planted(self, tmp_path, name="fake-agent-xyz"):
        worktree = tmp_path / "wt"
        worktree.mkdir(exist_ok=True)
        planted = worktree / name
        planted.write_text("#!/bin/sh\ntouch \"$PWD/PWNED\"\n")
        planted.chmod(0o755)
        return worktree

    def test_relative_entries_are_dropped(self) -> None:
        from ctrlrelay.core.dispatcher import _absolute_path_only

        env = _absolute_path_only({"PATH": ".::bin:/usr/bin", "HOME": "/h"})
        assert env == {"PATH": "/usr/bin", "HOME": "/h"}

    def test_an_all_absolute_path_is_left_alone(self) -> None:
        from ctrlrelay.core.dispatcher import _absolute_path_only

        env = {"PATH": "/usr/bin:/bin"}
        assert _absolute_path_only(env) is env

    def _run(self, adapter, worktree, **kw):
        import asyncio

        return asyncio.run(adapter.spawn_session(
            "s1", "hi", worktree, worktree / ".ctrlrelay" / "state.json",
            timeout=10, github_access=False, **kw,
        ))

    def test_a_bare_agent_name_is_not_found_in_the_worktree(
        self, tmp_path, monkeypatch,
    ) -> None:
        import os

        from ctrlrelay.core.dispatcher import OpenCodeDispatcher

        worktree = self._planted(tmp_path)
        monkeypatch.setenv("PATH", "." + os.pathsep + os.environ["PATH"])
        adapter = OpenCodeDispatcher(
            opencode_binary="fake-agent-xyz", usage_store=None,
        )
        with pytest.raises(FileNotFoundError):
            self._run(adapter, worktree)
        assert not (worktree / "PWNED").exists()

    def test_an_env_shebang_launcher_does_not_find_its_interpreter_in_the_worktree(
        self, tmp_path, monkeypatch,
    ) -> None:
        """``#!/usr/bin/env fake-interp`` searches the child's PATH."""
        import os

        from ctrlrelay.core.dispatcher import OpenCodeDispatcher

        worktree = self._planted(tmp_path, name="fake-interp")
        launcher = tmp_path / "bin" / "agent-launcher"
        launcher.parent.mkdir()
        launcher.write_text("#!/usr/bin/env fake-interp\n")
        launcher.chmod(0o755)
        monkeypatch.setenv("PATH", "." + os.pathsep + os.environ["PATH"])
        adapter = OpenCodeDispatcher(
            opencode_binary=str(launcher), usage_store=None,
        )
        self._run(adapter, worktree)
        assert not (worktree / "PWNED").exists()

    def test_a_sandboxed_spawn_gets_the_same_path(self, tmp_path) -> None:
        """bwrap searches the PATH it is given, from the worktree too."""
        import asyncio
        import os
        from unittest.mock import patch

        from ctrlrelay.core.dispatcher import OpenCodeDispatcher

        worktree = self._planted(tmp_path)
        adapter = OpenCodeDispatcher(opencode_binary="/bin/echo", usage_store=None)
        env = {"PATH": "." + os.pathsep + "/usr/bin"}
        with patch(
            "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("stop"),
        ) as spawn:
            with pytest.raises(FileNotFoundError):
                asyncio.run(adapter._run_session(
                    cmd=["/usr/bin/bwrap", "--die-with-parent", "--", "fake-agent-xyz"],
                    env=env, ident={}, session_id="s1", prompt="hi",
                    working_dir=worktree,
                    state_file=worktree / ".ctrlrelay" / "state.json",
                    timeout=10, resume_session_id=None, repo=None,
                    issue_number=None, sandbox_prefix_len=3,
                ))
        assert spawn.call_args.kwargs["env"]["PATH"] == "/usr/bin"
