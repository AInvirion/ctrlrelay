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

    def test_an_absent_pr_number_still_produces_a_usable_line(self) -> None:
        """The outputs come from an agent and need not be complete."""
        warning = unreviewed_pr_warning(
            "o/r", _result(review_posted=False, review_status="failed")
        )
        assert warning is not None
        assert "o/r" in warning
        assert "#None" not in warning

    @pytest.mark.parametrize(
        "status",
        ["failed", "no_evidence", "errored", "base_unknown", "comment_failed"],
    )
    def test_every_way_the_review_can_fail_is_announced(
        self, status: str
    ) -> None:
        """Walked, not listed one at a time.

        Each of these is a distinct way the reviewer never got as far
        as posting, and each produced a pull request that reads as
        finished. A new one added later and forgotten is the shape of
        this ticket.
        """
        warning = unreviewed_pr_warning(
            "o/r",
            _result(pr_number=1, review_posted=False, review_status=status),
        )
        assert warning is not None, status


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
        """`comment_on_pr: false` means the review ran and was read."""
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
        assert not ReviewReport(True, "reviewed").is_failure
        assert not ReviewReport(False, "skipped").is_failure
        assert not ReviewReport(False, "not_commented").is_failure
        assert ReviewReport(False, "failed").is_failure
        assert ReviewReport(False, "anything_new").is_failure

    def test_a_status_nobody_has_written_yet_reads_as_a_failure(
        self,
    ) -> None:
        """The default direction is the safe one.

        A reviewer outcome added later and not classified here is
        announced rather than swallowed — the opposite of an
        allowlist, where the unknown case disappears.
        """
        assert ReviewReport(False, "some_future_status").is_failure

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
        # And every one of them builds a report, so a future exit
        # cannot return some other truthy thing the caller misreads.
        shapes = set()
        for n in returns:
            v = n.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                shapes.add(v.func.id)
            elif isinstance(v, ast.Name):
                shapes.add(v.id)
            else:
                shapes.add(ast.dump(v)[:40])
        assert shapes <= {"ReviewReport", "REVIEW_NOT_REPORTED"}, shapes


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
        keys = {
            node.slice.value
            for node in ast.walk(func)
            if isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value.startswith("review_")
        }
        assert keys == {
            "review_posted",
            "review_status",
            "review_detail",
        }, keys
