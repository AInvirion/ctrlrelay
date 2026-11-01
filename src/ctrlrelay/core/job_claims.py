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

``window`` is the exact minute the job was *due* by its cron rule
(:func:`ctrlrelay.core.scheduler.last_fire_time`), not when it started: every
machine works it out from the same rule, so machines a few seconds apart,
or one of them late, name the same window, and two different fires never
share one. A machine that dies holding a window loses only that window:
the next window is a new ref.

Old refs are deleted after ``KEEP_DAYS``; the claim's job only needs it
while its run lasts.
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
# Claims older than this are deleted. A sweep that is still running a week
# after it was due would lose its claim to the prune; sweeps take minutes
# to hours, so 30 days leaves a wide margin.
KEEP_DAYS = 30
_WINDOW_FORMAT = "%Y%m%dT%H%MZ"


def window_id(due: float) -> str:
    """The window name for a run due at ``due`` (epoch seconds): that
    minute, in UTC."""
    return datetime.fromtimestamp(due, tz=UTC).strftime(_WINDOW_FORMAT)


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
        """Never raises: anything unexpected is an error result, so one
        repo's claim cannot end the sweep for the rest."""
        try:
            return await self._claim(repo, job)
        except Exception as e:
            return JobClaim(won=False, error=f"{type(e).__name__}: {str(e)[:200]}")

    async def _claim(self, repo: str, job: str) -> JobClaim:
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
            try:
                await self._prune(repo, job, window)
            except Exception:
                pass  # best-effort: the claim is made either way
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
        prefix = f"{REF_PREFIX}/{job}/"
        try:
            refs = await self.github.list_refs(repo, prefix)
        except Exception:
            return
        try:
            now = datetime.strptime(current, _WINDOW_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            return
        for ref in refs:
            window = ref[len(prefix):] if ref.startswith(prefix) else ""
            if not window or "/" in window:
                continue  # only this job's own windows, nothing nested
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
