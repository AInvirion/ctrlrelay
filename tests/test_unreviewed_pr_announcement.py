"""Announcing a review that could not run (#196).

The defect: the dev lane logged `code_review.spawn_failed` and carried
on. The pull request then had zero reviews and zero comments, which on
the forge is indistinguishable from a review that has not run yet — so
`AInvirion/zzsites#842` read as finished for forty minutes having never
been looked at, on a repository whose rule is reviewed-plus-green.

`dev.code_review.not_posted` had fired ONCE in the entire poller log,
so this was a first occurrence and not a known-noisy event.

A failure whose only symptom is the absence of an artifact is
indistinguishable from the healthy case. That is the actual defect, and
a log line nobody is watching is not an announcement.

Note what this ticket is NOT: it was filed claiming the orchestrator
never announces a pull request it opens. It does — `cli.py` sends
`✅ PR ready: {url}` unconditionally on dev success, and that claim came
from searching the live config for `notify`/`announce` keys instead of
reading the send path. Absence of a setting is not absence of a
feature. The routine line is therefore left exactly as it was.
"""

from __future__ import annotations

import pytest

from ctrlrelay.cli import unreviewed_pr_warning
from ctrlrelay.pipelines.base import PipelineResult
from ctrlrelay.pipelines.dev import REVIEW_NOT_REPORTED, ReviewReport


def _result(**outputs) -> PipelineResult:
    return PipelineResult(
        success=True, session_id="s", summary="ok", outputs=dict(outputs)
    )


class TestWhenItWarns:
    def test_a_reviewer_that_could_not_spawn_is_announced(self) -> None:
        """The case the ticket is about."""
        warning = unreviewed_pr_warning(
            "AInvirion/zzsites",
            _result(
                pr_number=842,
                review_posted=False,
                review_status="failed",
                review_detail="FileNotFoundError: 'thereviewer'",
            ),
        )
        assert warning is not None
        # It names where, so the message stands alone in a channel.
        assert "AInvirion/zzsites#842" in warning
        # It carries the reason, so nobody has to go and find the log.
        assert "FileNotFoundError" in warning
        # And it ends in what to do. A warning with no action in it
        # gets dismissed, and a dismissed warning is the silent pass
        # this ticket removes.
        assert "Read the diff before you merge." in warning

    def test_it_says_zero_comments_is_not_the_same_as_clean(self) -> None:
        """The sentence that answers the forge's own ambiguity.

        Zero reviews and zero comments is what a reviewed-and-clean
        pull request looks like too, which is why the absence had to be
        said out loud rather than left to be inferred.
        """
        warning = unreviewed_pr_warning(
            "o/r",
            _result(pr_number=1, review_posted=False, review_status="failed"),
        )
        assert warning is not None
        assert "does not mean it is clean" in warning

    def test_a_missing_key_warns_rather_than_reading_as_fine(self) -> None:
        """Absent is not reviewed.

        The dev lane writes `review_posted` on every path, so its
        absence means something did not report. Defaulting that to
        `True` would reinstate the defect, and defaulting it to a bare
        `False` would lose the distinction from a review that was
        deliberately switched off.
        """
        warning = unreviewed_pr_warning("o/r", _result(pr_number=1))
        assert warning is not None
        assert REVIEW_NOT_REPORTED.detail in warning

    @pytest.mark.parametrize("value", ["false", "0", "", 0, 1, "true", None])
    def test_only_a_real_true_counts_as_reviewed(self, value) -> None:
        """These outputs are merged with an agent's own checkpoint.

        So the value can be whatever the agent wrote, and
        `bool("false")` is True — which would silence the warning,
        the exact direction this ticket exists to prevent. Anything
        that is not the boolean `True` has to read as not posted.

        Found by mutation: swapping `is True` back to `bool(...)` left
        every other case in this file green.
        """
        warning = unreviewed_pr_warning(
            "o/r",
            _result(
                pr_number=1, review_posted=value, review_status="failed"
            ),
        )
        assert warning is not None, value

    def test_a_real_true_does_silence_it(self) -> None:
        """The other direction, so the above cannot pass for a
        function that always warns."""
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1, review_posted=True, review_status="reviewed"
                ),
            )
            is None
        )

    def test_an_absent_pr_number_still_produces_a_usable_line(self) -> None:
        """The outputs come from an agent and need not be complete."""
        warning = unreviewed_pr_warning(
            "o/r", _result(review_posted=False, review_status="failed")
        )
        assert warning is not None
        assert "o/r" in warning
        assert "#None" not in warning

    def test_every_status_the_product_can_emit_is_classified(self) -> None:
        """Read out of the producing code, not listed by hand.

        An earlier version of this carried a hand-written list and a
        docstring claiming it was walked, which was the opposite of
        true: a new reviewer outcome added later would not have been in
        it. The statuses are now taken from the `ReviewReport(...)`
        constructions in `dev.py`, so adding one and forgetting it here
        is impossible — and every one of them that is not a deliberate
        setting has to produce a warning.
        """
        import ast
        from pathlib import Path as _Path

        import ctrlrelay.pipelines.dev as dev_module
        from ctrlrelay.core.code_review import ReviewStatus

        tree = ast.parse(_Path(dev_module.__file__).read_text())
        emitted = set()
        forwards_outcome = False
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ReviewReport"
            ):
                continue
            for kw in node.keywords:
                if kw.arg != "status":
                    continue
                if isinstance(kw.value, ast.Constant):
                    emitted.add(kw.value.value)
                elif (
                    # `status=outcome.status.value` — this branch
                    # forwards whatever the reviewer produced, so the
                    # enum is the authority for those.
                    isinstance(kw.value, ast.Attribute)
                    and kw.value.attr == "value"
                ):
                    forwards_outcome = True
                else:
                    raise AssertionError(
                        "a ReviewReport status is computed in a way "
                        "this test cannot read, so it can no longer "
                        "enumerate the statuses: "
                        + ast.dump(kw.value)[:160]
                    )

        assert forwards_outcome, (
            "nothing forwards the reviewer's own status any more; the "
            "enum below may no longer be a producer"
        )
        # Both producers: the literals here, and the enum they forward.
        emitted |= {member.value for member in ReviewStatus}
        assert len(emitted) >= 6, emitted
        deliberate = {"reviewed", "skipped", "not_commented"}
        failures = emitted - deliberate
        assert failures, emitted
        for status in sorted(failures):
            warning = unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1, review_posted=False, review_status=status
                ),
            )
            assert warning is not None, status
        # And the three deliberate ones stay quiet, so this cannot
        # pass for a classifier that warns about everything.
        for status in sorted(deliberate & emitted):
            posted = status == "reviewed"
            assert (
                unreviewed_pr_warning(
                    "o/r",
                    _result(
                        pr_number=1,
                        review_posted=posted,
                        review_status=status,
                    ),
                )
                is None
            ), status

    def test_only_the_three_intended_outcomes_are_quiet(self) -> None:
        """Each quiet status pinned to the branch that may emit it.

        The test above derives its expectations from the code it is
        checking, so a MISLABELLED producer is self-consistent with
        it: relabel the base_unknown branch to `status="skipped"` and
        a real failure goes silent while the derived set still adds up.
        Found by mutation.

        So this one is deliberately an enumeration — of intent, checked
        against the code. Adding a `ReviewReport` construction, or
        changing one's status, breaks it and forces a decision here
        rather than silently widening what is quiet.
        """
        import ast
        from pathlib import Path as _Path

        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(_Path(dev_module.__file__).read_text())
        built: list[tuple[object, object]] = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ReviewReport"
            ):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            status = kw.get("status")
            posted = kw.get("posted")
            built.append(
                (
                    status.value
                    if isinstance(status, ast.Constant)
                    else "<computed>",
                    posted.value
                    if isinstance(posted, ast.Constant)
                    else "<computed>",
                )
            )

        quiet = [
            pair
            for pair in built
            if pair[0] in {"reviewed", "skipped", "not_commented"}
        ]
        # Exactly three, one per reason:
        #   reviewed       — findings reached the pull request
        #   skipped        — code_review.method is 'off' for this repo
        #   not_commented  — comment_on_pr is off, findings in the log
        assert sorted(quiet) == sorted(
            [
                ("reviewed", True),
                ("skipped", False),
                ("not_commented", False),
            ]
        ), quiet


class TestWhenItStaysQuiet:
    """The other direction. A check that can only say yes is half a check.

    Without these, a warning on every pull request would pass the
    tests above and teach everybody to ignore it — which is the same
    outcome as saying nothing.
    """

    def test_a_posted_review_is_not_announced(self) -> None:
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1, review_posted=True, review_status="reviewed"
                ),
            )
            is None
        )

    def test_review_switched_off_is_not_announced(self) -> None:
        """The operator's own decision is not a fault.

        Warning on every pull request for a setting somebody chose is
        how a warning teaches people to ignore warnings.
        """
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1,
                    review_posted=False,
                    review_status="skipped",
                    review_detail="code_review.method is 'off'",
                ),
            )
            is None
        )

    def test_findings_kept_in_the_log_on_purpose_is_not_announced(
        self,
    ) -> None:
        """`comment_on_pr: false` is a setting, so its outcome is not news.

        A classification test, and it says so: it fabricates the
        status rather than running a reviewer. It does NOT establish
        that anybody read the log — nothing in the product guarantees
        that, and claiming it would be a sentence no code backs.
        """
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1,
                    review_posted=False,
                    review_status="not_commented",
                ),
            )
            is None
        )


class TestTheReportItself:
    def test_is_failure_excludes_only_the_two_deliberate_cases(self) -> None:
        # `is False` / `is True`, because `not x` and `assert x` both
        # pass for a property that returns None.
        assert ReviewReport(True, "reviewed").is_failure is False
        assert ReviewReport(False, "skipped").is_failure is False
        assert ReviewReport(False, "not_commented").is_failure is False
        assert ReviewReport(False, "failed").is_failure is True
        assert ReviewReport(False, "anything_new").is_failure is True

    def test_a_status_nobody_has_written_yet_reads_as_a_failure(
        self,
    ) -> None:
        """The default direction is the safe one.

        A reviewer outcome added later and not classified here is
        announced rather than swallowed — the opposite of an
        allowlist, where the unknown case disappears.
        """
        assert ReviewReport(False, "some_future_status").is_failure is True

    def test_the_sentence_survives_an_empty_detail(self) -> None:
        """`detail` comes from an exception string and can be blank."""
        line = ReviewReport(False, "failed", "").failure_sentence()
        assert "failed" in line
        assert "()" not in line

    def test_the_not_reported_sentinel_is_a_failure_and_says_why(
        self,
    ) -> None:
        assert REVIEW_NOT_REPORTED.is_failure
        assert "recorded no review outcome" in REVIEW_NOT_REPORTED.detail


class TestEveryExitReports:
    """`_review_and_comment` returned `None` on every path.

    That is the root of #196: findings posted, reviewer missing and
    review switched off all produced the same value, so the caller
    could not tell a pull request that had been looked at from one that
    had not.
    """

    @staticmethod
    def _github():
        from unittest.mock import AsyncMock

        gh = AsyncMock()
        gh.comment_on_pr = AsyncMock(return_value=None)
        return gh

    @pytest.mark.asyncio
    async def test_review_switched_off(self, tmp_path) -> None:
        from types import SimpleNamespace

        from ctrlrelay.pipelines.dev import _review_and_comment

        report = await _review_and_comment(
            github=self._github(),
            repo="o/r",
            session_id="s",
            pr_number=1,
            worktree_path=tmp_path,
            base_branch="main",
            config=SimpleNamespace(method="off"),
        )
        assert report.status == "skipped"
        assert not report.is_failure

    @pytest.mark.asyncio
    async def test_a_reviewer_that_cannot_spawn(self, tmp_path) -> None:
        """Driven through a real exec of a name that does not exist.

        This is the measured production failure:
        `FileNotFoundError` on the reviewer's name.
        """
        from types import SimpleNamespace

        from ctrlrelay.pipelines.dev import _review_and_comment

        report = await _review_and_comment(
            github=self._github(),
            repo="o/r",
            session_id="s",
            pr_number=1,
            worktree_path=tmp_path,
            base_branch="main",
            config=SimpleNamespace(
                method="cli",
                cli_command="/nonexistent/thereviewer review",
                timeout_seconds=30,
                comment_on_pr=True,
            ),
        )
        assert report.is_failure, report
        assert report.status == "failed"
        assert "FileNotFoundError" in report.detail

    @pytest.mark.asyncio
    async def test_findings_posted(self, tmp_path, monkeypatch) -> None:
        import stat
        from types import SimpleNamespace

        from ctrlrelay.core import binaries
        from ctrlrelay.pipelines.dev import _review_and_comment

        userbin = tmp_path / "bin"
        userbin.mkdir()
        script = userbin / "thereviewer"
        script.write_text(
            '#!/bin/sh\necho "$ git diff origin/main...HEAD"\n'
            'echo "Looks fine."\nexit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(userbin))
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", ())

        gh = self._github()
        report = await _review_and_comment(
            github=gh,
            repo="o/r",
            session_id="s",
            pr_number=1,
            worktree_path=tmp_path,
            base_branch="main",
            config=SimpleNamespace(
                method="cli",
                cli_command="thereviewer",
                timeout_seconds=30,
                comment_on_pr=True,
            ),
        )
        assert report.posted, report
        assert not report.is_failure
        gh.comment_on_pr.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_comment_that_fails_to_post(
        self, tmp_path, monkeypatch
    ) -> None:
        """The review ran and its findings did not land.

        Worth announcing: the branch was looked at but the pull
        request still carries nothing, so a reader is in the same
        position as if nobody had looked.
        """
        import stat
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from ctrlrelay.core import binaries
        from ctrlrelay.pipelines.dev import _review_and_comment

        userbin = tmp_path / "bin"
        userbin.mkdir()
        script = userbin / "thereviewer"
        script.write_text(
            '#!/bin/sh\necho "$ git diff origin/main...HEAD"\n'
            'echo "Looks fine."\nexit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(userbin))
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", ())

        gh = self._github()
        gh.comment_on_pr = AsyncMock(side_effect=RuntimeError("403"))
        report = await _review_and_comment(
            github=gh,
            repo="o/r",
            session_id="s",
            pr_number=1,
            worktree_path=tmp_path,
            base_branch="main",
            config=SimpleNamespace(
                method="cli",
                cli_command="thereviewer",
                timeout_seconds=30,
                comment_on_pr=True,
            ),
        )
        assert report.is_failure, report
        assert report.status == "comment_failed"
        assert "RuntimeError" in report.detail

    def test_no_exit_can_return_none(self) -> None:
        """Structural, so a path added later cannot reintroduce it.

        Read from the syntax tree rather than from the behavioural
        cases above, because those cover the exits that exist today and
        the fault was an exit that reported nothing.
        """
        import ast
        from pathlib import Path as _Path

        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(_Path(dev_module.__file__).read_text())
        func = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_review_and_comment"
            ),
            None,
        )
        assert func is not None, "_review_and_comment is gone from dev.py"

        returns = [n for n in ast.walk(func) if isinstance(n, ast.Return)]
        assert returns, "the function has stopped returning anything"
        bad = [
            n.lineno
            for n in returns
            if n.value is None
            or (isinstance(n.value, ast.Constant) and n.value.value is None)
        ]
        assert not bad, (
            f"line(s) {bad} leave _review_and_comment without saying what "
            "happened to the review, which is #196"
        )

        # And it cannot fall off the end, which returns None without
        # any `Return` for the check above to see. Wrapping the whole
        # body in an `if` is the mutation that shape misses.
        last = func.body[-1]
        assert isinstance(last, (ast.Return, ast.Raise, ast.Try)), (
            "_review_and_comment can fall through to the end of its "
            f"body, which returns None; last statement is "
            f"{type(last).__name__} at line {last.lineno}"
        )
        if isinstance(last, ast.Try):
            # The real shape today: a try whose body and every handler
            # return. An earlier version exempted the body with
            # `or block_name == "body"`, so deleting the successful
            # `return ReviewReport(...)` passed — found by review.
            assert any(
                isinstance(st, (ast.Return, ast.Raise))
                for st in last.body
            ), "the final try's body can fall through, returning None"
            for handler in last.handlers:
                assert any(
                    isinstance(st, (ast.Return, ast.Raise))
                    for st in handler.body
                ), (
                    f"the handler at line {handler.lineno} can fall "
                    "through, returning None"
                )
            assert not last.orelse or any(
                isinstance(st, (ast.Return, ast.Raise))
                for st in last.orelse
            ), "the final try's else can fall through"


class TestTheOutcomeReachesTheCaller:
    """The report is useless if it does not leave the pipeline."""

    def test_the_review_call_site_merges_it_into_the_outputs(self) -> None:
        """Structural: driving the whole dev pipeline to assert one key
        is not worth it, but the wiring is exactly what #196 needed.

        `unreviewed_pr_warning` reads `review_posted` out of
        `result.outputs`, and treats an absent key as a failure — so if
        this wiring breaks, every pull request is announced as
        unreviewed. Loud rather than silent, which is the right
        direction, and still worth a guard.
        """
        import ast
        from pathlib import Path as _Path

        import ctrlrelay.pipelines.dev as dev_module

        source = _Path(dev_module.__file__).read_text()
        tree = ast.parse(source)
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "run_dev_issue"
        )
        # Assigned, not merely mentioned: a dead read of these keys
        # would satisfy a search for the strings alone.
        written: dict[str, set[str]] = {}
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and target.slice.value.startswith("review_")
                ):
                    written.setdefault(target.value.id, set()).add(
                        target.slice.value
                    )
        assert len(written) == 1, (
            f"the review keys are written to {len(written)} different "
            f"names: {written}"
        )
        holder, keys = next(iter(written.items()))
        assert keys == {
            "review_posted",
            "review_status",
            "review_detail",
        }, keys

        # And that name has to become a PipelineResult's outputs, or
        # the keys are written somewhere nobody reads.
        passed_on = [
            kw
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PipelineResult"
            for kw in node.keywords
            if kw.arg == "outputs"
            and isinstance(kw.value, ast.Name)
            and kw.value.id == holder
        ]
        assert passed_on, (
            f"{holder!r} is populated and never passed as a "
            "PipelineResult's outputs"
        )


class _FakeTransport:
    """Records what it was asked to send, in order."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.sent: list[str] = []
        self._fail_on = fail_on

    async def send(self, message: str, **_kw) -> None:
        if self._fail_on is not None and len(self.sent) == self._fail_on:
            raise OSError("bridge went away")
        self.sent.append(message)


class TestTheWarningIsActuallySent:
    """Driven, not read.

    Three review rounds kept finding the same limit in the `ast`
    guards that used to stand here: they can show a call exists and
    cannot show it runs. `if False: await transport.send(warning)`
    satisfied the transport, await, ordering and own-message checks
    while restoring the original silence. So the sequence moved into
    `announce_dev_success`, which a fake transport can watch.
    """

    @pytest.mark.asyncio
    async def test_both_messages_go_out_when_there_is_no_review(
        self,
    ) -> None:
        from ctrlrelay.cli import announce_dev_success

        transport = _FakeTransport()
        await announce_dev_success(
            transport,
            repo="AInvirion/zzsites",
            result=_result(
                pr_number=842,
                pr_url="https://example/842",
                review_posted=False,
                review_status="failed",
                review_detail="FileNotFoundError",
            ),
        )
        assert len(transport.sent) == 2, transport.sent
        # Separate messages. A warning concatenated onto a success
        # line is read as part of the success line.
        assert "NO REVIEW ON THIS PR" in transport.sent[0]
        assert transport.sent[1] == "✅ PR ready: https://example/842"
        assert "NO REVIEW" not in transport.sent[1]

    @pytest.mark.asyncio
    async def test_the_warning_is_sent_first(self) -> None:
        """Both sends share one `except` in the caller, so the second
        is what a mid-sequence transport failure loses — and of the two
        the warning is the one this ticket is about."""
        from ctrlrelay.cli import announce_dev_success

        # A transport that dies on its SECOND send.
        transport = _FakeTransport(fail_on=1)
        with pytest.raises(OSError):
            await announce_dev_success(
                transport,
                repo="o/r",
                result=_result(
                    pr_number=1,
                    pr_url="u",
                    review_posted=False,
                    review_status="failed",
                ),
            )
        # The warning survived; the routine line is what was lost.
        assert len(transport.sent) == 1, transport.sent
        assert "NO REVIEW ON THIS PR" in transport.sent[0]

    @pytest.mark.asyncio
    async def test_a_reviewed_pr_gets_only_the_routine_line(self) -> None:
        """The other direction, so the above cannot pass for something
        that always sends two."""
        from ctrlrelay.cli import announce_dev_success

        transport = _FakeTransport()
        await announce_dev_success(
            transport,
            repo="o/r",
            result=_result(
                pr_number=1,
                pr_url="u",
                review_posted=True,
                review_status="reviewed",
            ),
        )
        assert transport.sent == ["✅ PR ready: u"]

    @pytest.mark.asyncio
    async def test_the_routine_line_survives_a_missing_pr_url(self) -> None:
        """`outputs` comes from an agent and need not be complete."""
        from ctrlrelay.cli import announce_dev_success

        transport = _FakeTransport()
        await announce_dev_success(
            transport,
            repo="o/r",
            result=_result(review_posted=True, review_status="reviewed"),
        )
        assert transport.sent == ["✅ PR ready: "]

    @pytest.mark.asyncio
    async def test_what_it_returns_is_what_it_sent(self) -> None:
        """The return value is what the caller's tests read, so it has
        to be the truth rather than a second construction of it."""
        from ctrlrelay.cli import announce_dev_success

        transport = _FakeTransport()
        returned = await announce_dev_success(
            transport,
            repo="o/r",
            result=_result(
                pr_number=1,
                pr_url="u",
                review_posted=False,
                review_status="failed",
            ),
        )
        assert returned == transport.sent

    def test_handle_issue_delegates_to_it(self) -> None:
        """The one claim left to structure, and it is a weak one.

        Everything about the order and the two messages is asserted
        above against a real transport. All this adds is that the dev
        branch of `handle_issue` routes through that function rather
        than growing its own copy — `handle_issue` is a closure inside
        `poller_start`, which opens a state database and polls 87
        repositories.

        It cannot show the call is reachable. Nothing structural can,
        which is why the behaviour moved out rather than the guard
        getting a fourth patch.
        """
        import ast
        from pathlib import Path as _Path

        import ctrlrelay.cli as cli_module

        tree = ast.parse(_Path(cli_module.__file__).read_text())
        handle_issue = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "handle_issue"
            ),
            None,
        )
        assert handle_issue is not None, "handle_issue is gone from cli.py"
        calls = [
            n
            for n in ast.walk(handle_issue)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "announce_dev_success"
        ]
        assert len(calls) == 1, (
            f"expected exactly one announce_dev_success call, got "
            f"{len(calls)}"
        )
        # And it does not also hand-roll the routine line, which is how
        # the two messages would drift apart again.
        literals = " ".join(
            n.value
            for n in ast.walk(handle_issue)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        assert "PR ready" not in literals, (
            "handle_issue builds a 'PR ready' line of its own; the "
            "announcement lives in announce_dev_success"
        )


class TestTheSafeDefault:
    """The initial value is a guard for a branch nobody has added yet.

    It survives a mutation because every branch today overwrites it —
    which is the point. Removing the initialisation entirely would be
    worse than a wrong default: a new branch that forgot to set the
    name would raise `UnboundLocalError` out of `run_dev_issue`, and
    the poller marks an issue seen before handling it, so that drops
    the issue's work rather than losing a message.
    """

    def test_it_starts_as_not_reported(self) -> None:
        import ast
        from pathlib import Path as _Path

        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(_Path(dev_module.__file__).read_text())
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "run_dev_issue"
        )
        # Dominance, not "first by line". An unreachable
        # `if False: review_report = REVIEW_NOT_REPORTED` above a wrong
        # live default satisfies a first-by-line check — found by
        # mutation. So: find the block that merges the review keys,
        # walk its ancestors, and require the default to be a DIRECT
        # statement of one of them, before the merge, with no
        # unconditional reassignment after it in that same block.
        parent: dict[int, ast.AST] = {}
        for node in ast.walk(func):
            for child in ast.iter_child_nodes(node):
                parent[id(child)] = node

        merge = next(
            (
                n
                for n in ast.walk(func)
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "review_posted"
                    for t in n.targets
                )
            ),
            None,
        )
        assert merge is not None, "nothing merges review_posted any more"

        def _blocks_containing(stmt):
            """Every statement list an ancestor of ``stmt`` owns."""
            seen = []
            node = stmt
            while id(node) in parent:
                owner = parent[id(node)]
                for field in ("body", "orelse", "finalbody"):
                    block = getattr(owner, field, None)
                    if isinstance(block, list) and node in block:
                        seen.append(block)
                node = owner
            return seen

        default_found = False
        for block in _blocks_containing(merge):
            def _is_default(st) -> bool:
                """`x = DEFAULT` or `x: T = DEFAULT` — both are fine."""
                if isinstance(st, ast.Assign):
                    names = [
                        t.id for t in st.targets if isinstance(t, ast.Name)
                    ]
                elif isinstance(st, ast.AnnAssign) and isinstance(
                    st.target, ast.Name
                ):
                    names = [st.target.id]
                else:
                    return False
                return (
                    "review_report" in names
                    and isinstance(st.value, ast.Name)
                    and st.value.id == "REVIEW_NOT_REPORTED"
                )

            defaults = [
                i for i, st in enumerate(block) if _is_default(st)
            ]
            if not defaults:
                continue
            # Assignment, annotated assignment or `del`: all three
            # change what an unset path sees, and an earlier version
            # looked only for `Assign` — so `del review_report` passed
            # and would raise UnboundLocalError.
            overrides = []
            for i, st in enumerate(block):
                if i <= defaults[0]:
                    continue
                if isinstance(st, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "review_report"
                    for t in st.targets
                ):
                    overrides.append(i)
                elif (
                    isinstance(st, ast.AnnAssign)
                    and isinstance(st.target, ast.Name)
                    and st.target.id == "review_report"
                ):
                    overrides.append(i)
                elif isinstance(st, ast.Delete) and any(
                    isinstance(t, ast.Name) and t.id == "review_report"
                    for t in st.targets
                ):
                    overrides.append(i)
            assert not overrides, (
                "review_report is reassigned or deleted unconditionally "
                f"after its default at block positions {overrides}, so "
                "the default is not what an unset path gets"
            )
            default_found = True
            break
        assert default_found, (
            "no block containing the merge sets review_report to "
            "REVIEW_NOT_REPORTED as one of its own statements"
        )

        assigns = sorted(
            (
                n
                for n in ast.walk(func)
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "review_report"
                    for t in n.targets
                )
            ),
            key=lambda n: (n.lineno, n.col_offset),
        )
        uses = [
            n.lineno
            for n in ast.walk(func)
            if isinstance(n, ast.Name)
            and n.id == "review_report"
            and isinstance(n.ctx, ast.Load)
        ]
        assert uses, "review_report is assigned and never read"
        assert assigns[0].lineno < min(uses), (
            f"the default is assigned at line {assigns[0].lineno} but "
            f"the name is read from line {min(uses)}"
        )
