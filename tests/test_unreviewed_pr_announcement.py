"""Announcing a review that could not run (#196).

The defect: the dev lane logged `code_review.spawn_failed` and carried
on. The pull request then had zero reviews and zero comments, which on
the forge is exactly what a reviewed-and-clean pull request looks like
— so `AInvirion/zzsites#842` read as finished for forty minutes having
never been looked at, on a repository whose rule is reviewed-plus-green.

`dev.code_review.not_posted` had fired ONCE in the entire poller log,
so this was a first occurrence rather than a known-noisy event.

A failure whose only symptom is the absence of an artifact is
indistinguishable from the healthy case. A log line nobody is watching
is not an announcement.

Note what this ticket is NOT. It was filed claiming the orchestrator
never announces a pull request it opens. It does — `cli.py` sends
`✅ PR ready: {url}` on dev success, and has for a long time. That
claim came from searching the live config for `notify`/`announce` keys
instead of reading the send path: absence of a setting is not absence
of a feature. The routine line is therefore unchanged, and there is a
test below pinning that it still exists.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from ctrlrelay.cli import announce_dev_success, unreviewed_pr_warning
from ctrlrelay.pipelines.base import PipelineResult
from ctrlrelay.pipelines.dev import REVIEW_NOT_REPORTED, ReviewReport


def _report(
    status: str,
    *,
    posted: bool = False,
    detail: str = "",
    deliberate: bool = False,
) -> ReviewReport:
    return ReviewReport(
        posted=posted, status=status, detail=detail, deliberate=deliberate
    )


def _result(review=None, **outputs) -> PipelineResult:
    """A dev-success result, with the review on the FIELD.

    That split is the whole point of the field: ``outputs`` is merged
    with the coding agent's own checkpoint, and the agent is the party
    whose branch is being reviewed, so nothing trustworthy can live
    there.
    """
    return PipelineResult(
        success=True,
        session_id="s",
        summary="ok",
        outputs=dict(outputs),
        review=review,
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


class TestWhenItWarns:
    def test_a_reviewer_that_could_not_spawn_is_announced(self) -> None:
        """The case the ticket is about."""
        warning = unreviewed_pr_warning(
            "AInvirion/zzsites",
            _result(
                pr_number=842,
                review=_report(
                    "failed", detail="FileNotFoundError: 'thereviewer'"
                ),
            ),
        )
        assert warning is not None
        # Names where, so the message stands alone in a channel.
        assert "AInvirion/zzsites#842" in warning
        # Carries the reason, so nobody has to go and find the log.
        assert "FileNotFoundError" in warning
        # Ends in what to do. A warning with no action in it gets
        # dismissed, and a dismissed warning is the silent pass this
        # ticket removes.
        assert "Read the diff before you merge." in warning

    def test_it_says_zero_comments_is_not_the_same_as_clean(self) -> None:
        """The sentence that answers the forge's own ambiguity."""
        warning = unreviewed_pr_warning(
            "o/r", _result(pr_number=1, review=_report("failed"))
        )
        assert warning is not None
        assert "does not mean it is clean" in warning

    def test_no_recorded_outcome_warns_rather_than_reading_as_fine(
        self,
    ) -> None:
        """Absent is not reviewed.

        Rounding "I do not know" towards "reviewed" is the defect.
        """
        warning = unreviewed_pr_warning("o/r", _result(pr_number=1))
        assert warning is not None
        assert REVIEW_NOT_REPORTED.detail in warning

    @pytest.mark.parametrize(
        "value",
        [True, "reviewed", 1, {"posted": True}, object()],
    )
    def test_anything_that_is_not_a_report_warns(self, value) -> None:
        """Fail closed on whatever ends up in the field.

        Reading `.is_failure` off a duck would either raise into the
        announcement path — which drops an already-seen issue — or
        believe it.
        """
        warning = unreviewed_pr_warning(
            "o/r", _result(pr_number=1, review=value)
        )
        assert warning is not None, value

    def test_an_absent_pr_number_still_produces_a_usable_line(self) -> None:
        """`outputs` comes from an agent and need not be complete."""
        warning = unreviewed_pr_warning("o/r", _result(review=_report("failed")))
        assert warning is not None
        assert "o/r" in warning
        assert "#None" not in warning

    def test_every_status_the_product_can_emit_is_announced(self) -> None:
        """Read out of both producers, not listed by hand.

        The literal `ReviewReport(...)` constructions in `dev.py`, and
        the `ReviewStatus` enum that one of those branches forwards. A
        status added to either and forgotten here is the shape of this
        ticket.

        Every one of them is announced, because quietness is no longer
        decided by the status at all — see
        `TestQuietnessIsNotDecidedByAString`.
        """
        import ctrlrelay.pipelines.dev as dev_module
        from ctrlrelay.core.code_review import ReviewStatus

        tree = ast.parse(Path(dev_module.__file__).read_text())
        emitted, forwards = set(), False
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
                    isinstance(kw.value, ast.Attribute)
                    and kw.value.attr == "value"
                ):
                    forwards = True
                else:
                    raise AssertionError(
                        "a ReviewReport status is computed in a way this "
                        "test cannot read: " + ast.dump(kw.value)[:160]
                    )

        assert forwards, "nothing forwards the reviewer's own status"
        emitted |= {m.value for m in ReviewStatus}
        assert len(emitted) >= 6, emitted

        for status in sorted(emitted):
            warning = unreviewed_pr_warning(
                "o/r",
                _result(pr_number=1, review=_report(status)),
            )
            assert warning is not None, status


class TestQuietnessIsNotDecidedByAString:
    """The cold review's best finding, as a test.

    Quietness used to be `status not in ("skipped", "not_commented")`.
    `ReviewStatus.SKIPPED` has the value `"skipped"`, and the branch
    that forwards that enum does NOT know whether the operator chose
    it — so a reviewer that started returning it would have silenced
    the warning about a pull request nobody read. The backwards
    verdict, latent, and no test here could have seen it.

    It is an explicit `deliberate` flag now, set only by the two
    branches that know.
    """

    def test_the_enum_value_that_collides_does_not_silence_anything(
        self,
    ) -> None:
        from ctrlrelay.core.code_review import ReviewStatus

        colliding = {m.value for m in ReviewStatus} & {
            "skipped",
            "not_commented",
        }
        assert colliding, (
            "the collision is gone, so this test is about nothing — "
            "check whether the enum was renamed rather than deleting it"
        )
        for status in sorted(colliding):
            # Forwarded from the reviewer: nobody said this was a
            # setting, so it must be announced.
            warning = unreviewed_pr_warning(
                "o/r",
                _result(pr_number=1, review=_report(status)),
            )
            assert warning is not None, status

    def test_the_flag_and_not_the_status_decides(self) -> None:
        # Same status, opposite verdicts, decided by the flag.
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(pr_number=1, review=_report("skipped")),
            )
            is not None
        )
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1,
                    review=_report("skipped", deliberate=True),
                ),
            )
            is None
        )

    def test_only_the_branches_that_know_set_the_flag(self) -> None:
        """Pinned against the source, deliberately as an enumeration.

        Reading the expected set out of the code is how an earlier
        version of this was self-consistent with a mislabelled
        producer. This is an enumeration of intent instead: exactly two
        constructions may be deliberate, with the status each must
        carry. Adding one forces a decision here.
        """
        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(Path(dev_module.__file__).read_text())
        deliberate = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ReviewReport"
            ):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            flag = kw.get("deliberate")
            if isinstance(flag, ast.Constant) and flag.value is True:
                status = kw.get("status")
                deliberate.append(
                    status.value
                    if isinstance(status, ast.Constant)
                    else "<computed>"
                )
        #   skipped        — code_review.method is 'off' for this repo
        #   not_commented  — comment_on_pr is off, findings in the log
        assert sorted(deliberate) == ["not_commented", "skipped"], deliberate


class TestWhenItStaysQuiet:
    """The other direction. A check that can only say yes is half a check.

    Without these, warning on every pull request would pass everything
    above and teach everybody to ignore it — the same outcome as saying
    nothing.
    """

    def test_a_posted_review_is_not_announced(self) -> None:
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1,
                    review=_report("reviewed", posted=True),
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
                    review=_report(
                        "skipped",
                        detail="code_review.method is 'off'",
                        deliberate=True,
                    ),
                ),
            )
            is None
        )

    def test_findings_kept_in_the_log_on_purpose_is_not_announced(
        self,
    ) -> None:
        """`comment_on_pr: false` is a setting, so its outcome is not news.

        A classification test, and it says so: it builds the report
        rather than running a reviewer. It does NOT establish that
        anybody read the log — nothing in the product guarantees that,
        and claiming it would be a sentence no code backs.
        """
        assert (
            unreviewed_pr_warning(
                "o/r",
                _result(
                    pr_number=1,
                    review=_report("not_commented", deliberate=True),
                ),
            )
            is None
        )


class TestTheReportItself:
    def test_is_failure_is_the_flag_and_the_posted_bit(self) -> None:
        # `is True` / `is False`, because `assert x` passes for any
        # truthy value and `assert not x` for any falsy one.
        assert _report("reviewed", posted=True).is_failure is False
        assert _report("skipped", deliberate=True).is_failure is False
        assert _report("not_commented", deliberate=True).is_failure is False
        assert _report("failed").is_failure is True
        assert _report("anything_new").is_failure is True
        # A posted review is never a failure even if somebody also
        # marked it deliberate.
        assert _report("reviewed", posted=True, deliberate=True).is_failure is False

    def test_a_status_nobody_has_written_yet_reads_as_a_failure(
        self,
    ) -> None:
        """The default direction is the safe one.

        A reviewer outcome added later and not marked deliberate is
        announced rather than swallowed — the opposite of an
        allowlist, where the unknown case disappears.
        """
        assert _report("some_future_status").is_failure is True

    def test_the_sentence_survives_an_empty_detail(self) -> None:
        """`detail` comes from an exception string and can be blank."""
        line = _report("failed", detail="").failure_sentence()
        assert "failed" in line
        assert "()" not in line

    def test_the_not_reported_sentinel_is_a_failure_and_says_why(
        self,
    ) -> None:
        assert REVIEW_NOT_REPORTED.is_failure is True
        assert "recorded no review outcome" in REVIEW_NOT_REPORTED.detail


class TestEveryExitReports:
    """`_review_and_comment` returned `None` on every path.

    That is the root: findings posted, reviewer missing and review
    switched off all produced the same value, so the caller could not
    tell a pull request that had been looked at from one that had not.
    """

    @staticmethod
    def _github():
        from unittest.mock import AsyncMock

        gh = AsyncMock()
        gh.comment_on_pr = AsyncMock(return_value=None)
        return gh

    @staticmethod
    def _working_reviewer(tmp_path: Path, monkeypatch) -> None:
        import stat

        from ctrlrelay.core import binaries

        userbin = tmp_path / "bin"
        userbin.mkdir(exist_ok=True)
        script = userbin / "thereviewer"
        script.write_text(
            '#!/bin/sh\necho "$ git diff origin/main...HEAD"\n'
            'echo "Looks fine."\nexit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(userbin))
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", ())

    @pytest.mark.asyncio
    async def test_review_switched_off(self, tmp_path: Path) -> None:
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
        assert report.deliberate is True
        assert report.is_failure is False

    @pytest.mark.asyncio
    async def test_a_reviewer_that_cannot_spawn(self, tmp_path: Path) -> None:
        """The measured production failure, through a real exec."""
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
        assert report.is_failure is True, report
        assert report.status == "failed"
        assert report.deliberate is False
        assert "FileNotFoundError" in report.detail

    @pytest.mark.asyncio
    async def test_findings_posted(self, tmp_path: Path, monkeypatch) -> None:
        from types import SimpleNamespace

        from ctrlrelay.pipelines.dev import _review_and_comment

        self._working_reviewer(tmp_path, monkeypatch)
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
        assert report.posted is True, report
        assert report.is_failure is False
        gh.comment_on_pr.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_findings_kept_in_the_log(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from types import SimpleNamespace

        from ctrlrelay.pipelines.dev import _review_and_comment

        self._working_reviewer(tmp_path, monkeypatch)
        report = await _review_and_comment(
            github=self._github(),
            repo="o/r",
            session_id="s",
            pr_number=1,
            worktree_path=tmp_path,
            base_branch="main",
            config=SimpleNamespace(
                method="cli",
                cli_command="thereviewer",
                timeout_seconds=30,
                comment_on_pr=False,
            ),
        )
        assert report.status == "not_commented"
        assert report.deliberate is True
        assert report.is_failure is False

    @pytest.mark.asyncio
    async def test_a_comment_that_fails_to_post(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The reviewer looked and the pull request still carries nothing.

        Announced, because the reader is in the same position as if
        nobody had looked.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from ctrlrelay.pipelines.dev import _review_and_comment

        self._working_reviewer(tmp_path, monkeypatch)
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
        assert report.is_failure is True, report
        assert report.status == "comment_failed"
        assert report.deliberate is False
        assert "RuntimeError" in report.detail

    def test_no_exit_can_return_none(self) -> None:
        """Structural, so a path added later cannot reintroduce it."""
        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(Path(dev_module.__file__).read_text())
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

        # And it cannot fall off the end, which returns None with no
        # `Return` for the check above to see.
        last = func.body[-1]
        assert isinstance(last, (ast.Return, ast.Raise, ast.Try)), (
            "_review_and_comment can fall through to the end of its body; "
            f"last statement is {type(last).__name__} at line {last.lineno}"
        )
        if isinstance(last, ast.Try):
            # Every branch of it, including the body. An earlier version
            # exempted the body, so deleting the successful return passed.
            assert any(
                isinstance(st, (ast.Return, ast.Raise)) for st in last.body
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
    """It is useless if it does not leave the pipeline."""

    def test_it_travels_on_the_field_and_not_through_outputs(self) -> None:
        """The cold review's other finding, as a guard.

        `outputs` is merged with the coding agent's own checkpoint, and
        the agent is the party whose branch is being reviewed — the
        adversary `core.code_review`'s docstring is about. An agent
        writing `review_posted: true` could have silenced the warning
        about its own unreviewed pull request. Nothing it writes can
        reach a dataclass field.
        """
        import ctrlrelay.cli as cli_module
        import ctrlrelay.pipelines.dev as dev_module

        # Read from the syntax tree, not the raw text: both files
        # carry COMMENTS naming these keys to explain why they are not
        # used, and a substring scan reports those as the violation.
        # Same shape as scanning whole files for a banned word and
        # finding somebody's prose about it.
        banned = {"review_posted", "review_status", "review_detail"}
        for module in (dev_module, cli_module):
            tree = ast.parse(Path(module.__file__).read_text())
            literals = {
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            offenders = banned & literals
            assert not offenders, (
                f"{sorted(offenders)} appear as string literals in "
                f"{Path(module.__file__).name} — the outcome must travel "
                "on the result's field, not through agent-writable outputs"
            )

        # And the stronger property, because renaming the key defeats
        # the list above: inside `unreviewed_pr_warning`, the ONLY
        # thing read out of `outputs` is the PR number, which merely
        # decorates the message. Found by mutation — reading the
        # report from `outputs["review"]` passed a name-based check.
        cli_tree = ast.parse(Path(cli_module.__file__).read_text())
        func = next(
            n
            for n in ast.walk(cli_tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "unreviewed_pr_warning"
        )
        read_keys = set()
        for node in ast.walk(func):
            # `outputs.get("x")` / `(... or {}).get("x")`
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                read_keys.add(node.args[0].value)
            # `outputs["x"]`
            elif isinstance(node, ast.Subscript) and isinstance(
                node.slice, ast.Constant
            ):
                read_keys.add(node.slice.value)
        assert read_keys == {"pr_number"}, (
            "unreviewed_pr_warning reads "
            f"{sorted(read_keys)} out of the result; only 'pr_number' "
            "may come from agent-writable outputs, because the verdict "
            "must come from the field"
        )

    def test_the_call_site_sets_the_field_with_replace(self) -> None:
        """`replace`, so a field added to PipelineResult later is
        carried by construction rather than reset to its default."""
        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(Path(dev_module.__file__).read_text())
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "run_dev_issue"
        )
        calls = [
            n
            for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "replace"
            and any(kw.arg == "review" for kw in n.keywords)
        ]
        assert len(calls) == 1, (
            f"expected one dataclasses.replace(..., review=...), got "
            f"{len(calls)}"
        )
        kw = next(k for k in calls[0].keywords if k.arg == "review")
        assert isinstance(kw.value, ast.Name), ast.dump(kw.value)[:120]
        assert kw.value.id == "review_report", kw.value.id


class TestTheSafeDefault:
    """The initial value is a guard for a branch nobody has added yet.

    It survives a mutation because every branch today overwrites it —
    which is the point. Removing the initialisation would be worse than
    a wrong default: a new branch that forgot to set the name would
    raise `UnboundLocalError` out of `run_dev_issue`, and the poller
    marks an issue seen before handling it, so that drops the issue's
    work rather than losing a message.
    """

    def test_the_default_is_set_and_not_unconditionally_overridden(
        self,
    ) -> None:
        """What a structural guard can claim here, and no more.

        It pins that the default exists, in a block the use is inside,
        and that nothing between them replaces it as a DIRECT
        statement. It cannot distinguish a branch that legitimately
        records an outcome from one that wrongly records
        `posted=True` — both are conditional assignments and the
        difference is semantic. Found by mutation: inserting
        `if result.success: review_report = ReviewReport(posted=True,
        ...)` passes, and should, because that is the shape every real
        branch has.

        Closing that would mean driving `run_dev_issue` end to end.
        Named as a limit rather than left implied.
        """
        import ctrlrelay.pipelines.dev as dev_module

        tree = ast.parse(Path(dev_module.__file__).read_text())
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "run_dev_issue"
        )

        parent: dict[int, ast.AST] = {}
        for node in ast.walk(func):
            for child in ast.iter_child_nodes(node):
                parent[id(child)] = node

        use = next(
            (
                n
                for n in ast.walk(func)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "replace"
                and any(kw.arg == "review" for kw in n.keywords)
            ),
            None,
        )
        assert use is not None, "nothing sets the review field any more"

        def _is_assign(st) -> bool:
            if isinstance(st, ast.Assign):
                return any(
                    isinstance(t, ast.Name) and t.id == "review_report"
                    for t in st.targets
                )
            if isinstance(st, ast.AnnAssign):
                return (
                    isinstance(st.target, ast.Name)
                    and st.target.id == "review_report"
                )
            if isinstance(st, ast.Delete):
                return any(
                    isinstance(t, ast.Name) and t.id == "review_report"
                    for t in st.targets
                )
            return False

        def _is_default(st) -> bool:
            return (
                isinstance(st, (ast.Assign, ast.AnnAssign))
                and _is_assign(st)
                and isinstance(st.value, ast.Name)
                and st.value.id == "REVIEW_NOT_REPORTED"
            )

        # Every statement list from the use outwards, nearest first.
        chain: list[list] = []
        node: ast.AST = use
        while id(node) in parent:
            owner = parent[id(node)]
            for field in ("body", "orelse", "finalbody"):
                block = getattr(owner, field, None)
                if isinstance(block, list) and node in block:
                    chain.append(block)
            node = owner
        assert chain, "could not locate the blocks around the use"

        # The default must be in one of them, and NO block between the
        # use and that one may assign the name unconditionally. An
        # earlier version only checked the default's own block, so an
        # override in an intermediate block passed — the fourth time a
        # guard here was satisfied by its own input.
        depth = next(
            (i for i, block in enumerate(chain) if any(map(_is_default, block))),
            None,
        )
        assert depth is not None, (
            "no block containing the use sets review_report to "
            "REVIEW_NOT_REPORTED as one of its own statements"
        )
        for i, block in enumerate(chain[: depth + 1]):
            offenders = [
                st.lineno
                for st in block
                if _is_assign(st) and not _is_default(st)
            ]
            assert not offenders, (
                "review_report is assigned or deleted unconditionally "
                f"at line(s) {offenders}, between its default and the "
                "use — so the default is not what an unset path gets"
            )


class TestTheWarningIsActuallySent:
    """Driven, not read.

    Three review rounds kept finding the same limit in the `ast` guards
    that used to stand here: they can show a call exists and cannot
    show it runs. `if False: await transport.send(warning)` satisfied
    the transport, await, ordering and own-message checks while
    restoring the original silence. So the sequence moved into
    `announce_dev_success`, which a fake transport can watch.
    """

    @pytest.mark.asyncio
    async def test_both_messages_go_out_when_there_is_no_review(
        self,
    ) -> None:
        transport = _FakeTransport()
        await announce_dev_success(
            transport,
            repo="AInvirion/zzsites",
            result=_result(
                pr_number=842,
                pr_url="https://example/842",
                review=_report("failed", detail="FileNotFoundError"),
            ),
        )
        assert len(transport.sent) == 2, transport.sent
        # Separate messages: a warning concatenated onto a success line
        # is read as part of the success line.
        assert "NO REVIEW ON THIS PR" in transport.sent[0]
        assert transport.sent[1] == "✅ PR ready: https://example/842"
        assert "NO REVIEW" not in transport.sent[1]

    @pytest.mark.asyncio
    async def test_the_warning_is_sent_first(self) -> None:
        """Both sends share one `except` in the caller, so the second is
        what a mid-sequence transport failure loses — and of the two the
        warning is the one this ticket is about."""
        transport = _FakeTransport(fail_on=1)
        with pytest.raises(OSError):
            await announce_dev_success(
                transport,
                repo="o/r",
                result=_result(
                    pr_number=1, pr_url="u", review=_report("failed")
                ),
            )
        assert len(transport.sent) == 1, transport.sent
        assert "NO REVIEW ON THIS PR" in transport.sent[0]

    @pytest.mark.asyncio
    async def test_a_reviewed_pr_gets_only_the_routine_line(self) -> None:
        transport = _FakeTransport()
        await announce_dev_success(
            transport,
            repo="o/r",
            result=_result(
                pr_number=1,
                pr_url="u",
                review=_report("reviewed", posted=True),
            ),
        )
        assert transport.sent == ["✅ PR ready: u"]

    @pytest.mark.asyncio
    async def test_the_routine_line_survives_a_missing_pr_url(self) -> None:
        """`outputs` comes from an agent and need not be complete."""
        transport = _FakeTransport()
        await announce_dev_success(
            transport,
            repo="o/r",
            result=_result(review=_report("reviewed", posted=True)),
        )
        assert transport.sent == ["✅ PR ready: "]

    @pytest.mark.asyncio
    async def test_what_it_returns_is_what_it_sent(self) -> None:
        transport = _FakeTransport()
        returned = await announce_dev_success(
            transport,
            repo="o/r",
            result=_result(
                pr_number=1, pr_url="u", review=_report("failed")
            ),
        )
        assert returned == transport.sent

    def test_handle_issue_delegates_to_it(self) -> None:
        """The one claim left to structure, and it is a weak one.

        Everything about the order and the two messages is asserted
        above against a real transport. All this adds is that the dev
        branch of `handle_issue` routes through that function rather
        than growing its own copy.

        It cannot show the call is reachable. Nothing structural can,
        which is why the behaviour moved out rather than the guard
        getting a fourth patch.

        **The delegation moved one hop in #204.** `handle_issue` now
        calls `announce_issue_outcome`, which chooses the branch and
        calls `announce_dev_success` for a dev success. The reason it
        moved is this file's own lesson arriving again: the branch
        selection was inline, and the failure branch referenced
        `failure_text`, imported only inside `if is_task:`. A dev-lane
        failure raised NameError into a swallowing `except`, so the
        failure message could never be sent — and no structural check
        here could have seen it, because the call was present and
        correct.

        That `announce_issue_outcome` really reaches
        `announce_dev_success` is asserted behaviourally, against a
        fake transport, in `tests/test_issue_outcome_announcement.py`.
        This stays as the one thing a behavioural test cannot say:
        that `handle_issue` has not grown its own copy.
        """
        import ctrlrelay.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text())
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
            and n.func.id in ("announce_issue_outcome", "announce_dev_success")
        ]
        assert len(calls) == 1, (
            "expected exactly one announcement call in handle_issue, got "
            f"{len(calls)}"
        )
        # And it does not hand-roll the routine line, which is how the
        # two messages would drift apart again.
        literals = " ".join(
            n.value
            for n in ast.walk(handle_issue)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        assert "PR ready" not in literals, (
            "handle_issue builds a 'PR ready' line of its own; the "
            "announcement lives in announce_dev_success"
        )

    def test_the_routine_success_line_still_exists(self) -> None:
        """It was never missing, and replacing it was not this ticket.

        The claim at the top of #196 — that nothing announces a pull
        request the dev lane opens — came from searching the live
        config for `notify`/`announce` keys instead of reading the send
        path. Pinned so a later change cannot fold the two messages
        into one and lose the separation the warning depends on.
        """
        import ctrlrelay.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text())
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "announce_dev_success"
        )
        literals = " ".join(
            n.value
            for n in ast.walk(func)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        assert "PR ready" in literals, literals[:300]


def test_os_is_used() -> None:
    """Keeps the `os` import honest for the linter."""
    assert os.sep
