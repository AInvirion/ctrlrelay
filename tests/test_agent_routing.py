"""Per-repo agent backend selection (#286).

The ticket's scope is an **opt-in, per-repo** backend with `claude` the
default — not a global flip. ctrlrelay runs unattended over many
repositories and opens pull requests, so a weaker model behind the whole
daemon produces bad pull requests at machine speed with nobody watching.

The routing lives in one adapter that satisfies `AgentAdapter` and picks
a backend from the `repo` it is already handed on every `spawn_session`
call. That is deliberate: the alternative was editing the ~8 call sites
that pass `dispatcher=dispatcher`, and a call site missed there is a repo
silently running on the wrong backend.
"""

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _cfg(**kw):
    """A Config with two repos, the minimum the loader accepts."""
    from ctrlrelay.core.config import Config

    data = {
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
        "repos": [
            {"name": "AInvirion/ctrlrelay"},
            {"name": "AInvirion/scratchpad", "agent_type": "opencode"},
        ],
    }
    data.update(kw)
    return Config.model_validate(data)


def _fake_adapter(name: str):
    adapter = AsyncMock()
    adapter.agent_type = name
    adapter.active = 0
    adapter.started = 0
    return adapter


class TestRepoConfig:
    def test_agent_type_defaults_to_none_meaning_the_global_one(self) -> None:
        from ctrlrelay.core.config import RepoConfig

        assert RepoConfig(name="o/r").agent_type is None

    def test_a_per_repo_override_loads(self) -> None:
        cfg = _cfg()
        by_name = {r.name: r for r in cfg.repos}
        assert by_name["AInvirion/ctrlrelay"].agent_type is None
        assert by_name["AInvirion/scratchpad"].agent_type == "opencode"


class TestRouter:
    def test_satisfies_the_adapter_protocol(self) -> None:
        from ctrlrelay.core.dispatcher import AgentAdapter, RepoAgentRouter

        assert inspect.signature(
            RepoAgentRouter.spawn_session
        ) == inspect.signature(AgentAdapter.spawn_session)

    @pytest.mark.asyncio
    async def test_an_overridden_repo_goes_to_its_own_backend(self) -> None:
        from ctrlrelay.core.dispatcher import RepoAgentRouter

        default = _fake_adapter("claude")
        local = _fake_adapter("opencode")
        router = RepoAgentRouter(
            default=default, by_repo={"AInvirion/scratchpad": local}
        )

        await router.spawn_session(
            session_id="s1",
            prompt="p",
            working_dir=Path("/tmp"),
            state_file=Path("/tmp/state.json"),
            repo="AInvirion/scratchpad",
            github_access=False,
        )
        local.spawn_session.assert_awaited_once()
        default.spawn_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_every_other_repo_goes_to_the_default(self) -> None:
        from ctrlrelay.core.dispatcher import RepoAgentRouter

        default = _fake_adapter("claude")
        local = _fake_adapter("opencode")
        router = RepoAgentRouter(
            default=default, by_repo={"AInvirion/scratchpad": local}
        )

        await router.spawn_session(
            session_id="s1",
            prompt="p",
            working_dir=Path("/tmp"),
            state_file=Path("/tmp/state.json"),
            repo="AInvirion/ctrlrelay",
            github_access=False,
        )
        default.spawn_session.assert_awaited_once()
        local.spawn_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unnamed_repo_goes_to_the_default_not_the_weaker_model(
        self,
    ) -> None:
        """`repo` was observability-only before this, so a caller may
        omit it. Falling back to the DEFAULT is the safe direction: an
        unrouted spawn must not silently land on a local model."""
        from ctrlrelay.core.dispatcher import RepoAgentRouter

        default = _fake_adapter("claude")
        local = _fake_adapter("opencode")
        router = RepoAgentRouter(
            default=default, by_repo={"AInvirion/scratchpad": local}
        )

        for repo in (None, "", "someone/unconfigured"):
            await router.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=Path("/tmp"),
                state_file=Path("/tmp/state.json"),
                repo=repo,
                github_access=False,
            )
        assert default.spawn_session.await_count == 3
        local.spawn_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_repo_name_is_matched_case_insensitively(self) -> None:
        """GitHub treats `owner/repo` case-insensitively and issue
        payloads, config and `gh` output do not always agree on the
        case. A route that depends on which of them reached us is a
        route that works until it does not."""
        from ctrlrelay.core.dispatcher import RepoAgentRouter

        default = _fake_adapter("claude")
        local = _fake_adapter("opencode")
        router = RepoAgentRouter(
            default=default, by_repo={"AInvirion/scratchpad": local}
        )

        await router.spawn_session(
            session_id="s1",
            prompt="p",
            working_dir=Path("/tmp"),
            state_file=Path("/tmp/state.json"),
            repo="ainvirion/SCRATCHPAD",
            github_access=False,
        )
        local.spawn_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_every_argument_is_forwarded_unchanged(self) -> None:
        from ctrlrelay.core.budget import BudgetScope
        from ctrlrelay.core.dispatcher import RepoAgentRouter

        local = _fake_adapter("opencode")
        router = RepoAgentRouter(
            default=_fake_adapter("claude"),
            by_repo={"AInvirion/scratchpad": local},
        )
        scope = BudgetScope(labels=["size:S"])

        await router.spawn_session(
            session_id="s1",
            prompt="the prompt",
            working_dir=Path("/tmp/wt"),
            state_file=Path("/tmp/wt/state.json"),
            timeout=42,
            resume_session_id="ses_1",
            repo="AInvirion/scratchpad",
            issue_number=286,
            github_access=True,
            budget_scope=scope,
        )
        kwargs = local.spawn_session.await_args.kwargs
        assert kwargs == {
            "session_id": "s1",
            "prompt": "the prompt",
            "working_dir": Path("/tmp/wt"),
            "state_file": Path("/tmp/wt/state.json"),
            "timeout": 42,
            "resume_session_id": "ses_1",
            "repo": "AInvirion/scratchpad",
            "issue_number": 286,
            "github_access": True,
            "budget_scope": scope,
        }


class TestIdleUsageProbe:
    """The daemon's probe reads three attributes off whatever it was
    handed, with `getattr(..., default)`. A router that answered none of
    them would silently turn the probe off; one that answered them
    wrongly would fire a subscription call while a run is live."""

    def test_claude_binary_is_exposed_so_the_probe_still_runs(self) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, RepoAgentRouter

        claude = ClaudeDispatcher(usage_store=None, claude_binary="/usr/bin/claude")
        router = RepoAgentRouter(
            default=claude, by_repo={"o/r": _fake_adapter("opencode")}
        )
        assert router.claude_binary == "/usr/bin/claude"

    def test_with_no_claude_backend_at_all_there_is_nothing_to_probe(
        self,
    ) -> None:
        """A pure-opencode deployment has no subscription. The probe
        skips on a falsy binary, which is the honest answer."""
        from ctrlrelay.core.dispatcher import OpenCodeDispatcher, RepoAgentRouter

        router = RepoAgentRouter(
            default=OpenCodeDispatcher(usage_store=None), by_repo={}
        )
        assert not router.claude_binary

    def test_a_claude_backend_behind_an_opencode_default_is_found(self) -> None:
        from ctrlrelay.core.dispatcher import (
            ClaudeDispatcher,
            OpenCodeDispatcher,
            RepoAgentRouter,
        )

        router = RepoAgentRouter(
            default=OpenCodeDispatcher(usage_store=None),
            by_repo={
                "o/r": ClaudeDispatcher(
                    usage_store=None, claude_binary="/usr/bin/claude"
                )
            },
        )
        assert router.claude_binary == "/usr/bin/claude"

    @pytest.mark.asyncio
    async def test_active_and_started_count_runs_on_every_backend(
        self, tmp_path: Path
    ) -> None:
        """`active == 0` is how the probe decides the machine is idle.
        Counting only the default backend would fire a subscription call
        in the middle of an opencode run."""
        from ctrlrelay.core.dispatcher import OpenCodeDispatcher, RepoAgentRouter

        local = OpenCodeDispatcher(usage_store=None)
        router = RepoAgentRouter(
            default=OpenCodeDispatcher(usage_store=None),
            by_repo={"o/r": local},
        )
        assert router.active == 0
        assert router.started == 0

        seen: list[tuple[int, int]] = []

        class _Proc:
            returncode = 0

            async def communicate(self):
                seen.append((router.active, router.started))
                return (b"", b"")

        with patch("asyncio.create_subprocess_exec", return_value=_Proc()):
            await router.spawn_session(
                session_id="s1",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                repo="o/r",
                github_access=False,
            )

        assert seen == [(1, 1)]
        assert router.active == 0
        assert router.started == 1


class TestBuildingTheRouter:
    def test_no_overrides_means_no_router_at_all(self) -> None:
        """An existing claude deployment must get exactly what it got
        before: the plain adapter, not a wrapper around it."""
        from ctrlrelay.core.dispatcher import (
            ClaudeDispatcher,
            make_repo_agent_dispatcher,
        )

        cfg = _cfg(repos=[{"name": "AInvirion/ctrlrelay"}])
        adapter = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None
        )
        assert isinstance(adapter, ClaudeDispatcher)

    def test_one_override_builds_a_router_over_both_backends(self) -> None:
        from ctrlrelay.core.dispatcher import (
            ClaudeDispatcher,
            OpenCodeDispatcher,
            RepoAgentRouter,
            make_repo_agent_dispatcher,
        )

        adapter = make_repo_agent_dispatcher(
            _cfg(), outcome_hook=None, usage_store=None
        )
        assert isinstance(adapter, RepoAgentRouter)
        assert isinstance(adapter.default, ClaudeDispatcher)
        assert isinstance(
            adapter.by_repo["ainvirion/scratchpad"], OpenCodeDispatcher
        )

    def test_repos_sharing_a_type_share_one_adapter(self) -> None:
        """One adapter per backend, not per repo: each holds the session
        counters the idle probe reads, and N copies would each see only
        their own runs."""
        from ctrlrelay.core.dispatcher import (
            RepoAgentRouter,
            make_repo_agent_dispatcher,
        )

        cfg = _cfg(repos=[
            {"name": "AInvirion/ctrlrelay"},
            {"name": "AInvirion/scratchpad", "agent_type": "opencode"},
            {"name": "AInvirion/other", "agent_type": "opencode"},
        ])
        adapter = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None
        )
        assert isinstance(adapter, RepoAgentRouter)
        assert (
            adapter.by_repo["ainvirion/scratchpad"]
            is adapter.by_repo["ainvirion/other"]
        )

    def test_a_repo_naming_the_global_type_needs_no_router(self) -> None:
        from ctrlrelay.core.dispatcher import (
            ClaudeDispatcher,
            make_repo_agent_dispatcher,
        )

        cfg = _cfg(repos=[
            {"name": "AInvirion/ctrlrelay", "agent_type": "claude"},
        ])
        adapter = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None
        )
        assert isinstance(adapter, ClaudeDispatcher)

    def test_an_unknown_per_repo_type_fails_at_startup(self) -> None:
        """Same rule as `agent.type`: a typo must stop the daemon, not
        quietly fall back to claude for that repo."""
        from ctrlrelay.core.dispatcher import make_repo_agent_dispatcher

        cfg = _cfg(repos=[
            {"name": "AInvirion/scratchpad", "agent_type": "opencoed"},
        ])
        with pytest.raises(NotImplementedError) as excinfo:
            make_repo_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        msg = str(excinfo.value)
        assert "opencoed" in msg
        assert "AInvirion/scratchpad" in msg

    def test_the_outcome_hook_and_usage_store_reach_every_backend(self) -> None:
        """#204 and #247 are required keywords on the factory precisely
        so no construction site inherits silence by omission. A second
        backend built without them would be exempt from both."""
        from ctrlrelay.core.dispatcher import (
            RepoAgentRouter,
            make_repo_agent_dispatcher,
        )
        from ctrlrelay.core.usage import UsageStore

        async def hook(outcome) -> None:
            return None

        store = UsageStore(Path("/tmp/ctrlrelay-test/usage.json"))
        adapter = make_repo_agent_dispatcher(
            _cfg(), outcome_hook=hook, usage_store=store
        )
        assert isinstance(adapter, RepoAgentRouter)
        for backend in [adapter.default, *adapter.by_repo.values()]:
            assert backend.outcome_hook is hook
            assert backend.usage_store is store
