"""The resume prompt must restate the checkpoint contract (#173).

The initial prompt spends ~30 lines establishing how an agent signals
how it stopped. The resume prompt was **50 characters of template** plus
the operator's answer - 58 in the incident below, because "Approved" is
eight of them. (#173 quotes 58 as the length; that is true of that one
answer, not in general.)

    User answered: {answer}

    Continue from where you left off.

So a resumed agent could do its work, exit 0 with empty stderr, write no
`state.json`, and be indistinguishable from one that crashed.

Measured case, 2026-09-16: `secops-AInvirion-Product-Telemetry-bbb99925`
resumed after an "Approved", ran 27.4s, exited 0 and wrote no checkpoint
- having merged three of four pull requests. The operator was told the
resume failed, which was wrong in both directions.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PIPELINES = ("secops", "dev", "task")


def _pipeline(mod: str):
    cls = {
        "secops": "SecopsPipeline",
        "dev": "DevPipeline",
        "task": "TaskPipeline",
    }[mod]
    module = __import__(f"ctrlrelay.pipelines.{mod}", fromlist=[cls])
    return getattr(module, cls)(
        dispatcher=AsyncMock(),
        github=MagicMock(),
        worktree=MagicMock(),
        dashboard=MagicMock(),
        state_db=MagicMock(),
        transport=MagicMock(),
    )


def _ctx(tmp_path: Path):
    from ctrlrelay.pipelines.base import PipelineContext

    return PipelineContext(
        session_id="sid-1",
        repo="owner/repo",
        worktree_path=tmp_path,
        context_path=tmp_path / "CLAUDE.md",
        state_file=tmp_path / ".ctrlrelay" / "state.json",
        issue_number=7,
    )


@pytest.mark.parametrize("mod", PIPELINES)
@pytest.mark.asyncio
async def test_the_resume_prompt_carries_the_checkpoint_contract(
    mod: str, tmp_path: Path
) -> None:
    """Asserted on the prompt the dispatcher is actually handed.

    Not on `_checkpoint_contract` in isolation: that would pass while
    `resume` ignored it, which is the defect.
    """
    from ctrlrelay.core.dispatcher import SessionResult

    pipeline = _pipeline(mod)
    ctx = _ctx(tmp_path)
    pipeline.state_db.get_agent_session_id.return_value = (
        "ff5333ea-0000-0000-0000-000000000000"
    )
    pipeline.dispatcher.spawn_session.return_value = SessionResult(
        session_id="sid-1", exit_code=0, state=None, stderr=""
    )

    await pipeline.resume(ctx, "Approved")

    assert pipeline.dispatcher.spawn_session.await_count == 1
    sent = pipeline.dispatcher.spawn_session.await_args

    # The PROMPT only. An earlier version of this test joined every
    # argument into one blob, and `working_dir` happens to contain the
    # state file path - so the "names the state file" assertion passed
    # against the reverted code, for a reason that had nothing to do
    # with the prompt.
    assert "prompt" in sent.kwargs, (
        f"prompt is not a keyword argument here: {sent.kwargs.keys()}"
    )
    prompt = sent.kwargs["prompt"]
    assert isinstance(prompt, str)

    assert "Approved" in prompt, "the answer must still reach the agent"
    assert "Continue from where you left off" in prompt

    # The contract, named by the things it must tell the agent.
    assert str(ctx.state_file) in prompt, (
        "the resume prompt must name the state file the agent writes to"
    )
    assert "BLOCKED_NEEDS_INPUT" in prompt
    assert "FAILED" in prompt
    assert '"status":"DONE"' in prompt


@pytest.mark.parametrize("mod", PIPELINES)
def test_the_contract_is_one_source_shared_with_the_initial_prompt(
    mod: str, tmp_path: Path
) -> None:
    """One source per pipeline, and each pipeline's own.

    `dev`'s DONE block carries `pr_url` and `pr_number`, and the three
    name different conditions for DONE. A single shared block would have
    quietly changed what two of them promise.

    Scope, stated because the first name overclaimed: this does NOT
    drive `resume`, and reverting `resume` to the old one-liner leaves it
    green. It pins that the initial prompt renders the SAME text the
    helper returns, so the two cannot drift. The resume wiring is held
    by `test_the_resume_prompt_carries_the_checkpoint_contract` above,
    and only by that one.
    """
    pipeline = _pipeline(mod)
    contract = pipeline._checkpoint_contract(
        "/wt/.ctrlrelay/state.json", "sid-1"
    )

    # `contract in initial` is satisfied by the empty string, so the
    # containment assertion below proves nothing on its own. Pin that
    # there is a contract first.
    assert len(contract.splitlines()) >= 20, contract
    assert "/wt/.ctrlrelay/state.json" in contract
    assert "BLOCKED_NEEDS_INPUT" in contract

    if mod == "secops":
        initial = pipeline._build_prompt(
            "owner/repo",
            session_id="sid-1",
            state_file=Path("/wt/.ctrlrelay/state.json"),
        )
    else:
        initial = pipeline._build_prompt(
            "owner/repo",
            7,
            {},
            session_id="sid-1",
            state_file=Path("/wt/.ctrlrelay/state.json"),
        )

    assert contract in initial, (
        "the initial prompt must render the same contract text the resume "
        "prompt does; if these diverge, one of them is a stale copy"
    )
    if mod == "dev":
        assert "pr_url" in contract, "dev's DONE carries the PR outputs"
    else:
        assert "pr_url" not in contract


@pytest.mark.parametrize("mod", PIPELINES)
def test_every_checkpoint_snippet_creates_its_own_directory(mod: str) -> None:
    """Each snippet must be self-sufficient (#189).

    `task.py` had the `mkdir -p` on DONE only. That cost nothing, because
    the orchestrator pre-creates the directory at `task.py:343` and
    `:605` - so this was latent, not a live defect, and it is asserted
    here as a consistency property rather than a bug repro.

    The reason it is worth pinning: the `mkdir` is what makes a snippet
    independent of who created the directory. Without it, a snippet
    relies on a pre-creation two call sites away with nothing connecting
    them, and if that ever moves, **BLOCKED and FAILED stop being
    writable while DONE keeps working** - the failure appears only on the
    paths that report trouble, and looks exactly like the agent crashing.

    Counted rather than read, and counted on the RENDERED contract rather
    than the source, so a fourth pipeline is held the day it is added.

    Deliberately not asserted: that the agent writes BLOCKED
    successfully. That passes today with the fault present, because the
    directory already exists - it would measure the pre-creation and not
    this.
    """
    contract = _pipeline(mod)._checkpoint_contract(
        "/wt/.ctrlrelay/state.json", "sid-1"
    )

    writes = contract.count("printf ")
    mkdirs = contract.count('mkdir -p "$(dirname ')

    # Negative control: a contract that rendered no snippets at all would
    # otherwise satisfy 0 == 0.
    assert writes == 3, (
        f"{mod}: expected DONE, BLOCKED and FAILED snippets, found {writes}"
    )
    assert mkdirs == writes, (
        f"{mod}: {writes} checkpoint snippets but only {mkdirs} create the "
        "directory first; the ones without it depend on somebody else "
        "having made it"
    )
