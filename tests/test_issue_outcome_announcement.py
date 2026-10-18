"""Tests for `announce_issue_outcome` (#204).

This block used to be inline inside `handle_issue`, which is a closure
inside a Typer command, and it carried a defect nothing could reach:
`failure_text` was imported inside `if is_task:` and called in the
shared failure branch, so a **dev**-lane failure raised
``NameError: name 'failure_text' is not defined``. The surrounding
``except Exception`` turned that into a yellow console line, so the
``❌ Failed on #N`` message could not be sent for any dev issue —
which is the lane `zzsites#853` was dispatched on.

A *task* failure sent it fine, which is why the branch read as
working, and the only symptom was an absent message.

`test_a_dev_failure_is_announced` is the regression. On the pre-fix
code the dev branch reached `failure_text` with the name unbound, so
it failed rather than asserting — whether that surfaces as a
`NameError` depends on how the import is removed, which is why the
test asserts on the message that must be sent and not on an exception
type.
"""

import pytest

from ctrlrelay.cli import announce_issue_outcome
from ctrlrelay.pipelines.base import PipelineResult


class FakeTransport:
    """Records what was sent, in order, and nothing else.

    A fake rather than a mock because the SEQUENCE is part of the
    claim for the success path — `announce_dev_success` puts the
    unreviewed-pull-request warning first on purpose.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, **kwargs) -> None:
        self.sent.append(text)


def failed(**kw) -> PipelineResult:
    return PipelineResult(
        success=False,
        session_id="dev-o-r-853-abc",
        summary="No checkpoint state returned",
        **kw,
    )


class TestFailureIsAnnounced:
    @pytest.mark.asyncio
    async def test_a_dev_failure_is_announced(self) -> None:
        """The regression. Fails on the pre-fix code, where the dev
        branch reached `failure_text` with the name unbound."""
        t = FakeTransport()
        sent = await announce_issue_outcome(
            t, repo="o/r", issue_number=853, is_task=False, result=failed(exit_code=1)
        )
        assert len(t.sent) == 1
        assert sent == t.sent
        assert t.sent[0].startswith("❌ Failed on #853:")

    @pytest.mark.asyncio
    async def test_a_task_failure_is_announced_too(self) -> None:
        """The branch that always worked. Kept so a future change that
        fixes one lane and breaks the other is caught either way."""
        t = FakeTransport()
        await announce_issue_outcome(
            t, repo="o/r", issue_number=853, is_task=True, result=failed(exit_code=1)
        )
        assert t.sent[0].startswith("❌ Failed on #853:")

    @pytest.mark.asyncio
    async def test_the_message_carries_the_reason_and_the_exit_code(
        self,
    ) -> None:
        """Not just that something failed.

        `failure_text` keeps both the summary and the exit code,
        because "exited 0 and wrote nothing" is a different fault from
        "crashed" — the distinction `Unknown error` destroyed in #174.
        """
        t = FakeTransport()
        await announce_issue_outcome(
            t, repo="o/r", issue_number=853, is_task=False, result=failed(exit_code=1)
        )
        assert "No checkpoint state returned" in t.sent[0]
        assert "exit code 1" in t.sent[0]


class TestOtherOutcomesAreUnchanged:
    @pytest.mark.asyncio
    async def test_a_blocked_run_announces_the_question(self) -> None:
        t = FakeTransport()
        await announce_issue_outcome(
            t,
            repo="o/r",
            issue_number=853,
            is_task=False,
            result=PipelineResult(
                success=False,
                blocked=True,
                session_id="s",
                summary="Blocked on user input",
                question="which column?",
            ),
        )
        assert t.sent == ["⏸️ Blocked on #853: which column?"]

    @pytest.mark.asyncio
    async def test_a_task_success_names_the_task_lane(self) -> None:
        t = FakeTransport()
        await announce_issue_outcome(
            t,
            repo="o/r",
            issue_number=853,
            is_task=True,
            result=PipelineResult(
                success=True, session_id="s", summary="report posted"
            ),
        )
        assert t.sent == ["✅ Task done on #853 (o/r): report posted"]

    @pytest.mark.asyncio
    async def test_a_dev_success_calls_announce_dev_success(self) -> None:
        """The delegation itself, not just its output.

        Asserting only the two resulting messages would pass for a
        duplicated implementation inlined here — which is exactly the
        drift the extraction exists to prevent.
        """
        from unittest.mock import AsyncMock, patch

        import ctrlrelay.cli as cli_module

        t = FakeTransport()
        with patch.object(
            cli_module, "announce_dev_success", new=AsyncMock(return_value=[])
        ) as delegate:
            await announce_issue_outcome(
                t,
                repo="o/r",
                issue_number=853,
                is_task=False,
                result=PipelineResult(
                    success=True, session_id="s", summary="PR #42 opened"
                ),
            )
        delegate.assert_awaited_once()
        assert delegate.await_args.kwargs["repo"] == "o/r"

    @pytest.mark.asyncio
    async def test_a_dev_success_goes_through_announce_dev_success(
        self,
    ) -> None:
        """Two messages, warning first.

        Asserted here as well as in `test_unreviewed_pr_announcement`
        because this function is now the only caller — a refactor that
        drops the delegation would otherwise be invisible.
        """
        from ctrlrelay.pipelines.dev import REVIEW_NOT_REPORTED

        t = FakeTransport()
        await announce_issue_outcome(
            t,
            repo="o/r",
            issue_number=853,
            is_task=False,
            result=PipelineResult(
                success=True,
                session_id="s",
                summary="PR #42 opened",
                outputs={"pr_url": "https://github.com/o/r/pull/42"},
                review=REVIEW_NOT_REPORTED,
            ),
        )
        assert len(t.sent) == 2
        assert "https://github.com/o/r/pull/42" in t.sent[-1]
