"""Base protocol and types for pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class PipelineContext:
    """Context for a pipeline execution."""

    session_id: str
    repo: str
    worktree_path: Path
    context_path: Path
    state_file: Path
    issue_number: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Result of a pipeline execution."""

    success: bool
    session_id: str
    summary: str
    blocked: bool = False
    question: str | None = None
    error: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Pipeline(Protocol):
    """Protocol for pipeline implementations."""

    name: str

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Execute the pipeline."""
        ...

    async def resume(
        self, ctx: PipelineContext, answer: str
    ) -> PipelineResult:
        """Resume a blocked pipeline with user answer."""
        ...
