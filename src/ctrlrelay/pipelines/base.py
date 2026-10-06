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
    # The agent process's exit code, when the result came from a session
    # that actually ran. `None` means "not applicable or not known" —
    # never 0, because exiting 0 and writing no checkpoint is the exact
    # case this field exists to make visible (#174).
    exit_code: int | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


def failure_text(result: PipelineResult) -> str:
    """The operator-facing reason a run failed.

    Every failure alert goes through this. That is the point: the fault
    in #174 was reachable from four separate ``result.error or
    result.summary`` expressions, so fixing the one named in the report
    would have left the same useless alert reachable three other ways.

    ``error`` used to win over ``summary`` unconditionally. A pipeline
    whose agent exited 0 with empty stderr set
    ``summary="No checkpoint state returned"`` and then
    ``error="Unknown error"`` one line below it, so the alert said
    `Unknown error` while the real reason sat in the field that lost.
    Both are kept now.

    And when nothing was recorded, this does not manufacture a string
    that reads like a reason. It says what was expected and did not
    arrive, which is a different fact from a crash — the distinction the
    operator needs first and the one `Unknown error` destroyed.
    """
    parts: list[str] = []
    for candidate in (result.summary, result.error):
        if not candidate:
            continue
        text = candidate.strip()
        # `error` frequently restates `summary` verbatim; printing it
        # twice reads like two separate faults.
        if text and text not in parts:
            parts.append(text)

    # Always stated when known, because "exited 0" is what separates
    # "the agent crashed" from "the agent finished and wrote nothing",
    # and those have different causes and different fixes.
    exit_note = (
        f" (agent exit code {result.exit_code})"
        if result.exit_code is not None
        else ""
    )

    if not parts:
        return (
            "failed with no reason recorded: expected a checkpoint state "
            "or a stderr message, got neither" + exit_note
        )
    return " - ".join(parts) + exit_note


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
