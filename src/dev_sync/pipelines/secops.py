"""Secops pipeline for security triage across repos."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_sync.core.checkpoint import CheckpointStatus
from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult
from dev_sync.core.github import GitHubCLI
from dev_sync.core.obs import get_logger, hash_text, log_event
from dev_sync.core.state import StateDB
from dev_sync.core.worktree import WorktreeManager
from dev_sync.dashboard.client import DashboardClient, EventPayload
from dev_sync.pipelines.base import PipelineContext, PipelineResult
from dev_sync.transports.base import Transport

_logger = get_logger("pipeline.secops")


@dataclass
class SecopsPipeline:
    """Security operations pipeline for daily triage."""

    dispatcher: ClaudeDispatcher
    github: GitHubCLI
    worktree: WorktreeManager
    dashboard: DashboardClient | None
    state_db: StateDB
    transport: Transport | None

    name: str = "secops"

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Run secops on a single repo."""
        prompt = self._build_prompt(ctx.repo)

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
        )

        return self._session_to_result(result)

    async def resume(self, ctx: PipelineContext, answer: str) -> PipelineResult:
        """Resume blocked secops with user answer."""
        prompt = f"User answered: {answer}\n\nContinue from where you left off."

        log_event(
            _logger,
            "dev.session.resumed",
            session_id=ctx.session_id,
            repo=ctx.repo,
            issue_number=ctx.issue_number,
            pipeline=self.name,
            resume_session_id=ctx.session_id,
            answer_length=len(answer),
            answer_hash=hash_text(answer),
        )

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
            resume_session_id=ctx.session_id,
        )

        return self._session_to_result(result)

    def _build_prompt(self, repo: str) -> str:
        """Build the secops prompt."""
        return f"""Execute security operations for repository {repo}.

1. Run /gh-dashboard to get current security status
2. For each Dependabot alert or security PR:
   - Review the severity and impact
   - If patch/minor with green CI, auto-merge
   - If major or unclear, use checkpoint.blocked() to ask for guidance
3. Summarize actions taken

Use checkpoint.done() when complete with summary of actions.
Use checkpoint.blocked() if you need human input.
Use checkpoint.failed() if something goes wrong."""

    def _session_to_result(self, result: SessionResult) -> PipelineResult:
        """Convert SessionResult to PipelineResult."""
        if result.state is None:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="No checkpoint state returned",
                error=result.stderr or "Unknown error",
            )

        if result.state.status == CheckpointStatus.DONE:
            return PipelineResult(
                success=True,
                session_id=result.session_id,
                summary=result.state.summary or "Completed",
                outputs=result.state.outputs,
            )

        if result.state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="Blocked on user input",
                blocked=True,
                question=result.state.question,
            )

        return PipelineResult(
            success=False,
            session_id=result.session_id,
            summary="Failed",
            error=result.state.error,
        )


async def run_secops_all(
    repos: list[Any],
    dispatcher: ClaudeDispatcher,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
) -> list[PipelineResult]:
    """Run secops pipeline on all configured repos."""
    results = []

    pipeline = SecopsPipeline(
        dispatcher=dispatcher,
        github=github,
        worktree=worktree,
        dashboard=dashboard,
        state_db=state_db,
        transport=transport,
    )

    for repo_config in repos:
        repo = repo_config.name
        session_id = f"secops-{repo.replace('/', '-')}-{uuid.uuid4().hex[:8]}"

        if not state_db.acquire_lock(repo, session_id):
            results.append(PipelineResult(
                success=False,
                session_id=session_id,
                summary=f"Could not acquire lock for {repo}",
                error="Repository locked by another session",
            ))
            continue

        try:
            await worktree.ensure_bare_repo(repo)
            worktree_path = await worktree.create_worktree(repo, session_id)

            context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
            if context_path.exists():
                worktree.symlink_context(worktree_path, context_path)

            state_file = worktree_path / ".dev-sync" / "state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)

            ctx = PipelineContext(
                session_id=session_id,
                repo=repo,
                worktree_path=worktree_path,
                context_path=context_path,
                state_file=state_file,
            )

            state_db.execute(
                """INSERT INTO sessions (id, pipeline, repo, worktree_path, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, "secops", repo, str(worktree_path), "running", int(time.time())),
            )
            state_db.commit()

            result = await pipeline.run(ctx)
            results.append(result)

            status = "done" if result.success else ("blocked" if result.blocked else "failed")
            state_db.execute(
                "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
                (status, result.summary, int(time.time()), session_id),
            )
            state_db.commit()

            if dashboard and result.success:
                await dashboard.push_event(EventPayload(
                    level="info",
                    pipeline="secops",
                    repo=repo,
                    message=result.summary,
                    session_id=session_id,
                ))

            worktree.remove_context_symlink(worktree_path)
            await worktree.remove_worktree(repo, session_id)

        except Exception as e:
            state_db.execute(
                "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
                ("failed", f"Error: {e}", int(time.time()), session_id),
            )
            state_db.commit()
            results.append(PipelineResult(
                success=False,
                session_id=session_id,
                summary=f"Error processing {repo}",
                error=str(e),
            ))

        finally:
            state_db.release_lock(repo, session_id)

    return results
