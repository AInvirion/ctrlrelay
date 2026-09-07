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
"""

from __future__ import annotations

import re
import time
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
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Retire questions that aged out or whose subject is already
    resolved. Returns the rows actually expired.

    TTL runs first and needs no network, so a GitHub outage still lets
    the age-based sweep make progress. ``github=None`` disables only the
    resolved-elsewhere pass.
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

    if github is None:
        return expired

    probes_used = 0
    for row in state_db.list_unanswered_pending_resumes():
        if probes_used >= _MAX_RESOLVED_PROBES_PER_RUN:
            log_event(
                _logger,
                "question.expiry.probe_budget_exhausted",
                budget=_MAX_RESOLVED_PROBES_PER_RUN,
                reason="remaining rows deferred to the next run",
            )
            break
        numbers = extract_referenced_numbers(row["question"])
        # Rows citing nothing are skipped without spending budget —
        # they can never be resolution-expired anyway.
        if not numbers:
            continue
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
