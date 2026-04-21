"""Secops pipeline for security triage across repos."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctrlrelay.core.checkpoint import CheckpointStatus
from ctrlrelay.core.dispatcher import AgentAdapter, SessionResult
from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, hash_text, log_event
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager
from ctrlrelay.dashboard.client import DashboardClient, EventPayload
from ctrlrelay.pipelines.base import PipelineContext, PipelineResult
from ctrlrelay.transports.base import Transport

_logger = get_logger("pipeline.secops")


@dataclass
class SecopsPipeline:
    """Security operations pipeline for daily triage."""

    dispatcher: AgentAdapter
    github: GitHubCLI
    worktree: WorktreeManager
    dashboard: DashboardClient | None
    state_db: StateDB
    transport: Transport | None

    name: str = "secops"

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Run secops on a single repo."""
        prompt = self._build_prompt(
            ctx.repo,
            session_id=ctx.session_id,
            state_file=ctx.state_file,
        )

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

    def _build_prompt(
        self,
        repo: str,
        session_id: str = "",
        state_file: Path | None = None,
    ) -> str:
        """Build the secops prompt."""
        state_file_path = str(state_file) if state_file else "/tmp/state.json"

        return f"""Execute security operations for repository {repo}.

1. Check Dependabot alerts:
   `gh api repos/{repo}/dependabot/alerts --jq '.[] | select(.state=="open")'`
2. Check security PRs:
   `gh pr list --repo {repo} --author "app/dependabot" --json number,title`
3. For each alert or PR:
   - Review the severity and impact
   - If patch/minor update with passing CI, merge the PR
   - If major or unclear, signal BLOCKED to ask for guidance
4. Summarize actions taken

## Signaling Completion

**CRITICAL**: Before exiting, you MUST write a checkpoint file to signal completion.

STATE_FILE: {state_file_path}
SESSION_ID: {session_id}

**DONE** (completed):
```bash
mkdir -p "$(dirname '{state_file_path}')"
printf '{{"version":"1","status":"DONE","session_id":"{session_id}",'\
'"timestamp":"%s","summary":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<SUMMARY>" > '{state_file_path}'
```

**BLOCKED** (need input):
```bash
mkdir -p "$(dirname '{state_file_path}')"
printf '{{"version":"1","status":"BLOCKED_NEEDS_INPUT",'\
'"session_id":"{session_id}","timestamp":"%s","question":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<QUESTION>" > '{state_file_path}'
```

**FAILED**:
```bash
mkdir -p "$(dirname '{state_file_path}')"
printf '{{"version":"1","status":"FAILED","session_id":"{session_id}",'\
'"timestamp":"%s","error":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<ERROR>" > '{state_file_path}'
```"""

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
    dispatcher: AgentAdapter,
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

        worktree_path: Path | None = None
        session_row_inserted = False
        session_final_state_written = False
        try:
            await worktree.ensure_bare_repo(repo)
            worktree_path = await worktree.create_worktree(repo, session_id)

            context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
            if context_path.exists():
                worktree.symlink_context(worktree_path, context_path)

            state_file = worktree_path / ".ctrlrelay" / "state.json"
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
            session_row_inserted = True

            result = await pipeline.run(ctx)
            results.append(result)

            status = "done" if result.success else ("blocked" if result.blocked else "failed")
            state_db.execute(
                "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
                (status, result.summary, int(time.time()), session_id),
            )
            state_db.commit()
            session_final_state_written = True

            if dashboard and result.success:
                await dashboard.push_event(EventPayload(
                    level="info",
                    pipeline="secops",
                    repo=repo,
                    message=result.summary,
                    session_id=session_id,
                ))

        except asyncio.CancelledError:
            # Scheduled secops interrupted mid-run (SIGTERM during a
            # scheduler.shutdown). Mark the session as cancelled so later
            # inspection doesn't see a phantom "running" row — but ONLY
            # if we hadn't already written a final state. Without this
            # guard, a cancel landing during the post-run dashboard push
            # would clobber a successful "done" status with "cancelled"
            # even though the work completed.
            if session_row_inserted and not session_final_state_written:
                try:
                    state_db.execute(
                        "UPDATE sessions SET status = ?, summary = ?, "
                        "ended_at = ? WHERE id = ?",
                        (
                            "cancelled",
                            "Cancelled during shutdown",
                            int(time.time()),
                            session_id,
                        ),
                    )
                    state_db.commit()
                except Exception:
                    pass
            raise

        except Exception as e:
            if session_row_inserted:
                state_db.execute(
                    "UPDATE sessions SET status = ?, summary = ?, "
                    "ended_at = ? WHERE id = ?",
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
            # Hold the per-repo lock THROUGH worktree cleanup so a
            # concurrent dev/secops run can't acquire the same repo and
            # race `git worktree prune` on the shared bare clone.
            # Cleanup is bounded by an asyncio.wait_for timeout so a
            # truly-stuck operation can't pin the lock forever — once
            # the timeout fires we release anyway, preferring "possibly
            # leaked git admin state (recoverable via `git worktree
            # prune`)" over "repo locked across daemon restart".
            #
            # No asyncio.shield here: `_run_git` kills its subprocess
            # synchronously on CancelledError so unshielded cleanup
            # unwinds fast and without leaking a stray git. Previously
            # the shield made the outer task cancel immediately while
            # the inner cleanup kept running untracked — leaking state.
            if worktree_path is not None:
                try:
                    worktree.remove_context_symlink(worktree_path)
                except Exception as cleanup_exc:
                    log_event(
                        _logger,
                        "secops.cleanup.symlink_failed",
                        session_id=session_id,
                        repo=repo,
                        error_type=type(cleanup_exc).__name__,
                        error=str(cleanup_exc)[:200],
                    )
                try:
                    # Timeout matches Scheduler's cancel budget (150s) — a
                    # full worktree prune can take up to 120s per
                    # WorktreeManager._run_git ceiling.
                    await asyncio.wait_for(
                        worktree.remove_worktree(repo, session_id),
                        timeout=130.0,
                    )
                except asyncio.TimeoutError:
                    log_event(
                        _logger,
                        "secops.cleanup.worktree_timeout",
                        session_id=session_id,
                        repo=repo,
                        worktree_path=str(worktree_path),
                    )
                except asyncio.CancelledError:
                    log_event(
                        _logger,
                        "secops.cleanup.worktree_cancelled_mid_shutdown",
                        session_id=session_id,
                        repo=repo,
                        worktree_path=str(worktree_path),
                    )
                    # Release the lock before propagating — leaving it
                    # held across a daemon restart is worse than the
                    # small race with a future scheduled run (the dev
                    # handler retries on lock conflict anyway).
                    try:
                        state_db.release_lock(repo, session_id)
                    except Exception:
                        pass
                    raise
                except Exception as cleanup_exc:
                    log_event(
                        _logger,
                        "secops.cleanup.worktree_failed",
                        session_id=session_id,
                        repo=repo,
                        worktree_path=str(worktree_path),
                        error_type=type(cleanup_exc).__name__,
                        error=str(cleanup_exc)[:200],
                    )

            # Release the lock AFTER cleanup attempt so new runs don't
            # race the prune. In the cancel path above we released early
            # and re-raised; here we handle the non-cancel paths.
            try:
                state_db.release_lock(repo, session_id)
            except Exception as lock_exc:
                log_event(
                    _logger,
                    "secops.cleanup.lock_release_failed",
                    session_id=session_id,
                    repo=repo,
                    error_type=type(lock_exc).__name__,
                    error=str(lock_exc)[:200],
                )

    return results
