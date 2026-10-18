"""Consecutive-dispatch-failure alerting.

#204: between 2026-10-17 06:00 and 2026-10-18 06:05 PDT every dispatch
this relay made exited non-zero — 163 of them, across every repo in the
scope list and both the secops and dev lanes — and nothing told anybody.
Both systemd units stayed ``active (running)`` throughout and the daily
sweeps kept firing on schedule, so a two-day fleet-wide outage of
everything the tool does was indistinguishable from a quiet week.

#198 fixed the case where the notification transport is missing. This is
the other half: the transport is fine and **nothing asks it to say
anything**.

What this module is careful about, in order of how much it cost to learn:

- **One message, not one per repo.** The sweep already fans out a line
  per failing repo, and 88 of those is how an operator learns to skim
  the channel. The alert here is about the relay, so it is sent once.
- **No re-send until a success intervenes.** #200 measured the opposite
  shape on this very deployment: an expired question re-asked unchanged
  every morning, one approval asked **thirteen times** over a fortnight,
  which trained its reader to skip the channel — so the questions that
  were *not* repeats went unanswered too. A daily "still broken" is that
  bug with a different payload.
- **The latch has to be able to re-arm.** A dedup that never clears is a
  guard that fires once in the lifetime of a database, and the next
  outage is silent again. A success clears it, which is also the only
  event that means the relay recovered.
- **An all-failed sweep needs at least one repo in it.** `0 of 0 failed`
  satisfies "every repo failed" arithmetically. A zero-repo sweep is an
  empty answer sheet, not an outage, and alerting on one would make the
  alert fire on a config with no repos — see the fleet rule about a wait
  loop that cannot tell an empty result from a complete one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.state import StateDB

_logger = get_logger("core.dispatch_health")

# Reasons, as they appear on the alert and in the log event.
REASON_CONSECUTIVE = "consecutive_failures"
REASON_SWEEP_ALL_FAILED = "sweep_all_failed"

# How long a claim on the right to send one alert is held before it is
# considered abandoned. It does two jobs:
#
#   - a process killed mid-send does not hold the claim forever, so the
#     outage is still announced once it comes back;
#   - a delivery that FAILS leaves its claim to expire rather than
#     releasing it, so during an outage at most one attempt is made per
#     minute instead of one per failed dispatch -- which at ~2 s per
#     failure and a ~15 s connect-plus-send bound would otherwise be
#     most of the poll cycle.
DEFAULT_ALERT_LEASE_SECONDS = 60


@dataclass(frozen=True)
class DispatchAlert:
    """One operator-facing alert about the relay itself.

    ``consecutive`` is the run length at the moment the alert was
    raised, and ``threshold`` the configured trip point, so the message
    can say which rule fired rather than asserting a severity.
    """

    reason: str
    consecutive: int
    threshold: int
    detail: str
    # Identifies the lease this alert holds, so a DELIVERY is recorded
    # against our own claim and not a newer one taken by another
    # process. See `StateDB.mark_dispatch_alert_delivered`.
    claim_token: str | None = None

    def message(self) -> str:
        """The line to post on the chat transport.

        Ends in what to do. A warning with no action in it is a shrug,
        and a shrug in a channel that already carries routine sweep
        chatter is read as routine sweep chatter.
        """
        if self.reason == REASON_SWEEP_ALL_FAILED:
            headline = (
                f"🚨 CTRLRelay: every repo failed in one sweep "
                f"({self.detail})."
            )
        else:
            headline = (
                f"🚨 CTRLRelay: {self.consecutive} dispatches in a row have "
                f"failed (threshold {self.threshold}). {self.detail}"
            )
        return (
            f"{headline}\n"
            "Nothing is dispatching until this clears. Check the agent "
            "credential first — an expired one exits non-zero in about two "
            "seconds — then `journalctl --user -u ctrlrelay-poller` and the "
            "`claude_exit_code` / `output_tail` columns on the `sessions` "
            "table for the cause.\n"
            "This will not be repeated until a dispatch succeeds."
        )


class DispatchHealth:
    """Tracks the dispatch failure run and decides when to alert.

    The run length lives in the state database rather than in memory on
    purpose: the poller restarts (redeploys, reboots, a `systemctl
    restart`), and an in-memory counter would reset the run to zero
    every time — so a relay that is broken *because* it keeps being
    restarted is exactly the one that would never reach the threshold.
    It also means the latch survives, so a restart loop cannot re-send
    the same alert on every boot.

    ``threshold`` is required and has no default. A default here would
    be a second place for the trip point to be configured, and the one
    callers forgot to pass would silently keep the old no-alert
    behaviour.
    """

    __slots__ = ("state_db", "threshold", "lease_seconds")

    def __init__(
        self,
        state_db: "StateDB",
        *,
        threshold: int,
        lease_seconds: int = DEFAULT_ALERT_LEASE_SECONDS,
    ) -> None:
        if threshold < 1:
            raise ValueError(
                f"threshold must be at least 1, got {threshold!r}. "
                "Set alerts.enabled to false to switch alerting off; a "
                "zero or negative threshold would mean either 'alert on "
                "everything' or 'alert on nothing' depending on how the "
                "comparison is written, and neither is a setting anybody "
                "asked for."
            )
        if lease_seconds < 1:
            raise ValueError(
                f"lease_seconds must be at least 1, got {lease_seconds!r}. "
                "A zero lease makes every claim instantly expired, so two "
                "processes could both attempt the same alert and a failed "
                "delivery would retry on every dispatch."
            )
        self.state_db = state_db
        self.threshold = threshold
        self.lease_seconds = lease_seconds

    def record_success(self, *, repo: str | None = None) -> None:
        """Clear the run and re-arm the latch.

        Called on every dispatch that exits 0, including ones whose
        pipeline then failed for its own reasons — the question this
        tracker answers is "can the relay still run a worker at all",
        not "did the work succeed".
        """
        previous = self.state_db.reset_dispatch_failures()
        if previous:
            log_event(
                _logger,
                "dispatch_health.recovered",
                repo=repo,
                previous_consecutive_failures=previous,
            )

    def record_failure(
        self,
        *,
        repo: str | None = None,
        session_id: str | None = None,
        exit_code: int | None = None,
    ) -> DispatchAlert | None:
        """Extend the run; return an alert the first time it trips.

        Returns ``None`` when the run is still under the threshold, and
        ``None`` again on every later failure in the same run — the
        latch that suppresses a repeat is DELIVERY, not this claim —
        see :meth:`StateDB._may_claim_alert`. A claim is a lease, so an
        attempt that is killed or that fails is retried once the lease
        expires.

        The increment, the comparison and the claim happen in one
        transaction inside ``StateDB``, because two pollers can share
        one database and a success landing between this one's read and
        its claim would announce a relay that had just recovered.
        """
        consecutive, claim_token = self.state_db.record_dispatch_failure(
            self.threshold, lease_seconds=self.lease_seconds
        )
        log_event(
            _logger,
            "dispatch_health.failure",
            repo=repo,
            session_id=session_id,
            exit_code=exit_code,
            consecutive_failures=consecutive,
            threshold=self.threshold,
        )
        if claim_token is None:
            # Either under the threshold, or already latched. Logged
            # once the threshold is passed, because "the alert was
            # suppressed" and "the alert was never reached" are
            # different facts and only one of them means the dedup is
            # working.
            if consecutive >= self.threshold:
                log_event(
                    _logger,
                    "dispatch_health.alert_suppressed",
                    repo=repo,
                    consecutive_failures=consecutive,
                    reason=REASON_CONSECUTIVE,
                )
            return None
        detail = (
            f"Last failure was {repo or 'an unnamed repo'}"
            + (f", exit code {exit_code}" if exit_code is not None else "")
            + "."
        )
        return DispatchAlert(
            reason=REASON_CONSECUTIVE,
            consecutive=consecutive,
            threshold=self.threshold,
            detail=detail,
            claim_token=claim_token,
        )

    def record_sweep(self, *, total: int, failed: int) -> DispatchAlert | None:
        """Alert when a whole sweep failed, regardless of the run length.

        A sweep that fails on every repo is already an outage even if
        the configured threshold has not been reached — 88 repos is one
        scheduler fire, and waiting for a second day of it is the
        behaviour this ticket exists to remove.

        ``total`` must be at least 1 and ``failed`` must EQUAL it.
        A zero-repo sweep returns ``None``: nothing ran, which is not
        the same as everything failing.

        The comparison is ``!=``, not ``<``. An earlier version said
        "must equal" in this docstring and tested ``failed < total``,
        so ``total=1, failed=2`` produced an alert reading "all 1 of 1
        repos failed" — a sentence that was not true of its own inputs.
        A count larger than the total is a caller bug, and the honest
        answer to one is to decline rather than to report a number
        nobody measured.
        """
        if total < 1 or failed < 0 or failed != total:
            return None
        claim_token = self.state_db.claim_dispatch_alert(
            lease_seconds=self.lease_seconds
        )
        if claim_token is None:
            log_event(
                _logger,
                "dispatch_health.alert_suppressed",
                consecutive_failures=self.state_db.dispatch_failure_count(),
                reason=REASON_SWEEP_ALL_FAILED,
            )
            return None
        return DispatchAlert(
            reason=REASON_SWEEP_ALL_FAILED,
            consecutive=self.state_db.dispatch_failure_count(),
            threshold=self.threshold,
            detail=f"all {total} of {total} repos failed",
            claim_token=claim_token,
        )
