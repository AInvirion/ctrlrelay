"""Tests for pipeline base protocol."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.checkpoint import CheckpointStatus


class TestPipelineProtocol:
    def test_pipeline_context_has_required_fields(self) -> None:
        """PipelineContext should have all required fields."""
        from ctrlrelay.pipelines.base import PipelineContext

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=Path("/tmp/worktree"),
            context_path=Path("/tmp/context/CLAUDE.md"),
            state_file=Path("/tmp/state.json"),
        )

        assert ctx.session_id == "sess-123"
        assert ctx.repo == "owner/repo"

    def test_pipeline_result_has_required_fields(self) -> None:
        """PipelineResult should capture execution outcome."""
        from ctrlrelay.pipelines.base import PipelineResult

        result = PipelineResult(
            success=True,
            session_id="sess-123",
            summary="Completed successfully",
        )

        assert result.success
        assert result.summary == "Completed successfully"


class TestFailureText:
    """The alert text an operator actually reads (#174).

    The incident: session `secops-AInvirion-Product-Telemetry-bbb99925`
    sent `Unknown error` on 2026-09-16 while the real reason — the agent
    exited 0 without writing a checkpoint — had already been computed one
    line earlier and was then discarded by `result.error or
    result.summary`.
    """

    def _secops(self):
        from unittest.mock import MagicMock

        from ctrlrelay.pipelines.secops import SecopsPipeline

        return SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=MagicMock(),
            state_db=MagicMock(),
            transport=MagicMock(),
        )

    def _dev(self):
        from unittest.mock import MagicMock

        from ctrlrelay.pipelines.dev import DevPipeline

        return DevPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=MagicMock(),
            state_db=MagicMock(),
            transport=MagicMock(),
        )

    def test_the_2026_09_16_alert_names_the_reason_and_the_exit_code(self) -> None:
        """Drives the whole operator path, not just the formatter.

        Agent exits 0, writes no checkpoint, stderr empty — exactly the
        incident. Asserted as the complete sentence rather than as
        `"Unknown error" not in text`, because an absence assertion here
        would also pass for an empty alert.
        """
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import failure_text

        result = self._secops()._session_to_result(
            SessionResult(
                session_id="secops-AInvirion-Product-Telemetry-bbb99925",
                exit_code=0,
                state=None,
                stderr="",
            )
        )

        assert failure_text(result) == (
            "No checkpoint state returned (agent exit code 0)"
        )

    def test_dev_reports_the_same_reason_as_secops(self) -> None:
        """The fallback existed in both pipelines, so both are pinned."""
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import failure_text

        result = self._dev()._session_to_result(
            SessionResult(
                session_id="dev-owner-repo-1-abc",
                exit_code=0,
                state=None,
                stderr="",
            )
        )

        assert failure_text(result) == (
            "No checkpoint state returned (agent exit code 0)"
        )

    def test_a_real_stderr_is_kept_beside_the_summary(self) -> None:
        """The paired case, so the test fails both ways round.

        Without this, deleting the `error` field entirely would satisfy
        the test above — the opposite defect wearing the shape of a fix.
        """
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import failure_text

        result = self._secops()._session_to_result(
            SessionResult(
                session_id="sess",
                exit_code=1,
                state=None,
                stderr="  Traceback: boom\n",
            )
        )

        text = failure_text(result)
        assert "No checkpoint state returned" in text
        assert "Traceback: boom" in text
        assert text.endswith("(agent exit code 1)")

    def test_exit_code_zero_is_reported_rather_than_dropped(self) -> None:
        """`if result.exit_code:` would silently lose the 0.

        Exiting 0 is the whole diagnosis in this bug class, and 0 is
        falsy — so the one value that matters most is the one a truthiness
        test discards.
        """
        from ctrlrelay.pipelines.base import PipelineResult, failure_text

        text = failure_text(
            PipelineResult(
                success=False,
                session_id="sess",
                summary="No checkpoint state returned",
                exit_code=0,
            )
        )

        assert text == "No checkpoint state returned (agent exit code 0)"

    def test_an_unknown_exit_code_is_omitted_not_guessed(self) -> None:
        """`None` means not known, and says nothing rather than "0"."""
        from ctrlrelay.pipelines.base import PipelineResult, failure_text

        text = failure_text(
            PipelineResult(
                success=False,
                session_id="sess",
                summary="Could not acquire lock for owner/repo",
                error="Repository locked by another session",
            )
        )

        assert text == (
            "Could not acquire lock for owner/repo - "
            "Repository locked by another session"
        )

    def test_nothing_recorded_says_what_was_expected(self) -> None:
        """The case `Unknown error` used to occupy.

        A manufactured string reads like a reason and carries none, so
        the replacement names the two things that did not arrive.
        """
        from ctrlrelay.pipelines.base import PipelineResult, failure_text

        text = failure_text(
            PipelineResult(
                success=False,
                session_id="sess",
                summary="",
                error=None,
                exit_code=0,
            )
        )

        assert text == (
            "failed with no reason recorded: expected a checkpoint state "
            "or a stderr message, got neither (agent exit code 0)"
        )

    def test_an_error_restating_the_summary_is_not_printed_twice(self) -> None:
        """secops' exception path fills both fields with the same text."""
        from ctrlrelay.pipelines.base import PipelineResult, failure_text

        text = failure_text(
            PipelineResult(
                success=False,
                session_id="sess",
                summary="TimeoutError: fetch timed out",
                error="TimeoutError: fetch timed out",
                exit_code=None,
            )
        )

        assert text == "TimeoutError: fetch timed out"

    def test_a_failed_checkpoint_cannot_omit_its_error(self) -> None:
        """Why task.py needs no fallback at all.

        The `or "unknown failure"` deleted there was **unreachable**, not
        a live fault: this validator refuses FAILED without a truthy
        `error`, and `not self.error` rejects "" as well as None. This
        pins the invariant the deletion relies on, so relaxing the
        validator fails here rather than silently reviving the need for
        a fallback nobody would re-add.

        Deliberately not a test of the deleted branch — a test for code
        that cannot run passes forever and reads as coverage.
        """
        import pytest
        from pydantic import ValidationError

        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus

        for absent in (None, ""):
            with pytest.raises(ValidationError, match="error is required"):
                CheckpointState(
                    version="1",
                    status=CheckpointStatus.FAILED,
                    session_id="task-owner-repo-1-abc",
                    error=absent,
                )

    def test_a_task_failure_reports_the_agents_own_error(self) -> None:
        """The reachable path: the agent's message reaches the alert."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import failure_text
        from ctrlrelay.pipelines.task import TaskPipeline

        result = TaskPipeline.__new__(TaskPipeline)._session_to_result(
            SessionResult(
                session_id="task-owner-repo-1-abc",
                exit_code=2,
                state=CheckpointState(
                    version="1",
                    status=CheckpointStatus.FAILED,
                    session_id="task-owner-repo-1-abc",
                    error="uv sync could not reach the index",
                ),
            )
        )

        assert failure_text(result) == (
            "Task failed - uv sync could not reach the index "
            "(agent exit code 2)"
        )


class TestEveryAlertGoesThroughFailureText:
    """Holds the call sites, which the formatter's own tests do not.

    Measured on 082de46: reverting the two `cli.py` alert sites to
    `result.error or result.summary` passed the **entire** 1019-test
    suite. The formatter was pinned and the thing that made the fix a fix
    — that every alert resolves the two fields the same way — was not.
    """

    @pytest.mark.asyncio
    async def test_a_failure_after_an_answer_names_the_reason(
        self, tmp_path: Path
    ) -> None:
        """Drives the operator path at pipelines/secops.py:685.

        The agent answers, resumes, exits 0 and writes no checkpoint —
        the incident's own shape, one round later. The alert must carry
        the reason and the exit code, not `Unknown error`.
        """
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        blocked = MagicMock()
        blocked.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        blocked.question = "Merge #1?"
        blocked.outputs = {}
        blocked.error = None

        # `resume` goes through `_spawn(..., resume=True)`, so both the
        # first run and the resume arrive as `spawn_session` calls — a
        # `resume_session` mock is never consulted and would have left
        # this test looping to max_blocked_rounds instead of failing.
        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = [
            SessionResult(session_id="sess-1", exit_code=0, state=blocked),
            # The resume exits 0 and writes nothing: state is None.
            SessionResult(
                session_id="sess-1", exit_code=0, state=None, stderr=""
            ),
        ]

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "wt"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True

        sent: list[str] = []
        transport = AsyncMock()
        transport.ask.return_value = "yes, merge it"
        transport.send.side_effect = lambda text, **kw: sent.append(text)

        repo = MagicMock(local_path=tmp_path / "repo1")
        repo.name = "owner/repo1"

        await run_secops_all(
            repos=[repo],
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=transport,
            contexts_dir=tmp_path / "contexts",
        )

        failures = [t for t in sent if "Failed after your answer" in t]
        assert len(failures) == 1, sent
        assert "No checkpoint state returned" in failures[0]
        assert "(agent exit code 0)" in failures[0]

    def test_no_module_reconstructs_the_error_or_summary_precedence(self) -> None:
        """A structural guard over the whole package, so a fifth site
        cannot reintroduce the bug the other four carried.

        Read with `ast`, not a regex or a source slice: a substring
        search over this file would be satisfied by the expression
        appearing in a comment or a docstring — including the ones in
        this change explaining the fault — and could be disabled by
        writing prose nearby.
        """
        import ast

        offenders: list[str] = []
        root = Path(__file__).resolve().parent.parent / "src" / "ctrlrelay"
        files = sorted(root.rglob("*.py"))

        # Negative control: if the walk finds nothing to read, it would
        # report a clean result for the same reason a passing one does.
        assert len(files) > 20, f"only walked {len(files)} files"

        for path in files:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.BoolOp) or not isinstance(
                    node.op, ast.Or
                ):
                    continue
                attrs = [
                    v.attr for v in node.values if isinstance(v, ast.Attribute)
                ]
                if "error" in attrs and "summary" in attrs:
                    offenders.append(
                        f"{path.relative_to(root)}:{node.lineno}"
                    )

        assert offenders == [], (
            "these resolve `error`/`summary` by hand instead of calling "
            f"failure_text(), which is how #174 was reachable four ways: "
            f"{offenders}"
        )
