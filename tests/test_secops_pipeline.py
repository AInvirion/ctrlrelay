"""Tests for secops pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSecopsPipeline:
    def test_secops_pipeline_has_name(self) -> None:
        """SecopsPipeline should have name attribute."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=MagicMock(),
            state_db=MagicMock(),
            transport=MagicMock(),
        )

        assert pipeline.name == "secops"

    @pytest.mark.asyncio
    async def test_run_dispatches_claude_session(self, tmp_path: Path) -> None:
        """Should dispatch Claude with secops prompt."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.secops import SecopsPipeline

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Merged 2 PRs"
        mock_state.outputs = {"merged_prs": [1, 2]}

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        pipeline = SecopsPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
        )

        result = await pipeline.run(ctx)

        assert result.success
        assert result.summary == "Merged 2 PRs"
        mock_dispatcher.spawn_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_returns_blocked_when_needs_input(self, tmp_path: Path) -> None:
        """Should return blocked result when Claude needs input."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.secops import SecopsPipeline

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        mock_state.question = "Should I merge major version bump?"
        mock_state.summary = None
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        pipeline = SecopsPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
        )

        result = await pipeline.run(ctx)

        assert not result.success
        assert result.blocked
        assert "major version" in result.question

    @pytest.mark.asyncio
    async def test_run_all_processes_multiple_repos(self, tmp_path: Path) -> None:
        """Should run secops on all configured repos."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Done"
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True

        repos = [
            MagicMock(name="owner/repo1", local_path=tmp_path / "repo1"),
            MagicMock(name="owner/repo2", local_path=tmp_path / "repo2"),
        ]

        results = await run_secops_all(
            repos=repos,
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_run_all_skips_locked_repos(self, tmp_path: Path) -> None:
        """Should skip repos that are already locked."""
        from ctrlrelay.pipelines.secops import run_secops_all

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = False

        repos = [MagicMock(name="owner/locked-repo")]

        results = await run_secops_all(
            repos=repos,
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path,
        )

        assert len(results) == 1
        assert not results[0].success
        assert "locked" in results[0].error.lower()


class TestSecopsPromptOperatorConfigPRs:
    """The secops prompt must instruct the agent to auto-merge
    operator-authored PRs that ONLY touch .github/dependabot.yml.
    Without this, "enable Dependabot ecosystem" PRs the operator
    opens manually sit forever — the conservative default treats
    every operator-authored PR as needing approval, but a config-only
    additive change has effectively the same risk as a Dependabot
    config bump (zero)."""

    def test_prompt_mentions_operator_config_pr_auto_merge(self) -> None:
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")

        # Carve-out exists for dependabot.yml-only PRs.
        assert ".github/dependabot.yml" in prompt
        assert "auto-merge" in prompt.lower()
        # Code changes always BLOCKED, even from the trusted operator.
        lower = prompt.lower()
        assert (
            "Never auto-merge code changes" in prompt
            or "never auto-merge code" in lower
        )

    def test_prompt_positively_identifies_operator_not_just_excludes_dependabot(
        self,
    ) -> None:
        """Codex P1 (review of #132): the carve-out must positively
        identify the trusted operator (`author.login == $OPERATOR`),
        not just `author.login != "app/dependabot"`. Otherwise a
        collaborator, external contributor, or another bot could open
        a `.github/dependabot.yml`-only PR and slip past review."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")

        # Must instruct the agent to derive its own identity from `gh api user`
        # and use that as the trusted-operator allowlist.
        assert "gh api user" in prompt
        assert "OPERATOR" in prompt
        assert '--author "$OPERATOR"' in prompt
        # Must explicitly call out that other authors (collaborators, apps,
        # external contributors) are NOT eligible — even for dependabot.yml-only.
        assert "collaborators" in prompt.lower() or "external contributors" in prompt.lower()
        # Must NOT use the original buggy filter pattern that codex flagged.
        assert 'author.login != "app/dependabot"' not in prompt

    def test_prompt_requires_passing_ci_for_operator_config_merge(self) -> None:
        """Codex P2 (review of #132 round 3): the Dependabot auto-merge
        path requires patch/minor + passing CI. The operator-config
        carve-out must require passing CI too — otherwise a PR that
        introduces invalid dependabot YAML could auto-merge and break
        Dependabot for the repo."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")

        assert "gh pr checks" in prompt
        # CI must be in the same gate list as author+diff.
        assert "CI check" in prompt or "all status checks pass" in prompt.lower()

    def test_prompt_requires_diff_validation_for_additive_only(self) -> None:
        """Codex P2 (review of #132 round 2): the prompt says 'additive
        ecosystem entries' in prose but must ACTUALLY require the agent
        to inspect the diff and BLOCK non-additive changes (deletions
        or modifications of existing stanzas). Otherwise a trusted
        operator could open a PR that removes an entire ecosystem
        block and the auto-merge would still fire."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")

        # Must instruct the agent to actually pull the diff.
        assert "gh pr diff" in prompt
        # Must explicitly say BLOCK if any deletion/modification line is
        # present in the diff.
        assert (
            "PURELY" in prompt or "purely additive" in prompt.lower()
        )
        # Must reference the `-` line marker so the agent has a concrete
        # signal to look for, not just vague "additive only" prose.
        assert "begins with `-`" in prompt or "lines beginning with `-`" in prompt.lower()


class TestSecopsBlockedDispatch:
    """Regression for the silent-blocked-secops bug: when an agent ends
    BLOCKED_NEEDS_INPUT, the orchestrator must call transport.ask() to
    deliver the question to the operator (mirroring dev/task pipelines).
    Without this, blocked secops sessions land in the DB with status
    'blocked' but the question never reaches Telegram."""

    @pytest.mark.asyncio
    async def test_blocked_secops_dispatches_question_via_transport(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        # First spawn returns BLOCKED with a question; the resume call
        # returns DONE so the session unblocks.
        blocked_state = MagicMock()
        blocked_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        blocked_state.question = "Merge PR #42 (major version bump)?"
        blocked_state.summary = None
        blocked_state.outputs = {}
        blocked_state.error = None

        done_state = MagicMock()
        done_state.status = CheckpointStatus.DONE
        done_state.summary = "Merged PR #42"
        done_state.outputs = {"merged_prs": [42]}
        done_state.error = None

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = [
            SessionResult(session_id="sess", exit_code=0, state=blocked_state),
            SessionResult(session_id="sess", exit_code=0, state=done_state),
        ]

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.get_agent_session_id.return_value = "sess"

        mock_transport = AsyncMock()
        mock_transport.ask.return_value = "yes, merge it"

        repo = MagicMock()
        repo.name = "owner/repo"

        results = await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=mock_transport,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(results) == 1
        assert results[0].success
        # transport.ask MUST be called with the agent's question — that's
        # the whole point of the fix.
        mock_transport.ask.assert_called_once()
        call_args = mock_transport.ask.call_args
        assert "Merge PR #42" in call_args.args[0]
        assert call_args.kwargs.get("session_id", "").startswith("secops-")
        assert call_args.kwargs.get("repo") == "owner/repo"
        # Final state should be 'done' since the resume succeeded.
        statuses = [
            c.args[1][0] for c in mock_db.execute.call_args_list
            if c.args and "UPDATE sessions" in c.args[0]
        ]
        assert "done" in statuses

    @pytest.mark.asyncio
    async def test_transport_ask_failure_preserves_blocked_for_pending_resume(
        self, tmp_path: Path
    ) -> None:
        """When transport.ask() raises (bridge down, timeout), the session
        must stay blocked so the existing pending_resumes persistence
        branch fires — letting an out-of-band Telegram reply still resume
        the session via the sweeper."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        blocked_state = MagicMock()
        blocked_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        blocked_state.question = "Need decision"
        blocked_state.summary = None
        blocked_state.outputs = {}
        blocked_state.error = None

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess", exit_code=0, state=blocked_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True

        mock_transport = AsyncMock()
        mock_transport.ask.side_effect = RuntimeError("bridge unavailable")

        repo = MagicMock()
        repo.name = "owner/repo"

        results = await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=mock_transport,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(results) == 1
        assert results[0].blocked
        # Pending resume row should still get inserted so a later reply
        # can route through the sweeper.
        mock_db.add_pending_resume.assert_called_once()
        assert "Need decision" in mock_db.add_pending_resume.call_args.kwargs["question"]


class TestSecopsPromptRespectsAutomationConfig:
    """The per-repo `automation:` block in orchestrator.yaml defines the
    Dependabot policy per severity tier (patch/minor/major × auto/ask/never).
    Before #133, the secops prompt had hardcoded prose ("merge patch/minor")
    that ignored the config entirely — so a repo configured `dependabot_minor:
    never` would still get its minors auto-merged. The prompt must reflect
    the configured policy per repo.
    """

    def test_default_policy_when_no_automation_passed(self) -> None:
        """With no automation, the prompt must use the AutomationConfig
        defaults: patch=auto, minor=ask, never-major."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")

        assert "patch updates: AUTO-MERGE" in prompt
        assert "minor updates: ASK" in prompt
        assert "major updates: NEVER" in prompt

    def test_per_repo_policy_overrides_defaults(self) -> None:
        """When AutomationConfig is passed in, its values drive the prompt."""
        from ctrlrelay.core.config import AutomationConfig, AutomationPolicy
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        # Aggressive: auto-merge everything including majors.
        automation = AutomationConfig(
            dependabot_patch=AutomationPolicy.AUTO,
            dependabot_minor=AutomationPolicy.AUTO,
            dependabot_major=AutomationPolicy.AUTO,
        )
        prompt = pipeline._build_prompt(
            repo="o/r", session_id="s1", automation=automation,
        )

        assert "patch updates: AUTO-MERGE" in prompt
        assert "minor updates: AUTO-MERGE" in prompt
        assert "major updates: AUTO-MERGE" in prompt

    def test_conservative_policy_renders_never_for_all(self) -> None:
        """Conservative repo: dependabot disabled across all tiers."""
        from ctrlrelay.core.config import AutomationConfig, AutomationPolicy
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        automation = AutomationConfig(
            dependabot_patch=AutomationPolicy.NEVER,
            dependabot_minor=AutomationPolicy.NEVER,
            dependabot_major=AutomationPolicy.NEVER,
        )
        prompt = pipeline._build_prompt(
            repo="o/r", session_id="s1", automation=automation,
        )

        assert "patch updates: NEVER" in prompt
        assert "minor updates: NEVER" in prompt
        assert "major updates: NEVER" in prompt

    @pytest.mark.asyncio
    async def test_run_secops_all_threads_automation_into_ctx_extra(
        self, tmp_path: Path
    ) -> None:
        """run_secops_all must read each RepoConfig.automation and put it
        in ctx.extra so the prompt builder picks it up. Without this, the
        per-repo schema is decorative."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.config import AutomationConfig, AutomationPolicy
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        captured_prompts: list[str] = []

        async def fake_spawn(**kwargs):
            captured_prompts.append(kwargs["prompt"])
            state = MagicMock()
            state.status = CheckpointStatus.DONE
            state.summary = "Done"
            state.outputs = {}
            state.error = None
            return SessionResult(
                session_id=kwargs["session_id"], exit_code=0, state=state,
            )

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = fake_spawn

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True

        # Repo configured with NEVER on patches — a non-default policy.
        repo = MagicMock()
        repo.name = "owner/repo"
        repo.automation = AutomationConfig(
            dependabot_patch=AutomationPolicy.NEVER,
            dependabot_minor=AutomationPolicy.NEVER,
            dependabot_major=AutomationPolicy.NEVER,
        )

        await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(captured_prompts) == 1
        # The spawned prompt must reflect the NEVER policy — not the
        # auto/ask/never default.
        assert "patch updates: NEVER" in captured_prompts[0]


class TestSecopsCleanupLogging:
    """Regression for codex round-4 [P3]: worktree cleanup failures must
    not be silently swallowed. Log them via the obs stream so operators
    can see leaked admin state instead of discovering it later via a
    "worktree already exists" failure on a subsequent run."""

    @pytest.mark.asyncio
    async def test_worktree_remove_failure_is_logged(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Done"
        mock_state.outputs = {}
        mock_state.error = None
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess",
            exit_code=0,
            state=mock_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"
        # remove_worktree blows up — simulates a wedged `git worktree prune`.
        mock_worktree.remove_worktree.side_effect = RuntimeError(
            "worktree removal failed"
        )

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True

        repo = MagicMock(name="owner/repo")
        repo.name = "owner/repo"

        with patch("ctrlrelay.pipelines.secops._logger") as mock_logger:
            results = await run_secops_all(
                repos=[repo],
                dispatcher=mock_dispatcher,
                github=MagicMock(),
                worktree=mock_worktree,
                dashboard=None,
                state_db=mock_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
            )

        # Cleanup failure should not break the pipeline (result is still
        # recorded) but it MUST be logged — `log_event` calls _logger.info
        # under the hood so it shows up somewhere in mock_calls.
        assert len(results) == 1
        assert "secops.cleanup.worktree_failed" in str(mock_logger.mock_calls), (
            "worktree removal failure must be logged via obs, not "
            "swallowed (codex round-4 [P3] regression)"
        )
        # The repo lock must still be released.
        mock_db.release_lock.assert_called()


class TestSecopsCancelDoesNotOverwriteCompletedStatus:
    """Regression for codex round-6 [P2]: a CancelledError landing AFTER
    the session has already been marked done/blocked/failed (e.g. during
    the post-run dashboard.push_event) must NOT overwrite that final
    status with 'cancelled'."""

    @pytest.mark.asyncio
    async def test_cancel_during_dashboard_push_preserves_done_status(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Ran clean"
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess",
            exit_code=0,
            state=mock_state,
        )

        # Dashboard push hangs; we'll cancel during it.
        async def slow_push(payload):  # noqa: ARG001
            await asyncio.sleep(60)

        mock_dashboard = MagicMock()
        mock_dashboard.push_event = AsyncMock(side_effect=slow_push)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True

        repo = MagicMock()
        repo.name = "owner/repo"

        task = asyncio.create_task(
            run_secops_all(
                repos=[repo],
                dispatcher=mock_dispatcher,
                github=MagicMock(),
                worktree=mock_worktree,
                dashboard=mock_dashboard,
                state_db=mock_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
            )
        )

        # Wait until dashboard.push_event has started — that's when the
        # session is already marked 'done' but we're stuck in the await.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if mock_dashboard.push_event.called:
                break

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Examine all UPDATE sessions calls. The 'done' row must have
        # been written, and NO subsequent 'cancelled' overwrite must
        # have happened.
        updates = [
            c.args[1] for c in mock_db.execute.call_args_list
            if c.args and "UPDATE sessions" in c.args[0]
        ]
        statuses = [u[0] for u in updates if u]
        assert "done" in statuses, "happy-path 'done' update should have run"
        assert "cancelled" not in statuses, (
            "cancel during post-run cleanup must NOT clobber 'done' with "
            "'cancelled' (codex round-6 [P2] regression)"
        )


class TestSecopsLockHeldThroughCleanup:
    """Regression for codex round-10 [P1-a]: the per-repo lock must be
    held THROUGH worktree cleanup (not released before). Releasing early
    lets a concurrent dev/secops run acquire the same repo and race
    `git worktree prune` on the shared bare clone. Round 5 asked for
    release-before-cleanup to avoid lock leaks on cancel, but the
    bounded `asyncio.wait_for` timeout we use now makes that tradeoff
    unnecessary — cleanup either finishes or bails within 130s, then
    the lock is released."""

    @pytest.mark.asyncio
    async def test_lock_held_across_worktree_removal_on_happy_path(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        events: list[str] = []

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.side_effect = lambda *a, **kw: events.append(
            "release_lock"
        )

        async def traced_remove(repo_, session_id_):
            events.append("remove_worktree_start")
            await asyncio.sleep(0)
            events.append("remove_worktree_done")

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"
        mock_worktree.remove_worktree.side_effect = traced_remove

        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "ok"
        mock_state.outputs = {}
        mock_state.error = None
        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="s", exit_code=0, state=mock_state,
        )

        repo = MagicMock()
        repo.name = "owner/repo"

        await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        # remove_worktree must complete BEFORE release_lock — the repo
        # must be locked throughout cleanup so a concurrent run can't
        # race `git worktree prune`.
        assert "remove_worktree_done" in events
        assert "release_lock" in events
        assert events.index("remove_worktree_done") < events.index(
            "release_lock"
        ), (
            f"lock must stay held until worktree cleanup completes; "
            f"got events={events} (codex round-10 [P1-a] regression)"
        )

    @pytest.mark.asyncio
    async def test_cancel_still_releases_lock_so_daemon_restart_not_wedged(
        self, tmp_path: Path
    ) -> None:
        """Round-5 concern (lock-leak on cancel) is preserved: if cleanup
        is cancelled mid-flight, the lock is released explicitly in the
        except-CancelledError path before the error propagates."""
        import asyncio

        from ctrlrelay.pipelines.secops import run_secops_all

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        release_calls: list[tuple] = []
        mock_db.release_lock.side_effect = lambda *a, **kw: (
            release_calls.append(a) or True
        )

        async def slow_remove(repo_, session_id_):
            # Long enough that cancel will fire before it returns;
            # short enough that the test doesn't wait on a stray task.
            await asyncio.sleep(0.5)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"
        mock_worktree.remove_worktree.side_effect = slow_remove

        async def hang(ctx):  # noqa: ARG001
            await asyncio.Event().wait()

        repo = MagicMock()
        repo.name = "owner/repo"

        with patch(
            "ctrlrelay.pipelines.secops.SecopsPipeline.run",
            side_effect=hang,
        ):
            task = asyncio.create_task(
                run_secops_all(
                    repos=[repo],
                    dispatcher=AsyncMock(),
                    github=MagicMock(),
                    worktree=mock_worktree,
                    dashboard=None,
                    state_db=mock_db,
                    transport=None,
                    contexts_dir=tmp_path / "contexts",
                )
            )

            for _ in range(30):
                await asyncio.sleep(0.01)
                if any(
                    "INSERT INTO sessions" in c.args[0]
                    for c in mock_db.execute.call_args_list
                    if c.args
                ):
                    break

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert release_calls, (
            "cancel path must release the lock so a daemon restart "
            "isn't wedged"
        )


class TestSecopsCancellation:
    """Regression for codex [P2]: when a scheduled secops sweep is
    cancelled mid-run (scheduler.shutdown → SIGTERM), the session row
    must not be left in 'running' and the worktree must still be
    removed. Previously only the `except Exception` path wrote the
    session row, so CancelledError bypassed cleanup entirely."""

    @pytest.mark.asyncio
    async def test_cancel_during_run_marks_session_cancelled_and_removes_worktree(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        from ctrlrelay.pipelines.secops import run_secops_all

        # Pipeline blocks forever; we'll cancel from the outside.
        async def hang_forever(ctx):  # noqa: ARG001
            await asyncio.Event().wait()

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True

        repo = MagicMock(name="owner/repo")
        repo.name = "owner/repo"

        with patch(
            "ctrlrelay.pipelines.secops.SecopsPipeline.run",
            side_effect=hang_forever,
        ):
            task = asyncio.create_task(
                run_secops_all(
                    repos=[repo],
                    dispatcher=AsyncMock(),
                    github=MagicMock(),
                    worktree=mock_worktree,
                    dashboard=None,
                    state_db=mock_db,
                    transport=None,
                    contexts_dir=tmp_path / "contexts",
                )
            )

            # Wait until the pipeline is actually running inside the try
            # block (sessions INSERT has happened), then cancel.
            for _ in range(20):
                await asyncio.sleep(0.01)
                execute_calls = [c.args[0] for c in mock_db.execute.call_args_list]
                if any("INSERT INTO sessions" in s for s in execute_calls):
                    break

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Assert: session was updated to 'cancelled' before cleanup.
        cancel_updates = [
            c for c in mock_db.execute.call_args_list
            if c.args and "UPDATE sessions" in c.args[0]
            and len(c.args) > 1 and "cancelled" in c.args[1]
        ]
        assert cancel_updates, (
            "CancelledError path must write 'cancelled' status — "
            "codex [P2] regression"
        )

        # Assert: worktree cleanup was called in the finally block.
        assert mock_worktree.remove_worktree.called, (
            "finally block must remove the worktree on cancel"
        )

        # Assert: the per-repo lock was released.
        assert mock_db.release_lock.called


class TestPRNumberExtraction:
    """The persistence layer keys decisions by PR#, so the regex that
    pulls PR numbers out of free-form BLOCKED questions has to handle
    the variants the agent emits in practice (see real
    pending_resumes rows in production)."""

    def test_extracts_pr_hash_form(self) -> None:
        from ctrlrelay.pipelines.secops import _extract_pr_numbers

        q = "Dependabot PR #60 is a MAJOR bump. Approve merge?"
        assert _extract_pr_numbers(q) == ["60"]

    def test_extracts_bare_hash_form(self) -> None:
        from ctrlrelay.pipelines.secops import _extract_pr_numbers

        q = "approve which of: #15, #14, #13?"
        assert _extract_pr_numbers(q) == ["15", "14", "13"]

    def test_extracts_mixed_forms_and_dedupes(self) -> None:
        from ctrlrelay.pipelines.secops import _extract_pr_numbers

        q = "PR #293 (major); PR #290 (major). Also #293 has alert."
        assert _extract_pr_numbers(q) == ["293", "290"]

    def test_returns_empty_when_no_pr_mentioned(self) -> None:
        """Edge-case questions about a repo with no PRs (e.g. CodeQL
        alert decisions) shouldn't produce phantom rows."""
        from ctrlrelay.pipelines.secops import _extract_pr_numbers

        q = "Open CodeQL alert on injection sink. Suppress or fix?"
        assert _extract_pr_numbers(q) == []


class TestSecopsDecisionPersistence:
    """When the operator answers a BLOCKED question, the pipeline must
    persist that answer against every PR# mentioned in the question so
    the next sweep skips re-asking. Without this, the daily 6am cron
    surfaces the same "approve PR #60?" question every day even after
    the operator has answered."""

    @pytest.mark.asyncio
    async def test_blocked_loop_records_decisions_per_pr(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        blocked_state = MagicMock()
        blocked_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        blocked_state.question = "Approve majors PR #60 and PR #61?"
        blocked_state.summary = None
        blocked_state.outputs = {}
        blocked_state.error = None

        done_state = MagicMock()
        done_state.status = CheckpointStatus.DONE
        done_state.summary = "Merged"
        done_state.outputs = {}
        done_state.error = None

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = [
            SessionResult(session_id="s", exit_code=0, state=blocked_state),
            SessionResult(session_id="s", exit_code=0, state=done_state),
        ]

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.list_recent_automation_decisions.return_value = []

        mock_transport = AsyncMock()
        mock_transport.ask.return_value = "yes merge both"

        repo = MagicMock()
        repo.name = "owner/repo"

        await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=mock_transport,
            contexts_dir=tmp_path / "contexts",
        )

        # One row per PR# in the question, both with the verbatim
        # operator answer.
        calls = mock_db.record_automation_decision.call_args_list
        item_ids = sorted(c.kwargs["item_id"] for c in calls)
        assert item_ids == ["#60", "#61"]
        for c in calls:
            assert c.kwargs["decision"] == "yes merge both"
            assert c.kwargs["repo"] == "owner/repo"
            assert c.kwargs["operation"] == "dependabot_pr"

    @pytest.mark.asyncio
    async def test_no_decision_written_when_question_lacks_pr_number(
        self, tmp_path: Path
    ) -> None:
        """A BLOCKED question about a CodeQL alert (no PR#) must not
        produce a phantom row keyed on an empty item_id."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        blocked_state = MagicMock()
        blocked_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        blocked_state.question = "Open CodeQL alert: suppress or fix?"
        blocked_state.summary = None
        blocked_state.outputs = {}
        blocked_state.error = None

        done_state = MagicMock()
        done_state.status = CheckpointStatus.DONE
        done_state.summary = "Done"
        done_state.outputs = {}
        done_state.error = None

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = [
            SessionResult(session_id="s", exit_code=0, state=blocked_state),
            SessionResult(session_id="s", exit_code=0, state=done_state),
        ]

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.list_recent_automation_decisions.return_value = []

        mock_transport = AsyncMock()
        mock_transport.ask.return_value = "suppress for now"

        repo = MagicMock()
        repo.name = "owner/repo"

        await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=mock_transport,
            contexts_dir=tmp_path / "contexts",
        )

        mock_db.record_automation_decision.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_from_pending_records_decisions(
        self, tmp_path: Path
    ) -> None:
        """resume_secops_from_pending runs from the sweeper (out-of-band
        Telegram reply, original session torn down). It must also
        capture the operator's answer so the next 6am sweep doesn't
        re-ask. The question text is carried from the pending_resumes
        row."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import resume_secops_from_pending

        done_state = MagicMock()
        done_state.status = CheckpointStatus.DONE
        done_state.summary = "Resumed and merged"
        done_state.outputs = {}
        done_state.error = None

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="s", exit_code=0, state=done_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True

        await resume_secops_from_pending(
            session_id="s",
            repo="owner/repo",
            answer="merge 60 only",
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
            question="Approve PR #60 and PR #61?",
        )

        calls = mock_db.record_automation_decision.call_args_list
        item_ids = sorted(c.kwargs["item_id"] for c in calls)
        assert item_ids == ["#60", "#61"]
        for c in calls:
            assert c.kwargs["decision"] == "merge 60 only"


class TestSecopsPriorDecisionsInPrompt:
    """The prompt must inject the rolling window of prior operator
    decisions for this repo so the agent can act on them instead of
    re-asking. Without injection, persistence is dead weight."""

    def test_prompt_includes_prior_decisions_block(self) -> None:
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prior = [
            {
                "decided_at": 1779445200,
                "item_id": "#60",
                "decision": "yes, merge it",
            },
            {
                "decided_at": 1779358800,
                "item_id": "#25",
                "decision": "skip for now",
            },
        ]
        prompt = pipeline._build_prompt(
            repo="o/r", session_id="s1", prior_decisions=prior,
        )

        assert "Prior operator decisions" in prompt
        assert '#60: operator said "yes, merge it"' in prompt
        assert '#25: operator said "skip for now"' in prompt
        # Must instruct the agent to act on prior decisions, not just
        # echo them back as decoration.
        assert "avoid re-asking" in prompt.lower()
        assert "materially changed" in prompt.lower()

    def test_prompt_omits_block_when_no_prior_decisions(self) -> None:
        """When the table is empty (fresh repo, or window expired), the
        block must be omitted entirely — not rendered as an empty
        header that would confuse the agent."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")
        assert "Prior operator decisions" not in prompt

    @pytest.mark.asyncio
    async def test_run_secops_all_threads_prior_decisions_into_prompt(
        self, tmp_path: Path
    ) -> None:
        """run_secops_all must query state_db for prior decisions and
        thread them through ctx.extra so _build_prompt picks them up."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        captured_prompts: list[str] = []

        async def fake_spawn(**kwargs):
            captured_prompts.append(kwargs["prompt"])
            state = MagicMock()
            state.status = CheckpointStatus.DONE
            state.summary = "Done"
            state.outputs = {}
            state.error = None
            return SessionResult(
                session_id=kwargs["session_id"], exit_code=0, state=state,
            )

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = fake_spawn

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True
        mock_db.list_recent_automation_decisions.return_value = [
            {
                "decided_at": 1779445200,
                "item_id": "#42",
                "decision": "approved last week",
            }
        ]

        repo = MagicMock()
        repo.name = "owner/repo"

        await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(captured_prompts) == 1
        assert "#42" in captured_prompts[0]
        assert "approved last week" in captured_prompts[0]
        # Lookup must use a since_ts (30-day window), not unbounded.
        mock_db.list_recent_automation_decisions.assert_called_once()
        assert (
            mock_db.list_recent_automation_decisions.call_args.kwargs.get(
                "since_ts"
            )
            is not None
        )
        # Lookup must be namespaced to dependabot_pr — otherwise a
        # `codeql_alert` decision in the same repo would render into
        # this Dependabot prompt as a prior PR decision.
        assert (
            mock_db.list_recent_automation_decisions.call_args.kwargs.get(
                "operation"
            )
            == "dependabot_pr"
        )

    def test_prompt_renders_context_snippet_for_stale_check(self) -> None:
        """The prompt tells the agent to act on a prior decision only
        when circumstances haven't materially changed (different
        version bump, CI flipped). To enforce that, the rendered entry
        must include the stored context (the original question
        snippet) so the agent can compare prior bump/CI state with
        the current PR. Without context, the rule is unenforceable."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prior = [
            {
                "decided_at": 1779445200,
                "item_id": "#60",
                "decision": "yes merge it",
                "context": "PR #60 actions/upload-artifact 4->7 (CI green)",
            },
        ]
        prompt = pipeline._build_prompt(
            repo="o/r", session_id="s1", prior_decisions=prior,
        )
        # Question snippet must appear so the agent can detect a
        # force-pushed PR that swapped the version under #60.
        assert "actions/upload-artifact 4->7" in prompt
        # And the rule must explicitly point the agent at the snippet
        # so it knows to compare, not just decoration.
        assert "prior-question snippet" in prompt.lower()

    def test_prompt_handles_missing_context_gracefully(self) -> None:
        """Older rows (written before context was stored) lack the
        snippet. The render must not produce 'prior question: ' with
        an empty tail, or worse a literal 'None' — degrade to the
        item+answer-only form."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prior = [
            {
                "decided_at": 1779445200,
                "item_id": "#60",
                "decision": "merge",
                "context": None,
            },
        ]
        prompt = pipeline._build_prompt(
            repo="o/r", session_id="s1", prior_decisions=prior,
        )
        assert "#60" in prompt
        assert '"merge"' in prompt
        assert "prior question: " not in prompt
        assert "None" not in prompt.split("Prior operator decisions")[1].split(
            "**Use these"
        )[0]


class TestSecopsEdgeCasePromptDirectives:
    """The prompt must cover the two edge cases the agent reliably
    fumbles in production: (1) repos with no CI configured, where
    the 'auto-merge with passing CI' gate cannot be evaluated; and
    (2) CI failing on a check unrelated to the PR's package, where
    patch PRs would otherwise sit stuck for weeks."""

    def test_prompt_covers_no_ci_configured_case(self) -> None:
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")
        # Must call out the missing-workflows signal explicitly so the
        # agent can detect the case (not guess it).
        assert ".github/workflows" in prompt
        assert "No CI configured" in prompt or "no CI" in prompt.lower()
        # Must instruct one consolidated question, not one per PR.
        assert "consolidated" in prompt.lower()

    def test_prompt_covers_unrelated_ci_failure_case(self) -> None:
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")
        # Must instruct the agent to surface the pre-existing failure,
        # not silently leave patches stuck.
        assert "unrelated" in prompt.lower()
        assert "pre-existing" in prompt.lower() or "stuck" in prompt.lower()
        # Must reference the diagnostic command the agent runs to extract
        # the root cause, otherwise it's just vague prose.
        assert "gh run view" in prompt or "log-failed" in prompt.lower()

    def test_prompt_enforces_scope_discipline(self) -> None:
        """Production saw the agent invent options like 'batch into a
        single review PR' that were never in the policy ladder. Prompt
        must explicitly forbid this."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )
        prompt = pipeline._build_prompt(repo="o/r", session_id="s1")
        # Must name the freelance options the agent has been observed
        # offering so the prohibition is concrete, not abstract.
        assert "batch" in prompt.lower()
        # Must establish the three-exit ladder so the agent has a clear
        # mental model of legitimate actions.
        assert (
            "three legitimate exits" in prompt.lower()
            or "three exits" in prompt.lower()
        )

