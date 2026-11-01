"""Scheduled jobs run once across machines on one account (#246).

Issue runs claim with a comment on the issue (:mod:`ctrlrelay.core.claims`).
A scheduled job such as the security sweep has no issue, so its claim is
a git ref in the repository it runs on::

    refs/ctrlrelay/claims/<job>/<window>

pointing at a small blob that names the machine. Not a branch: GitHub
lists it nowhere in its interface. Creating a ref is all-or-nothing on
GitHub's side (a second create of the same name answers 422 "Reference
already exists"), so the first machine to create it holds the job for
that window, with no race window to check: the same rule as issue claims,
first claim wins, without the wait.

``window`` is the time the job was *due* (from its cron rule, not when it
actually started: a machine that was asleep runs it up to an hour late)
rounded to ``WINDOW_SECONDS``, so two machines firing the same cron a few
seconds apart, across a minute boundary, or one of them late, name the
same window. A machine that dies holding a window loses only that window:
the next window is a new ref.

Old refs are deleted after ``KEEP_DAYS``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ctrlrelay.core.claims import safe_node_name
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.job_claims")

REF_PREFIX = "refs/ctrlrelay/claims"
WINDOW_SECONDS = 600
KEEP_DAYS = 7
_WINDOW_FORMAT = "%Y%m%dT%H%MZ"


def window_id(now: float, period: int = WINDOW_SECONDS) -> str:
    """The run window ``now`` belongs to: rounded to the nearest
    ``period``, so 05:59:58 and 06:00:03 are one window."""
    rounded = int((now + period / 2) // period * period)
    return datetime.fromtimestamp(rounded, tz=UTC).strftime(_WINDOW_FORMAT)


def ref_name(job: str, window: str) -> str:
    return f"{REF_PREFIX}/{job}/{window}"


@dataclass(frozen=True)
class JobClaim:
    """``won``; or lost to ``owner`` (``"unknown"`` if its note could not
    be read); or ``error`` when the claim could not be made at all."""

    won: bool
    owner: str | None = None
    error: str | None = None


class JobClaimer:
    """Claims ``(repo, job, window)`` for this machine.

    A claim that cannot be made (GitHub down, no permission) is reported
    as an error, and the caller skips the job for this window: running it
    unclaimed could run it twice, and the next window tries again.
    """

    def __init__(self, github: Any, node: str, *, window: str) -> None:
        self.github = github
        self.node = safe_node_name(node)
        # One window for a whole sweep: repos late in a long sweep must not
        # drift into the next window and be swept again elsewhere.
        self.window = window

    async def claim(self, repo: str, job: str) -> JobClaim:
        window = self.window
        ref = ref_name(job, window)
        try:
            blob = await self.github.create_blob(repo, f"{self.node}\n")
            created = await self.github.create_ref(repo, ref, blob)
        except Exception as e:
            log_event(_logger, "jobs.claim_failed", repo=repo, job=job,
                      window=window, error=str(e)[:200])
            return JobClaim(won=False, error=f"{type(e).__name__}: {str(e)[:200]}")
        if created:
            log_event(_logger, "jobs.claimed", repo=repo, job=job, window=window,
                      node=self.node)
            await self._prune(repo, job, window)
            return JobClaim(won=True)
        owner = await self._owner(repo, ref)
        log_event(_logger, "jobs.skipped", repo=repo, job=job, window=window,
                  node=self.node, owner=owner)
        return JobClaim(won=False, owner=owner)

    async def _owner(self, repo: str, ref: str) -> str:
        try:
            sha = await self.github.get_ref_sha(repo, ref)
            raw = await self.github.get_blob(repo, sha)
            name = base64.b64decode(raw).decode("utf-8", "replace").strip()
            return name[:100] or "unknown"
        except Exception:
            return "unknown"

    async def _prune(self, repo: str, job: str, current: str) -> None:
        """Best-effort: delete this job's refs older than ``KEEP_DAYS``.
        Only refs whose window parses are touched."""
        try:
            refs = await self.github.list_refs(repo, f"{REF_PREFIX}/{job}/")
        except Exception:
            return
        try:
            now = datetime.strptime(current, _WINDOW_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            return
        for ref in refs:
            window = ref.rsplit("/", 1)[-1]
            try:
                when = datetime.strptime(window, _WINDOW_FORMAT).replace(tzinfo=UTC)
            except ValueError:
                continue
            if (now - when).days >= KEEP_DAYS:
                try:
                    await self.github.delete_ref(repo, ref)
                except Exception as e:
                    log_event(_logger, "jobs.prune_failed", repo=repo, ref=ref,
                              error=str(e)[:200])
