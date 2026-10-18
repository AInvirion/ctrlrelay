"""Tests for the consecutive-dispatch-failure alert (#204).

Every test here runs against a real :class:`StateDB` on disk rather
than a mock. The two things that decide whether this works — a counter
that survives a poller restart, and a latch that is cleared by exactly
one event — are properties of the database, and a mock would answer
for whatever the test expected.

The guard is proven in BOTH directions, because an alert counter that
cannot fire is the same class of bug as the one being fixed and would
be shipping inside the fix for it:

- inject a run of failures, watch exactly one message come out;
- run a healthy relay, watch nothing come out;
- recover, fail again, and watch a SECOND message come out — a dedup
  that never re-arms is a guard that fires once per database.
"""

from pathlib import Path

import pytest

from ctrlrelay.core.dispatch_health import (
    REASON_CONSECUTIVE,
    REASON_SWEEP_ALL_FAILED,
    DispatchHealth,
)
from ctrlrelay.core.state import StateDB


@pytest.fixture
def state_db(tmp_path: Path) -> StateDB:
    db = StateDB(tmp_path / "state.db")
    yield db
    db.close()


def run_failures(health: DispatchHealth, n: int, *, repo: str = "o/r") -> list:
    """Drive `n` failed dispatches; return the alerts that came out."""
    alerts = []
    for i in range(n):
        alert = health.record_failure(
            repo=repo, session_id=f"s-{i}", exit_code=1
        )
        if alert is not None:
            alerts.append(alert)
    return alerts


class TestThresholdTrip:
    def test_nothing_is_sent_below_the_threshold(self, state_db) -> None:
        health = DispatchHealth(state_db, threshold=3)
        assert run_failures(health, 2) == []

    def test_exactly_one_alert_at_the_threshold(self, state_db) -> None:
        health = DispatchHealth(state_db, threshold=3)
        alerts = run_failures(health, 3)
        assert len(alerts) == 1
        assert alerts[0].reason == REASON_CONSECUTIVE
        assert alerts[0].consecutive == 3
        assert alerts[0].threshold == 3

    def test_one_message_not_one_per_failure(self, state_db) -> None:
        """The run that produced the ticket was 163 long.

        163 messages is the per-repo fan-out noise the ticket
        explicitly rules out, so the count is asserted against the
        real number rather than against a token few.
        """
        health = DispatchHealth(state_db, threshold=3)
        assert len(run_failures(health, 163)) == 1

    def test_a_threshold_of_one_trips_on_the_first_failure(
        self, state_db
    ) -> None:
        health = DispatchHealth(state_db, threshold=1)
        assert len(run_failures(health, 1)) == 1

    def test_the_configured_threshold_is_the_one_used(self, state_db) -> None:
        """Not a hardcoded 3 that happens to match the default.

        The expectation here comes from the test, not from the
        object under test — a guard that reads its expectation out of
        the code it checks always passes.
        """
        health = DispatchHealth(state_db, threshold=7)
        assert run_failures(health, 6) == []
        assert len(run_failures(health, 1)) == 1


class TestHealthyRelaySaysNothing:
    def test_successes_alone_produce_no_alert(self, state_db) -> None:
        """Land a failure first, so the assertions are not true by
        default.

        Starting from a clean database, "no alert and a zero counter"
        is also satisfied by a `record_success` that does nothing at
        all. The first two failures make the state non-trivial, and the
        successes then have something to clear.
        """
        health = DispatchHealth(state_db, threshold=3)
        run_failures(health, 2)
        assert state_db.dispatch_failure_count() == 2
        for _ in range(20):
            health.record_success(repo="o/r")
        assert state_db.get_dispatch_health()["alerted_at"] is None
        assert state_db.dispatch_failure_count() == 0

    def test_failures_interleaved_with_successes_never_trip(
        self, state_db
    ) -> None:
        """Two failures then a success, repeatedly, is a flaky repo and
        not an outage. It must stay silent at a threshold of 3."""
        health = DispatchHealth(state_db, threshold=3)
        alerts = []
        for _ in range(30):
            alerts += run_failures(health, 2)
            health.record_success(repo="o/r")
        assert alerts == []


class TestLatchAndRecovery:
    def test_further_failures_after_the_alert_are_silent(
        self, state_db
    ) -> None:
        health = DispatchHealth(state_db, threshold=3)
        run_failures(health, 3)
        assert run_failures(health, 50) == []

    def test_a_success_clears_the_run_and_the_latch(self, state_db) -> None:
        health = DispatchHealth(state_db, threshold=3)
        run_failures(health, 5)
        health.record_success(repo="o/r")
        row = state_db.get_dispatch_health()
        assert row["consecutive_failures"] == 0
        assert row["alerted_at"] is None

    def test_a_second_outage_alerts_again(self, state_db) -> None:
        """The latch re-arms.

        Without this the alert fires once in the lifetime of the
        database and every later outage is as silent as the one the
        ticket is about.
        """
        health = DispatchHealth(state_db, threshold=3)
        first = run_failures(health, 4)
        health.record_success(repo="o/r")
        second = run_failures(health, 4)
        assert len(first) == 1
        assert len(second) == 1

    def test_record_success_reports_the_run_it_cleared(self, state_db) -> None:
        run_failures(DispatchHealth(state_db, threshold=3), 4)
        assert state_db.reset_dispatch_failures() == 4
        # And again on an already-clean relay, so the caller can tell a
        # recovery from an ordinary success and not log one every time.
        assert state_db.reset_dispatch_failures() == 0


class TestSurvivesARestart:
    def test_the_run_length_outlives_the_process(self, tmp_path: Path) -> None:
        """A restart must not reset the count to zero.

        An in-memory counter would, and a relay that is broken AND
        being restarted is precisely the one that would then never
        reach the threshold.
        """
        path = tmp_path / "state.db"
        first = StateDB(path)
        run_failures(DispatchHealth(first, threshold=5), 4)
        first.close()

        second = StateDB(path)
        try:
            assert second.dispatch_failure_count() == 4
            # The fifth failure, after the "restart", is the one that
            # trips — proving the earlier four were still counted.
            alerts = run_failures(DispatchHealth(second, threshold=5), 1)
            assert len(alerts) == 1
            assert alerts[0].consecutive == 5
        finally:
            second.close()

    def test_the_latch_outlives_the_process(self, tmp_path: Path) -> None:
        """A restart loop must not re-send the alert on every boot."""
        path = tmp_path / "state.db"
        first = StateDB(path)
        assert len(run_failures(DispatchHealth(first, threshold=2), 2)) == 1
        first.close()

        second = StateDB(path)
        try:
            assert run_failures(DispatchHealth(second, threshold=2), 2) == []
        finally:
            second.close()


class TestSweepAllFailed:
    def test_a_fully_failed_sweep_alerts_under_the_threshold(
        self, state_db
    ) -> None:
        """88 repos is one scheduler fire, so this is already an outage."""
        health = DispatchHealth(state_db, threshold=3)
        alert = health.record_sweep(total=88, failed=88)
        assert alert is not None
        assert alert.reason == REASON_SWEEP_ALL_FAILED
        assert "88" in alert.detail

    def test_a_partly_failed_sweep_does_not_alert(self, state_db) -> None:
        health = DispatchHealth(state_db, threshold=3)
        assert health.record_sweep(total=88, failed=87) is None

    def test_an_empty_sweep_does_not_alert(self, state_db) -> None:
        """`0 of 0 failed` satisfies "every repo failed" arithmetically.

        Alerting on it would fire the alarm on a config with no repos.
        Nothing ran is not the same fact as everything failed.
        """
        health = DispatchHealth(state_db, threshold=3)
        assert health.record_sweep(total=0, failed=0) is None
        assert state_db.get_dispatch_health()["alerted_at"] is None

    def test_more_failures_than_repos_is_declined_not_reported(
        self, state_db
    ) -> None:
        """`total=1, failed=2` used to produce an alert reading
        "all 1 of 1 repos failed" — a sentence that was not true of its
        own inputs, because the docstring said "must equal" and the
        comparison was `failed < total`.

        A count larger than the total is a caller bug, and the honest
        answer to one is to decline rather than to publish a number
        nobody measured.
        """
        health = DispatchHealth(state_db, threshold=3)
        assert health.record_sweep(total=1, failed=2) is None
        assert health.record_sweep(total=5, failed=-1) is None
        assert state_db.get_dispatch_health()["alerted_at"] is None

    def test_a_single_repo_sweep_that_failed_does_alert(
        self, state_db
    ) -> None:
        """The control for the test above: total=1 is not empty.

        Without this, the empty-sweep guard could be implemented as
        "never alert on a sweep" and both tests would pass.
        """
        health = DispatchHealth(state_db, threshold=3)
        assert health.record_sweep(total=1, failed=1) is not None

    def test_the_sweep_alert_shares_the_one_message_latch(
        self, state_db
    ) -> None:
        health = DispatchHealth(state_db, threshold=3)
        assert health.record_sweep(total=5, failed=5) is not None
        assert health.record_sweep(total=5, failed=5) is None
        # And it does not double up with the consecutive alert either:
        # the run is already latched, so further failures stay silent.
        assert run_failures(health, 20) == []

    def test_the_sweep_latch_is_cleared_by_a_success(self, state_db) -> None:
        health = DispatchHealth(state_db, threshold=3)
        assert health.record_sweep(total=5, failed=5) is not None
        health.record_success()
        assert health.record_sweep(total=5, failed=5) is not None


class TestTheSharedDatabaseIsNotWedged:
    """The poller and the bridge both open `state.db`, and two pollers
    against one database has happened on this deployment. Every method
    here has to leave the connection with no transaction open."""

    def test_reading_the_count_does_not_open_a_write_transaction(
        self, tmp_path: Path
    ) -> None:
        """A read that writes takes a RESERVED lock for the life of the
        transaction, and then EVERY other connection to the file fails
        with `database is locked`.

        Measured on the first version: a bare `dispatch_failure_count()`
        on one connection made an ordinary `INSERT INTO sessions` on a
        second one fail. A read-shaped call could have wedged the live
        relay's whole database — far worse than the alert it was part of.
        """
        path = tmp_path / "state.db"
        a = StateDB(path)
        b = StateDB(path)
        try:
            a.dispatch_failure_count()
            a.get_dispatch_health()
            assert not a._conn.in_transaction, (
                "a read left a transaction open on the shared database"
            )
            # The observable consequence, asserted as well as the
            # internal flag: another connection can still write.
            b.execute(
                "INSERT INTO sessions (id, pipeline, repo, status, started_at)"
                " VALUES ('x', 'dev', 'o/r', 'running', 0)"
            )
            b.commit()
            assert b.get_session_row("x") is not None
        finally:
            a.close()
            b.close()

    def test_recording_a_failure_leaves_no_transaction_open(
        self, tmp_path: Path
    ) -> None:
        """The write path takes BEGIN IMMEDIATE, so it must commit."""
        path = tmp_path / "state.db"
        a = StateDB(path)
        b = StateDB(path)
        try:
            DispatchHealth(a, threshold=3).record_failure(repo="o/r")
            assert not a._conn.in_transaction
            b.execute(
                "INSERT INTO sessions (id, pipeline, repo, status, started_at)"
                " VALUES ('y', 'dev', 'o/r', 'running', 0)"
            )
            b.commit()
        finally:
            a.close()
            b.close()

    def test_a_success_recorded_elsewhere_is_seen_by_the_next_failure(
        self, tmp_path: Path
    ) -> None:
        """The increment, the comparison and the claim are one
        transaction, so a peer's success cannot land inside them.

        What this can assert from one thread is the consequence: a
        reset committed by connection B is visible to A's next
        decision, so A does not announce a relay that has recovered.
        """
        path = tmp_path / "state.db"
        a = StateDB(path)
        b = StateDB(path)
        try:
            ha = DispatchHealth(a, threshold=3)
            run_failures(ha, 2)
            b.reset_dispatch_failures()
            # A's next failure starts the run again from B's reset, so
            # it is the first of a new run and must not alert.
            assert ha.record_failure(repo="o/r") is None
            assert a.dispatch_failure_count() == 1
        finally:
            a.close()
            b.close()


class TestThresholdValidation:
    @pytest.mark.parametrize("bad", [0, -1, -99])
    def test_a_threshold_below_one_is_refused_loudly(
        self, state_db, bad: int
    ) -> None:
        """Not silently coerced.

        A zero threshold means "alert on everything" or "alert on
        nothing" depending on whether the comparison is `>=` or `>`,
        and neither is a setting anybody asked for. The error names
        the switch that does mean "off".
        """
        with pytest.raises(ValueError) as excinfo:
            DispatchHealth(state_db, threshold=bad)
        assert "alerts.enabled" in str(excinfo.value)


class TestAlertMessage:
    def test_the_message_names_the_count_and_the_threshold(
        self, state_db
    ) -> None:
        health = DispatchHealth(state_db, threshold=3)
        text = run_failures(health, 3)[0].message()
        assert "3 dispatches in a row" in text
        assert "threshold 3" in text

    def test_the_message_names_the_last_repo_and_exit_code(
        self, state_db
    ) -> None:
        health = DispatchHealth(state_db, threshold=1)
        alert = health.record_failure(
            repo="AInvirion/zzsites", session_id="s", exit_code=1
        )
        assert alert is not None
        assert "AInvirion/zzsites" in alert.message()
        assert "exit code 1" in alert.message()

    def test_the_message_ends_in_something_to_do(self, state_db) -> None:
        """A warning with no action in it is a shrug, and a shrug in a
        channel carrying routine sweep chatter reads as routine sweep
        chatter."""
        text = run_failures(DispatchHealth(state_db, threshold=1), 1)[0].message()
        assert "credential" in text
        assert "journalctl" in text
        # And it says it will not nag, so a reader does not wait for a
        # second one as confirmation.
        assert "not be repeated" in text

    def test_the_sweep_message_says_every_repo_failed(self, state_db) -> None:
        health = DispatchHealth(state_db, threshold=3)
        alert = health.record_sweep(total=88, failed=88)
        assert alert is not None
        text = alert.message()
        assert "every repo failed" in text
        assert "88" in text


class TestTheTransactionLeftOpenBranchIsVisible:
    def test_an_already_open_transaction_is_reported(
        self, tmp_path: Path, caplog
    ) -> None:
        """`_begin_immediate`'s docstring says it logs this. It must.

        A caller leaving a transaction open is a bug somewhere else,
        and committing it quietly is how that bug stays unfound. An
        earlier version claimed the log in prose and did not emit it,
        which made the sentence false and the branch invisible.
        """
        import logging

        db = StateDB(tmp_path / "state.db")
        # A caller mid-write, as `StateDB.execute` leaves it.
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("left-open", "dev", "o/r", "running", 0),
        )
        assert db._conn.in_transaction
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            DispatchHealth(db, threshold=3).record_failure(repo="o/r")
        assert any(
            r.getMessage() == "state.transaction_left_open"
            for r in caplog.records
        ), "the open transaction was committed without being reported"
        # And the caller's pending write was COMMITTED, not discarded.
        # Rolling it back would destroy data to tidy up a lock.
        assert db.get_session_row("left-open") is not None
        assert not db._conn.in_transaction
        db.close()
