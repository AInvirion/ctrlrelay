"""Detection of archived GitHub repos, shared by the poller and the
secops sweep (issue #163).

``skip_archived`` only ever existed in the config *generator*, so a repo
archived after its ``orchestrator.yaml`` entry was written kept being
swept forever: secops spawned a full agent session per day that did real
work only to discover the Dependabot alerts API answers 403 for archived
repos. The failure was quiet — a ``done`` session with a benign summary —
which is why it accumulated unnoticed.

**Fail open.** This check gates every piece of work the daemon does, so a
bug here doesn't skip one repo, it can silently stop the orchestrator
doing anything while the process looks healthy. Only a definite
``isArchived: true`` may skip a repo. A failed lookup, a timeout, an auth
error, a malformed response or a missing field all mean "unknown", and
unknown means the repo keeps being polled.

Consequently only the *confirmed-archived* outcome is cached — mirroring
``IssuePoller._issues_disabled_repos``. Nothing derived from an error is
remembered, so one flaky call can't disable a repo for the daemon's
lifetime, and the cache is in-memory so a restart re-checks everything
and an unarchived repo recovers on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.archived")

# Shorter than the 60s gh default: this probe runs once per repo per poll
# cycle, and a hung lookup would delay every repo behind it. Timing out
# just means "unknown" — the repo is polled as normal.
ARCHIVED_PROBE_TIMEOUT_SECONDS = 20


@dataclass
class ArchivedRepoTracker:
    """Answers "is this repo archived?" with a daemon-lifetime cache of
    confirmed-archived repos only.

    One instance is shared between the issue poller and the secops sweep
    so a single confirmation (and a single log line) covers both.
    """

    github: GitHubCLI
    # Confirmed archived — the only thing worth remembering. Held for the
    # process lifetime; unarchiving requires an operator, so a daemon
    # restart is a fine invalidation boundary.
    archived_repos: set[str] = field(default_factory=set, repr=False)
    # Repos whose last probe failed, tracked purely to suppress repeat
    # logs. Cleared on the next successful probe so a NEW outage is still
    # visible. This never affects the answer we return.
    _failed_probes: set[str] = field(default_factory=set, repr=False)

    async def is_archived(self, repo: str) -> bool:
        """Return True only for a confirmed-archived repo.

        Any error, timeout or ambiguous response returns False (the repo
        is treated as active) and is not cached, so the next cycle
        re-checks it. ``asyncio.CancelledError`` propagates so a shutdown
        signal isn't swallowed into a fail-open answer.
        """
        if repo in self.archived_repos:
            return True

        try:
            result: Any = await self.github.repo_is_archived(
                repo, timeout=ARCHIVED_PROBE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if repo not in self._failed_probes:
                self._failed_probes.add(repo)
                log_event(
                    _logger,
                    "poll.repo.archived_check_failed",
                    repo=str(repo),
                    reason=type(e).__name__,
                    error=str(e)[:200],
                    action="treating repo as active",
                )
            return False

        self._failed_probes.discard(repo)

        # `is True` on purpose: a truthy non-boolean (a Mock, a "true"
        # string from a future gh format change) must not skip a repo.
        if result is not True:
            return False

        self.archived_repos.add(repo)
        log_event(
            _logger,
            "poll.repo.archived",
            repo=str(repo),
            action=(
                "skipping until daemon restart — remove the entry from "
                "orchestrator.yaml"
            ),
        )
        return True
