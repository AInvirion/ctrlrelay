"""Run a configured code review over a branch before a PR is handed over.

The orchestrator runs this, not the agent. Telling an agent in its prompt
to "review your own work" is unverifiable: it can skip the step, misread
the result, or report success from a run that inspected nothing. A
reviewed PR and an unreviewed one have to be distinguishable from the
outside, and that only holds if something other than the reviewed party
decides.

The single most important property here is that **a review which read
nothing is not a pass**. Review CLIs report that condition in prose and
still exit 0 — e.g. "I could not access the repository contents" — so an
exit-code check alone certifies exactly the failure it exists to catch.
:func:`looks_unreadable` is the guard, and :class:`CodeReviewOutcome`
carries the distinction to the caller rather than collapsing it.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.code_review")

# The marker deliberately names no tool, model or vendor. It records that
# a review happened, nothing about what performed it — which backend runs
# is an implementation detail and must not leak into repo artifacts.
REVIEW_DONE_LABEL = "code_review done"

# A review that could not read the diff says so in prose and exits 0.
# Matching that shape is the only thing standing between "reviewed" and
# "certified without looking".
_UNREADABLE_PATTERNS = (
    "could not access the repository",
    "could not read the repository",
    "unable to access the repository",
    "could not inspect",
    "unable to inspect",
    "i was unable to review",
    "cannot safely validate",
    "should be considered unverified",
    "review was interrupted",
    "bwrap:",
    "rtm_newaddr",
    "failed to decode models response",
    "usage limit",
    "not supported when using",
)

# Long enough for a real review of a sizeable diff, short enough that a
# wedged reviewer cannot hold a dev session open to its own timeout.
DEFAULT_REVIEW_TIMEOUT_SECONDS = 900


class ReviewStatus(str, Enum):
    """What the review run actually established."""

    REVIEWED = "reviewed"
    """The reviewer read the diff and returned findings (or none)."""

    UNREADABLE = "unreadable"
    """The reviewer ran but inspected nothing. NOT a pass."""

    FAILED = "failed"
    """The reviewer could not be run at all."""

    SKIPPED = "skipped"
    """Review is switched off for this repo."""


@dataclass(frozen=True)
class CodeReviewOutcome:
    status: ReviewStatus
    output: str = ""
    error: str = ""

    @property
    def may_mark_reviewed(self) -> bool:
        """Only a review that actually read the diff earns the marker.

        Anything else must leave the PR unmarked: a marker applied to an
        unreadable run certifies precisely the case it exists to catch,
        and an unmarked PR is the signal that no review happened.
        """
        return self.status is ReviewStatus.REVIEWED


# The reviewer's own conclusion begins at a lone "codex" line; everything
# before it is the transcript, including the diff it was handed.
_VERDICT_MARKER_RE = re.compile(r"^codex\s*$", re.M)

# Fallback window when no marker is present: the verdict is written last.
_VERDICT_TAIL_CHARS = 4000


def extract_verdict(output: str) -> str:
    """Return just the reviewer's conclusion, not the whole transcript.

    This distinction is load-bearing. The transcript contains the diff
    under review, so scanning all of it for "could not access the
    repository" matches source code that merely *mentions* the phrase —
    including this module's own tests, which is exactly how the first
    version of :func:`looks_unreadable` declared a perfectly good review
    unreadable and refused to mark it.
    """
    text = output or ""
    matches = list(_VERDICT_MARKER_RE.finditer(text))
    if matches:
        return text[matches[-1].end():]
    return text[-_VERDICT_TAIL_CHARS:]


def looks_unreadable(output: str) -> bool:
    """True when the reviewer's own verdict says it inspected nothing.

    Only the verdict is examined — see :func:`extract_verdict`. A phrase
    appearing in the reviewed code is not the reviewer saying it.
    """
    lowered = extract_verdict(output).lower()
    return any(pattern in lowered for pattern in _UNREADABLE_PATTERNS)


def _strip_tool_identity(text: str) -> str:
    """Remove backend identity from text that will be posted to a repo.

    The findings are worth publishing; which model produced them is not.
    Leaking it into a PR comment would tie the repo's history to a
    vendor choice that is meant to stay an implementation detail.
    """
    cleaned = re.sub(
        r"\b(codex|copilot|cursor|claude|anthropic|openai|gpt[-\w.]*|"
        r"chatgpt|o[34]-\w+|sonnet|opus|haiku)\b",
        "the reviewer",
        text or "",
        flags=re.I,
    )
    # Collapse the repetition that substitution can produce
    # ("the reviewer the reviewer").
    return re.sub(r"(the reviewer)(\s+the reviewer)+", r"\1", cleaned)


async def run_code_review(
    *,
    repo: str,
    worktree_path: Path,
    base_branch: str,
    cli_command: str = "codex review",
    timeout_seconds: int = DEFAULT_REVIEW_TIMEOUT_SECONDS,
    session_id: str = "",
) -> CodeReviewOutcome:
    """Review the working branch against ``base_branch``.

    ``--base`` is appended because it reviews a pushed branch directly.
    The alternative, ``--uncommitted``, describes the working tree and
    would report nothing for a branch whose work is already committed —
    an empty review that reads as a clean one.
    """
    argv = [*shlex.split(cli_command), "--base", base_branch]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as e:
        log_event(
            _logger,
            "code_review.spawn_failed",
            session_id=session_id,
            repo=repo,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return CodeReviewOutcome(
            status=ReviewStatus.FAILED, error=f"{type(e).__name__}: {e}"
        )

    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        log_event(
            _logger,
            "code_review.timeout",
            session_id=session_id,
            repo=repo,
            timeout_seconds=timeout_seconds,
        )
        return CodeReviewOutcome(
            status=ReviewStatus.FAILED,
            error=f"review timed out after {timeout_seconds}s",
        )

    output = stdout.decode(errors="replace")

    # Silence is not a review. A misconfigured cli_command (`true`, a
    # wrapper that swallows its own output) exits 0 with nothing to say,
    # and treating that as REVIEWED hands out the marker for work nobody
    # did — the same hole as an unreadable run, just quieter.
    if not extract_verdict(output).strip():
        log_event(
            _logger,
            "code_review.empty",
            session_id=session_id,
            repo=repo,
            returncode=proc.returncode,
        )
        return CodeReviewOutcome(
            status=ReviewStatus.UNREADABLE,
            output=output,
            error="the reviewer produced no verdict",
        )

    # Order matters: a non-zero exit is a failure, but a ZERO exit with
    # unreadable output is the dangerous case — it looks like success.
    if looks_unreadable(output):
        log_event(
            _logger,
            "code_review.unreadable",
            session_id=session_id,
            repo=repo,
            returncode=proc.returncode,
            output_length=len(output),
        )
        return CodeReviewOutcome(
            status=ReviewStatus.UNREADABLE,
            output=output,
            error="the reviewer did not read the diff",
        )

    if proc.returncode != 0:
        log_event(
            _logger,
            "code_review.failed",
            session_id=session_id,
            repo=repo,
            returncode=proc.returncode,
        )
        return CodeReviewOutcome(
            status=ReviewStatus.FAILED,
            output=output,
            error=f"reviewer exited {proc.returncode}",
        )

    log_event(
        _logger,
        "code_review.completed",
        session_id=session_id,
        repo=repo,
        output_length=len(output),
    )
    return CodeReviewOutcome(status=ReviewStatus.REVIEWED, output=output)


def format_review_comment(outcome: CodeReviewOutcome) -> str:
    """Render findings for a PR comment, with backend identity removed."""
    body = _strip_tool_identity(extract_verdict(outcome.output)).strip()
    if len(body) > 60000:
        body = "…\n" + body[-60000:]
    return "## Automated code review\n\n" + (body or "_No findings reported._")
