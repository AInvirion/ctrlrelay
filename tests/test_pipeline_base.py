"""Tests for pipeline base protocol."""

from pathlib import Path


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
