"""Retirement of BLOCKED questions the operator can no longer usefully answer.

A ``pending_resumes`` row deliberately outlives the in-session wait: the
transport gives up after ``ask_timeout_seconds``, but the row survives so
a reply arriving hours later still drives the resume. Nothing ever
retired those rows, so two failure modes accumulated:

- **Nobody answered.** Real numbers from one deployment: 46 questions
  posted, 4 ever answered. The unanswered 42 stayed live targets for
  orphan reply routing, and each sweep re-posted the same question.
- **Somebody acted outside ctrlrelay.** The PR got merged by hand, or
  the issue was closed by a teammate. The question is now not just
  unanswered but meaningless, and an answer to it would drive a resume
  against state that has already moved on.

This module retires both. It only ever sets ``expired_at`` on rows that
are still unanswered, so a reply that lands in the same tick wins.

Retiring a row does not stop the question coming back. A scheduled sweep
starts a fresh session the next morning, the agent reaches the same
blocker, and asks again in new words. Two things here deal with that:
``find_repeat_history`` lets whoever posts the question say it is a
repeat, and the sweep prunes finished rows past a retention window so
that history stays a window rather than the whole life of the install.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ctrlrelay.core.github import GitHubCLI, GitHubError
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.core.state import StateDB

_logger = get_logger("core.question_expiry")

# Captures both "PR #60" and "#60" forms the agent uses interchangeably
# in BLOCKED questions. The negative-lookahead on trailing digits avoids
# matching CVE-2026-... style identifiers; PR numbers are always >= 1.
_PR_NUM_RE = re.compile(r"(?:PR\s*)?#(\d+)(?!\d)", re.IGNORECASE)

# Per-call cap on the `gh api` probe. The sweep runs on a schedule and
# holds no repo lock, but a hung gh would still stall the whole pass;
# a single REST read has no business taking longer than this.
_STATE_PROBE_TIMEOUT_SECONDS = 15

# Ceiling on probes per run, so one pass cannot outlive its own cron
# interval. Unbounded, the cost is one serialized `gh api` call per
# cited number: 2000 backlogged rows against a slow GitHub is
# 2000 x 15s = 8.3h of a job that fires hourly, and the sweep would
# spend all day overlapping itself. Rows past the cap are simply
# examined on the next run — expiry is never urgent, and the TTL pass
# retires them on age regardless of whether they were ever probed.
# 200 x 15s worst case = 50min, inside the hour with room to spare.
_MAX_RESOLVED_PROBES_PER_RUN = 200

EXPIRY_REASON_TTL = "ttl"
EXPIRY_REASON_RESOLVED = "resolved_elsewhere"

DEFAULT_RETENTION_DAYS = 90


def extract_referenced_numbers(question: str) -> list[str]:
    """Pull deduplicated issue/PR numbers out of a question, in the
    order the agent listed them."""
    seen: set[str] = set()
    out: list[str] = []
    for n in _PR_NUM_RE.findall(question or ""):
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _normalise(question: str) -> str:
    return re.sub(r"\s+", " ", (question or "").lower()).strip(" .?!")


def same_substance(a: str, b: str) -> bool:
    """Whether two BLOCKED questions ask about the same thing.

    The agent rewords the question every run, so comparing text finds
    almost nothing. What it keeps is the PR and issue numbers: a
    question that cites at least one number the other cites is about the
    same thing. Only when NEITHER cites a number does the text decide,
    after lowercasing and collapsing whitespace.

    Known limits, both in the direction of a missed repeat or an extra
    one, never of a lost question: a reworded question with no numbers
    is not matched; and an incidental shared number (an alert id cited
    in two unrelated questions) is matched.
    """
    na = set(extract_referenced_numbers(a))
    nb = set(extract_referenced_numbers(b))
    if na or nb:
        return bool(na & nb)
    return bool(_normalise(a)) and _normalise(a) == _normalise(b)


@dataclass(frozen=True)
class RepeatHistory:
    """Earlier, unanswered asks of the same question in the same repo."""

    times_asked_before: int
    last_asked_at: int


def find_repeat_history(
    state_db: StateDB,
    *,
    repo: str,
    question: str,
    exclude_session_id: str | None,
) -> RepeatHistory | None:
    """How many times this question was asked before in ``repo`` and
    never answered, or None when this is the first time.

    Counts every earlier row of the same substance that has no answer:
    still open, retired on TTL, or retired as resolved elsewhere (which
    did not stop it coming back, so it was not resolved). An answer
    resets the count, so only rows created after the latest answered
    one are counted. ``exclude_session_id`` is the session asking now;
    its own row, when a resume re-blocked and rewrote it, is not an
    earlier ask. It is required so a caller cannot forget it and count
    the question as a repeat of itself.
    """
    matches = [
        row
        for row in state_db.list_pending_resumes_for_repo(repo)
        if row["session_id"] != exclude_session_id
        and same_substance(question, row["question"])
    ]
    last_answer = max(
        (int(r["created_at"]) for r in matches if r["answered_at"] is not None),
        default=None,
    )
    unanswered = [
        r
        for r in matches
        if r["answered_at"] is None
        and (last_answer is None or int(r["created_at"]) > last_answer)
    ]
    if not unanswered:
        return None
    return RepeatHistory(
        times_asked_before=len(unanswered),
        last_asked_at=max(int(r["created_at"]) for r in unanswered),
    )


def format_repeat_note(history: RepeatHistory) -> str:
    """One plain-text line telling the reader this is not a new question."""
    n = history.times_asked_before
    times = "once" if n == 1 else f"{n} times"
    when = datetime.fromtimestamp(history.last_asked_at)
    return (
        f"Repeat: asked {times} before, last on {when:%b} {when.day}, "
        f"no answer yet."
    )


def prune_finished_questions(
    state_db: StateDB,
    *,
    retention_days: int,
    now: int | None = None,
) -> int:
    """Delete finished questions older than the retention window."""
    now = int(time.time()) if now is None else now
    pruned = state_db.prune_finished_pending_resumes(
        now - retention_days * 86400
    )
    if pruned:
        log_event(
            _logger,
            "question.pruned",
            rows=pruned,
            retention_days=retention_days,
        )
    return pruned


async def _all_references_resolved(
    github: GitHubCLI,
    *,
    repo: str,
    numbers: list[str],
) -> bool:
    """True when every cited number is closed or merged.

    Conservative on purpose. One still-open reference means the question
    retains something to decide, so the row stays. Any probe error at
    all — network, auth, a number that names nothing — returns False:
    we would rather re-ask a dead question than silently drop a live
    one, since the operator can ignore noise but cannot recover a
    question that was never posted.
    """
    if not numbers:
        return False

    for number in numbers:
        try:
            state = await github.get_issue_or_pr_state(
                repo,
                int(number),
                timeout=_STATE_PROBE_TIMEOUT_SECONDS,
            )
        except (GitHubError, TimeoutError, OSError, ValueError):
            return False
        except Exception:
            return False

        if (state.get("state") or "").lower() != "closed":
            return False

    return True


async def expire_stale_questions(
    state_db: StateDB,
    github: GitHubCLI | None,
    *,
    ttl_seconds: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Retire questions that aged out or whose subject is already
    resolved. Returns the rows actually expired.

    TTL runs first and needs no network, so a GitHub outage still lets
    the age-based sweep make progress. ``github=None`` disables only the
    resolved-elsewhere pass. Finished rows past ``retention_days`` are
    pruned next, also before any network call.
    """
    now = int(time.time()) if now is None else now
    expired: list[dict[str, Any]] = []

    for row in state_db.list_expirable_pending_resumes(now - ttl_seconds):
        if state_db.expire_pending_resume(row["session_id"], EXPIRY_REASON_TTL):
            expired.append({**row, "expired_reason": EXPIRY_REASON_TTL})
            log_event(
                _logger,
                "question.expired",
                session_id=row["session_id"],
                repo=row["repo"],
                pipeline=row["pipeline"],
                reason=EXPIRY_REASON_TTL,
                age_seconds=now - int(row["created_at"]),
            )

    try:
        prune_finished_questions(
            state_db, retention_days=retention_days, now=now
        )
    except Exception as e:
        # Housekeeping. A failed delete must not stop the expiry passes,
        # and it is logged so a table that keeps growing has a cause.
        log_event(
            _logger,
            "question.prune_failed",
            error_type=type(e).__name__,
            error=str(e)[:200],
        )

    if github is None:
        return expired

    probes_used = 0
    for row in state_db.list_unanswered_pending_resumes():
        numbers = extract_referenced_numbers(row["question"])
        # Rows citing nothing are skipped without spending budget —
        # they can never be resolution-expired anyway.
        if not numbers:
            continue

        # Check the budget against what this row will actually COST,
        # before spending any of it. Testing `probes_used` alone lets a
        # single row overshoot without limit: one question citing 300
        # numbers passes a `0 >= 200` check and then issues all 300
        # calls, ~75min of a job that fires hourly.
        if len(numbers) > _MAX_RESOLVED_PROBES_PER_RUN:
            # Bigger than any run's whole budget, so it can never be
            # resolution-checked — expiry needs EVERY reference closed.
            # Skip rather than break: one pathological row must not
            # starve the rest. The TTL pass still retires it on age.
            log_event(
                _logger,
                "question.expiry.row_exceeds_budget",
                session_id=row["session_id"],
                repo=row["repo"],
                references=len(numbers),
                budget=_MAX_RESOLVED_PROBES_PER_RUN,
            )
            continue
        if probes_used + len(numbers) > _MAX_RESOLVED_PROBES_PER_RUN:
            log_event(
                _logger,
                "question.expiry.probe_budget_exhausted",
                budget=_MAX_RESOLVED_PROBES_PER_RUN,
                probes_used=probes_used,
                reason="remaining rows deferred to the next run",
            )
            break
        probes_used += len(numbers)
        if not await _all_references_resolved(
            github, repo=row["repo"], numbers=numbers
        ):
            continue
        if state_db.expire_pending_resume(
            row["session_id"], EXPIRY_REASON_RESOLVED
        ):
            expired.append({**row, "expired_reason": EXPIRY_REASON_RESOLVED})
            log_event(
                _logger,
                "question.expired",
                session_id=row["session_id"],
                repo=row["repo"],
                pipeline=row["pipeline"],
                reason=EXPIRY_REASON_RESOLVED,
                references=",".join(numbers),
            )

    return expired
