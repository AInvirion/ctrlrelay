"""Task pipeline for non-PR GitHub issues.

A ``task``-labeled issue routes here instead of the dev pipeline. The
agent is told to do the work described (run a build, investigate a
failure, check a config, etc.) and hand back its findings, which the
relay posts as an issue comment. No branch, no PR. Useful when the
outcome is information rather than code changes.

Shared pieces with the dev pipeline:
- Per-repo state_db lock so secops / dev / task runs on the same repo
  serialize.
- Worktree on the repo's default branch (read-only work; the agent
  can still invoke tools, compile, run tests).
- BLOCKED → pending_resumes persistence + sweeper resume, identical
  to dev/secops, so a Telegram reply after an exit still lands.

Intentionally NOT shared with dev:
- No branch creation or PR handoff.
- No claim comment (the report-comment posted on exit serves as the
  equivalent "agent touched this" signal). Since #219 the relay posts
  it from the agent's DONE outputs; the agent has no GitHub access.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctrlrelay.core.checkpoint import CheckpointStatus
from ctrlrelay.core.dispatcher import AgentAdapter, SessionResult
from ctrlrelay.core.events import EventKind, emit_event, emit_outcome
from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, hash_text, log_event
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager
from ctrlrelay.dashboard.client import DashboardClient, EventPayload
from ctrlrelay.pipelines.base import PipelineContext, PipelineResult
from ctrlrelay.transports.base import Transport

DEFAULT_MAX_BLOCKED_ROUNDS = 5

_logger = get_logger("pipeline.task")


def _question_for_persist(session_id: str, result: PipelineResult) -> str:
    """Fallback question text when the agent signals BLOCKED with no
    question body — ensures persisted rows are always non-empty so an
    orphan Telegram reply has something to match on."""
    q = (result.question or "").strip()
    if q:
        return q
    return (
        f"Session {session_id} is blocked but did not include a "
        "question. Reply with guidance to resume."
    )


@dataclass
class TaskPipeline:
    """Task pipeline: agent does the work, posts a report-comment,
    signals DONE. No branch, no PR."""

    dispatcher: AgentAdapter
    github: GitHubCLI
    worktree: WorktreeManager
    dashboard: DashboardClient | None
    state_db: StateDB
    transport: Transport | None

    name: str = "task"

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        prompt = self._build_prompt(
            ctx.repo, ctx.issue_number, ctx.extra,
            session_id=ctx.session_id,
            state_file=ctx.state_file,
        )
        result = await self._spawn(ctx, prompt, resume=False)
        return await self._post_report(ctx, self._session_to_result(result))

    async def resume(self, ctx: PipelineContext, answer: str) -> PipelineResult:
        # The checkpoint contract is restated here, not just in the
        # initial prompt (#173). A resumed agent that is told "continue"
        # and nothing else can do its work, exit 0, write no state, and
        # be indistinguishable from one that crashed - which is what
        # happened on 2026-09-16, after three of four pull requests had
        # already been merged.
        contract = self._checkpoint_contract(
            str(ctx.state_file), ctx.session_id
        )
        prompt = (
            f"User answered: {answer}\n\n"
            "Continue from where you left off.\n\n"
            f"{contract}"
        )
        resume_uuid = self.state_db.get_agent_session_id(ctx.session_id)
        log_event(
            _logger,
            "task.session.resumed",
            session_id=ctx.session_id,
            repo=ctx.repo,
            issue_number=ctx.issue_number,
            pipeline=self.name,
            resume_session_id=resume_uuid,
            answer_length=len(answer),
            answer_hash=hash_text(answer),
        )
        result = await self._spawn(ctx, prompt, resume=True)
        return await self._post_report(ctx, self._session_to_result(result))

    async def _post_report(
        self, ctx: PipelineContext, result: PipelineResult
    ) -> PipelineResult:
        """Post the agent's report on the issue, as the relay (#219).

        The agent used to post it with ``gh issue comment`` under the
        operator's login. It has no GitHub access now, so it hands the
        report over in its DONE outputs and the relay posts it, with
        mentions disabled and the length capped.

        A DONE without a report is a failure, not a quiet success: the
        report is the task lane's whole output, and "done" with nothing
        on the issue is exactly what an operator cannot tell from a
        crash.
        """
        if not result.success:
            return result
        from ctrlrelay.core.publish import (
            COMMENT_MAX_CHARS,
            sanitize_untrusted_text,
        )

        raw = result.outputs.get("report")
        report = raw.strip() if isinstance(raw, str) else ""
        if not report or ctx.issue_number is None:
            return dataclasses.replace(
                result,
                success=False,
                summary="Task agent signalled DONE without a report",
                error="no report in the DONE checkpoint outputs",
            )
        body = sanitize_untrusted_text(report, max_chars=COMMENT_MAX_CHARS)
        body += (
            "\n\n---\nPosted by ctrlrelay (session "
            f"`{ctx.session_id}`) from the task agent's report."
        )
        try:
            await self.github.comment_on_issue(
                repo=ctx.repo, issue_number=ctx.issue_number, body=body,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                _logger,
                "task.report.post_failed",
                session_id=ctx.session_id,
                repo=ctx.repo,
                issue_number=ctx.issue_number,
                error_type=type(e).__name__,
                error=str(e)[:200],
            )
            return dataclasses.replace(
                result,
                success=False,
                summary="Task finished but ctrlrelay could not post the report",
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )
        log_event(
            _logger,
            "task.report.posted",
            session_id=ctx.session_id,
            repo=ctx.repo,
            issue_number=ctx.issue_number,
            report_len=len(report),
        )
        return result

    async def _spawn(
        self,
        ctx: PipelineContext,
        prompt: str,
        *,
        resume: bool,
    ) -> SessionResult:
        resume_uuid: str | None = None
        if resume:
            resume_uuid = self.state_db.get_agent_session_id(ctx.session_id)

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
            resume_session_id=resume_uuid,
            repo=ctx.repo,
            issue_number=ctx.issue_number,
            # #219: the relay posts the report; the agent has no GitHub
            # credentials.
            github_access=False,
        )

        if result.agent_session_id:
            try:
                self.state_db.set_agent_session_id(
                    ctx.session_id, result.agent_session_id
                )
            except Exception:
                pass
        return result

    def _build_prompt(
        self,
        repo: str,
        issue_number: int | None,
        extra: dict,
        *,
        session_id: str = "",
        state_file: Path | None = None,
    ) -> str:
        state_file_path = str(state_file) if state_file else "/tmp/state.json"
        title = extra.get("issue_title", "") if extra else ""
        body = extra.get("issue_body", "") if extra else ""

        return f"""You are the ctrlrelay task pipeline. Handle a
non-PR task for issue #{issue_number} in {repo}.

**Issue title:** {title}

**Issue body:**
{body}

## What "task" means here

This issue is tagged as a task, which means the operator wants you
to DO the work described and REPORT your findings. You are NOT
expected to write code, create a branch, or open a PR. Typical task
shapes: run a build and report errors, investigate a failure,
check a config, summarize state of something.

## Your workflow

1. Read the issue above. You have a clean worktree of {repo}'s
   default branch at the current directory — files are real; tools
   like `git`, `npm`, `uv`, `cargo`, etc. are on PATH.
2. Do the work. Run commands, read files. You can write files in the
   worktree if it helps your investigation, but those changes will
   be discarded at the end — do not rely on them persisting.
3. Write your findings as the `report` in the DONE checkpoint below.
   The orchestrator posts it as a comment on issue #{issue_number}
   after you exit. Keep it concise but concrete: what you found, what
   commands you ran, any snippets the operator should see. No
   unnecessary throat-clearing.
4. Signal DONE with a short summary as well (the Telegram
   notification the operator sees is based on it, so make it useful).

This session has no GitHub credentials: `gh` and any `git` command
that talks to GitHub will fail. Do not try to post the comment
yourself and do not look for credentials.

If you hit genuine ambiguity that needs the operator's input,
signal BLOCKED with a specific question — same pattern as the dev
pipeline. Don't BLOCK on things you can figure out yourself.

## Signaling completion

{self._checkpoint_contract(state_file_path, session_id)}"""

    def _checkpoint_contract(
        self, state_file_path: str, session_id: str
    ) -> str:
        """How the agent signals how it stopped.

        Extracted so the RESUME prompt can carry it too (#173). The
        initial prompt spent ~30 lines establishing this; the resume
        prompt was 58 characters and said only "continue", so a resumed
        agent could do its work, exit 0, write no state, and be
        indistinguishable from one that crashed.

        Measured case, 2026-09-16:
        secops-AInvirion-Product-Telemetry-bbb99925 resumed after an
        "Approved", ran 27.4s, exited 0 and wrote no checkpoint - having
        in fact merged three of four pull requests. The operator was told
        the resume failed, which was wrong in both directions: most of
        the work had landed, and the one thing outstanding was never
        named.

        Per-pipeline rather than shared, because the three contracts
        differ in substance and not only wording: the dev DONE block
        carries pr_url and pr_number outputs, and each names different
        conditions for DONE. One shared block would have quietly changed
        what two of them promise.
        """
        # Quoted for the shell rather than hand-wrapped in single quotes
        # (#191). With hand quoting, an apostrophe in the path either
        # closed the quote early (a syntax error, so no checkpoint at all)
        # or, paired, parsed fine and wrote to a different path:
        # /tmp/a'b'c/state.json landed in /tmp/abc/state.json.
        #
        # The directory is computed here rather than with `$(dirname)`,
        # because command substitution strips trailing newlines from a
        # directory name, and `--` stops a relative path starting with
        # `-` being read as an option. The redirect target needs no `--`:
        # the shell takes the word after `>` as a filename.
        quoted_path = shlex.quote(state_file_path)
        quoted_dir = shlex.quote(str(Path(state_file_path).parent))
        return f"""**CRITICAL**: Before exiting, write a checkpoint file.

STATE_FILE: {state_file_path}
SESSION_ID: {session_id}

**DONE** (task complete). The report is markdown and may span lines;
it goes inside a Python raw triple-quoted string, so it must not contain
three single quotes in a row:
```bash
mkdir -p -- {quoted_dir}
python3 - {quoted_path} <<'CTRLRELAY_DONE'
import datetime, json, sys
state = {{
    "version": "1",
    "status":"DONE",
    "session_id": "{session_id}",
    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "summary": "<ONE-LINE SUMMARY>",
    "outputs": {{
        "report": r'''<REPORT>''',
    }},
}}
with open(sys.argv[1], "w") as f:
    json.dump(state, f)
CTRLRELAY_DONE
```

**BLOCKED** (need operator input):
```bash
mkdir -p -- {quoted_dir}
printf '{{"version":"1","status":"BLOCKED_NEEDS_INPUT",'\\
'"session_id":"{session_id}","timestamp":"%s","question":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<QUESTION>" > {quoted_path}
```

**FAILED** (genuine failure, no way forward):
```bash
mkdir -p -- {quoted_dir}
printf '{{"version":"1","status":"FAILED",'\\
'"session_id":"{session_id}","timestamp":"%s","error":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<ERROR>" > {quoted_path}
```
"""

    def _session_to_result(self, result: SessionResult) -> PipelineResult:
        state = result.state
        if state is None:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="Task session ended without a checkpoint",
                error="no checkpoint written",
            )
        if state.status == CheckpointStatus.DONE:
            return PipelineResult(
                success=True,
                session_id=result.session_id,
                summary=state.summary or "Task complete",
                # Only the report crosses over (#219). Anything else the
                # agent wrote stays out: a stray `pr_url` here is how the
                # cli decides to announce "PR ready".
                outputs=(
                    {"report": state.outputs["report"]}
                    if "report" in state.outputs
                    else {}
                ),
            )
        if state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT:
            return PipelineResult(
                success=False,
                blocked=True,
                session_id=result.session_id,
                summary="Blocked on user input",
                question=state.question,
            )
        if state.status == CheckpointStatus.FAILED:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="Task failed",
                # The `or "unknown failure"` that used to be here was
                # unreachable, not merely useless: CheckpointState's
                # validator refuses FAILED without a truthy `error`
                # (`not self.error` catches "" as well as None), so this
                # field is guaranteed non-empty here. Deleted rather than
                # replaced — a fallback for a case that cannot happen
                # reads as coverage to the next person (#174).
                error=state.error,
                exit_code=result.exit_code,
            )
        return PipelineResult(
            success=False,
            session_id=result.session_id,
            summary=f"Unexpected checkpoint status: {state.status}",
            error=f"unexpected status {state.status}",
        )


async def run_task_issue(
    repo: str,
    issue_number: int,
    dispatcher: AgentAdapter,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
    max_blocked_rounds: int = DEFAULT_MAX_BLOCKED_ROUNDS,
) -> PipelineResult:
    """Run the task pipeline for a single issue. Mirrors run_dev_issue's
    lock/worktree/BLOCKED-loop pattern minus the branch-and-PR work."""
    session_id = (
        f"task-{repo.replace('/', '-')}-{issue_number}-"
        f"{uuid.uuid4().hex[:8]}"
    )

    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo}",
            error="Repository locked by another session",
        )

    worktree_path: Path | None = None
    session_row_inserted = False

    try:
        issue = await github.get_issue(repo, issue_number)

        await worktree.ensure_bare_repo(repo)
        # Default branch, session-scoped name so cleanup is keyed to
        # this session only. Read-only intent (agent won't commit),
        # but the worktree is a real checkout so builds / test
        # runners work.
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
            issue_number=issue_number,
            extra={
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body", ""),
            },
        )

        state_db.execute(
            """INSERT INTO sessions
               (id, pipeline, repo, worktree_path, status, started_at,
                issue_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, "task", repo, str(worktree_path),
                "running", int(time.time()), issue_number,
            ),
        )
        state_db.commit()
        session_row_inserted = True
        emit_event(
            state_db, EventKind.DISPATCHED,
            lambda: {
                "repo": repo,
                "issue_number": issue_number,
                "session_id": session_id,
                "detail": {"pipeline": "task"},
            },
        )

        pipeline = TaskPipeline(
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=state_db,
            transport=transport,
        )
        result = await pipeline.run(ctx)

        rounds = 0
        while (
            result.blocked
            and transport is not None
            and rounds < max_blocked_rounds
        ):
            question = _question_for_persist(session_id, result)
            try:
                answer = await transport.ask(
                    question,
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                )
            except Exception as e:
                # Preserve blocked=True on transport failure so outer
                # persistence fires — matches the dev pipeline fix.
                result = PipelineResult(
                    success=False,
                    blocked=True,
                    session_id=session_id,
                    summary=f"Blocked session deferred (transport): {e}",
                    error=str(e),
                    question=question,
                    outputs=result.outputs,
                )
                break
            rounds += 1
            result = await pipeline.resume(ctx, answer)

        status = "done" if result.success else (
            "blocked" if result.blocked else "failed"
        )
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? "
            "WHERE id = ?",
            (status, result.summary, int(time.time()), session_id),
        )
        state_db.commit()

        if result.blocked:
            try:
                state_db.add_pending_resume(
                    session_id=session_id,
                    pipeline="task",
                    repo=repo,
                    question=_question_for_persist(session_id, result),
                )
            except Exception as e:
                log_event(
                    _logger,
                    "task.pending_resume.insert_failed",
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )

        emit_outcome(
            state_db,
            lambda: {
                "status": status,
                "pipeline": "task",
                "repo": repo,
                "issue_number": issue_number,
                "session_id": session_id,
                "exit_code": result.exit_code,
                "question": result.question,
            },
        )

        if dashboard and result.success:
            await dashboard.push_event(EventPayload(
                level="info",
                pipeline="task",
                repo=repo,
                message=result.summary,
                session_id=session_id,
                details={"issue_number": issue_number},
            ))

        return result

    except Exception as e:
        if session_row_inserted:
            state_db.execute(
                "UPDATE sessions SET status = ?, summary = ?, "
                "ended_at = ? WHERE id = ?",
                ("failed", f"Error: {e}", int(time.time()), session_id),
            )
            state_db.commit()
            emit_outcome(
                state_db,
                lambda: {
                    "status": "failed",
                    "pipeline": "task",
                    "repo": repo,
                    "issue_number": issue_number,
                    "session_id": session_id,
                },
            )
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Error processing task issue #{issue_number}",
            error=str(e),
        )

    finally:
        # Task workflow: worktree is ephemeral (no PR depends on it).
        # Tear down on every outcome — even BLOCKED, because a task
        # resume will re-create the worktree from scratch anyway
        # (no in-progress branch state to preserve, unlike dev).
        #
        # Timeout + CancelledError handling mirrors secops: the repo
        # lock is shared across task/dev/secops, so a cancel during
        # `remove_worktree` that skipped release_lock would wedge ALL
        # three pipelines for that repo until the row was manually
        # cleared — bad. Release the lock early on cancel and
        # re-raise; on timeout, log and fall through to the normal
        # lock release below.
        if worktree_path is not None:
            try:
                worktree.remove_context_symlink(worktree_path)
            except Exception as cleanup_exc:
                log_event(
                    _logger,
                    "task.cleanup.symlink_failed",
                    session_id=session_id,
                    repo=repo,
                    error_type=type(cleanup_exc).__name__,
                    error=str(cleanup_exc)[:200],
                )
            try:
                # Timeout matches secops (130s) — a full worktree prune
                # can take up to ~120s on a slow volume.
                await asyncio.wait_for(
                    worktree.remove_worktree(repo, session_id),
                    timeout=130.0,
                )
            except asyncio.TimeoutError:
                log_event(
                    _logger,
                    "task.cleanup.worktree_timeout",
                    session_id=session_id,
                    repo=repo,
                    worktree_path=str(worktree_path),
                )
            except asyncio.CancelledError:
                log_event(
                    _logger,
                    "task.cleanup.worktree_cancelled_mid_shutdown",
                    session_id=session_id,
                    repo=repo,
                    worktree_path=str(worktree_path),
                )
                try:
                    state_db.release_lock(repo, session_id)
                except Exception:
                    pass
                raise
            except Exception as cleanup_exc:
                log_event(
                    _logger,
                    "task.cleanup.worktree_failed",
                    session_id=session_id,
                    repo=repo,
                    worktree_path=str(worktree_path),
                    error_type=type(cleanup_exc).__name__,
                    error=str(cleanup_exc)[:200],
                )
        try:
            state_db.release_lock(repo, session_id)
        except Exception as lock_exc:
            log_event(
                _logger,
                "task.cleanup.lock_release_failed",
                session_id=session_id,
                repo=repo,
                error_type=type(lock_exc).__name__,
                error=str(lock_exc)[:200],
            )


async def resume_task_from_pending(
    session_id: str,
    repo: str,
    answer: str,
    dispatcher: AgentAdapter,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
    max_blocked_rounds: int = DEFAULT_MAX_BLOCKED_ROUNDS,
) -> PipelineResult:
    """Resume a BLOCKED task session using an answer that arrived
    via Telegram after ``run_task_issue`` exited.

    Rebuilds the worktree from scratch (task worktrees are torn
    down on every outcome, including BLOCKED — see the finally block
    in run_task_issue). Reuses Claude's session UUID if captured so
    the resumed prompt has access to the prior conversation."""
    session_row = state_db.get_session_row(session_id)
    if session_row is None:
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=(
                f"Cannot resume task session {session_id}: row missing "
                "from sessions table"
            ),
            error="session_row_missing",
        )
    issue_number = session_row.get("issue_number")
    if issue_number is None:
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=(
                f"Cannot resume task session {session_id}: "
                "issue_number missing on sessions row"
            ),
            error="session_issue_number_missing",
        )

    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo} to resume task",
            error="Repository locked by another session",
        )

    worktree_path: Path | None = None

    try:
        await worktree.ensure_bare_repo(repo)
        worktree_path = await worktree.create_worktree(repo, session_id)

        context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
        if context_path.exists():
            worktree.symlink_context(worktree_path, context_path)

        state_file = worktree_path / ".ctrlrelay" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)

        issue = await github.get_issue(repo, int(issue_number))

        ctx = PipelineContext(
            session_id=session_id,
            repo=repo,
            worktree_path=worktree_path,
            context_path=context_path,
            state_file=state_file,
            issue_number=int(issue_number),
            extra={
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body", ""),
            },
        )

        state_db.execute(
            "UPDATE sessions SET status = ?, ended_at = NULL WHERE id = ?",
            ("running", session_id),
        )
        state_db.commit()

        pipeline = TaskPipeline(
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=state_db,
            transport=transport,
        )
        result = await pipeline.resume(ctx, answer)

        rounds = 0
        while (
            result.blocked
            and transport is not None
            and rounds < max_blocked_rounds
        ):
            question = _question_for_persist(session_id, result)
            try:
                next_answer = await transport.ask(
                    question,
                    session_id=session_id,
                    repo=repo,
                    issue_number=int(issue_number),
                )
            except Exception as e:
                result = PipelineResult(
                    success=False,
                    blocked=True,
                    session_id=session_id,
                    summary=(
                        f"Blocked session deferred during resume "
                        f"(transport): {e}"
                    ),
                    error=str(e),
                    question=question,
                    outputs=result.outputs,
                )
                break
            rounds += 1
            result = await pipeline.resume(ctx, next_answer)

        status = "done" if result.success else (
            "blocked" if result.blocked else "failed"
        )
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? "
            "WHERE id = ?",
            (status, result.summary, int(time.time()), session_id),
        )
        state_db.commit()

        if result.blocked:
            try:
                state_db.add_pending_resume(
                    session_id=session_id,
                    pipeline="task",
                    repo=repo,
                    question=_question_for_persist(session_id, result),
                )
            except Exception as e:
                log_event(
                    _logger,
                    "task.pending_resume.reblock_insert_failed",
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )

        emit_outcome(
            state_db,
            lambda: {
                "status": status,
                "pipeline": "task",
                "repo": repo,
                "issue_number": issue_number,
                "session_id": session_id,
                "exit_code": result.exit_code,
                "question": result.question,
            },
        )
        return result

    except Exception as e:
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? "
            "WHERE id = ?",
            ("failed", f"Resume error: {e}", int(time.time()), session_id),
        )
        state_db.commit()
        emit_outcome(
            state_db,
            lambda: {
                "status": "failed",
                "pipeline": "task",
                "repo": repo,
                "issue_number": issue_number,
                "session_id": session_id,
            },
        )
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Error resuming task session {session_id} on {repo}",
            error=str(e),
        )

    finally:
        # Same cancel/timeout handling as run_task_issue's finally —
        # shared repo lock means a cancel here would wedge
        # task/dev/secops on this repo if release_lock was skipped.
        if worktree_path is not None:
            try:
                worktree.remove_context_symlink(worktree_path)
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    worktree.remove_worktree(repo, session_id),
                    timeout=130.0,
                )
            except asyncio.TimeoutError:
                log_event(
                    _logger,
                    "task.resume.cleanup.worktree_timeout",
                    session_id=session_id,
                    repo=repo,
                    worktree_path=str(worktree_path),
                )
            except asyncio.CancelledError:
                log_event(
                    _logger,
                    "task.resume.cleanup.worktree_cancelled_mid_shutdown",
                    session_id=session_id,
                    repo=repo,
                    worktree_path=str(worktree_path),
                )
                try:
                    state_db.release_lock(repo, session_id)
                except Exception:
                    pass
                raise
            except Exception:
                pass
        try:
            state_db.release_lock(repo, session_id)
        except Exception as lock_exc:
            log_event(
                _logger,
                "task.resume.cleanup.lock_release_failed",
                session_id=session_id,
                repo=repo,
                error_type=type(lock_exc).__name__,
                error=str(lock_exc)[:200],
            )
