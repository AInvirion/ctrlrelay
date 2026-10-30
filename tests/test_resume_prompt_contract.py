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

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _discover_pipelines() -> dict[str, type]:
    """Every pipeline class that renders a checkpoint contract.

    Discovered by walking the package rather than listed here. A
    hardcoded tuple is an enumeration somebody has to remember to
    extend, and the docstrings below claim a pipeline added later is
    covered "the day it is added" - which a list makes false. A review
    round pointed that out and it was right.
    """
    import importlib
    import inspect
    import pkgutil

    import ctrlrelay.pipelines as pkg

    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"ctrlrelay.pipelines.{info.name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if hasattr(obj, "_checkpoint_contract") and hasattr(obj, "resume"):
                found[info.name] = obj
    return found


_DISCOVERED = _discover_pipelines()

# Negative control: if discovery broke, parametrising over an empty dict
# would collect zero tests and report a clean run.
assert set(_DISCOVERED) >= {"secops", "dev", "task"}, (
    f"pipeline discovery found {sorted(_DISCOVERED)}; it must find at "
    "least the three that exist, or these tests are measuring nothing"
)

PIPELINES = tuple(sorted(_DISCOVERED))


def _pipeline(mod: str):
    return _DISCOVERED[mod](
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
    the orchestrator pre-creates the directory - `run` and
    `resume_task_from_pending` both call
    `state_file.parent.mkdir(parents=True, exist_ok=True)` - so this was
    latent, not a live defect, and it is asserted here as a consistency
    property rather than a bug repro.

    (Named by method, not by line: the first version of this docstring
    cited `task.py:343` and `:605`, and the two lines this very change
    inserts shifted them. A line number in prose rots and nothing fails
    when it does - the same fault corrected in #173.)

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

    # Per shell block, so ORDER is checked and not just totals. Counting
    # alone passes for a snippet that writes the file and then creates
    # the directory, which is the same defect with the lines swapped.
    blocks = [
        b.split("```")[0]
        for b in contract.split("```bash")[1:]
    ]

    # Negative control: an empty contract would otherwise satisfy every
    # comparison below by having nothing to compare.
    assert len(blocks) == 3, (
        f"{mod}: expected DONE, BLOCKED and FAILED shell blocks, "
        f"found {len(blocks)}"
    )

    for i, block in enumerate(blocks):
        assert "printf " in block, f"{mod}: block {i} writes nothing"
        mk = block.find("mkdir -p -- ")
        pf = block.find("printf ")
        assert mk != -1, (
            f"{mod}: block {i} writes the checkpoint without creating its "
            "directory, so it depends on somebody else having made it"
        )
        assert mk < pf, (
            f"{mod}: block {i} creates the directory AFTER writing to it"
        )


# Paths the shell has to be careful with, as (id, path, relative). A
# relative path is resolved against the cwd the snippet runs in, which
# is `tmp_path`; an absolute one is placed under `tmp_path`.
#
# - apostrophe: closes a single-quoted string. With hand-written quotes
#   it is a syntax error, or (paired) a silently different path.
# - space and double quote: split an unquoted word / close a
#   double-quoted one.
# - `$` and backtick: expand inside double quotes.
# - trailing newline on the directory that holds the file: `$(dirname
#   ...)` strips trailing newlines, so mkdir made the wrong directory.
# - leading `-` on a relative path: read as an option by dirname/mkdir.
_HOSTILE_PATHS = [
    ("apostrophe", "O'Reilly/.ctrlrelay/state.json", False),
    ("paired-apostrophes", "a'b'c/.ctrlrelay/state.json", False),
    ("space-and-double-quote", 'dir "q" x/.ctrlrelay/state.json', False),
    ("dollar-and-backtick", "cost $HOME `id`/.ctrlrelay/state.json", False),
    ("trailing-newline-dir", "run\n/state.json", False),
    ("leading-dash-relative", "-dash/.ctrlrelay/state.json", True),
]

# What the agent is told to replace. `<PR_NUM>` sits unquoted in a
# `%d` slot, so it must become a number; everything else is a quoted
# string argument.
_PLACEHOLDER = re.compile(r"<[A-Z][A-Z_ -]*>")


def _fill(block: str) -> str:
    return _PLACEHOLDER.sub(
        lambda m: "42" if m.group(0) == "<PR_NUM>" else "filled in",
        block,
    )


@pytest.mark.parametrize("mod", PIPELINES)
@pytest.mark.parametrize(
    ("case", "raw", "relative"),
    _HOSTILE_PATHS,
    ids=[c[0] for c in _HOSTILE_PATHS],
)
def test_every_snippet_writes_a_checkpoint_at_a_path_needing_quotes(
    mod: str, case: str, raw: str, relative: bool, tmp_path: Path
) -> None:
    """A state-file path the shell has to quote must still be writable (#191).

    The snippets hand-wrapped the path in single quotes, so an apostrophe
    in it either closed the quote early (a syntax error: no DONE, no
    BLOCKED, no FAILED) or, when paired, parsed fine and wrote somewhere
    else. Either way the agent did its work and looked like it crashed.

    Asserts the artifact, not the absence of a syntax error: each block
    runs under `bash -c` from `tmp_path`, must exit 0, and must leave a
    JSON file at that exact path carrying the status the block is named
    for. The file's directory is removed between blocks and does not
    exist before the first, so every block is proven to create it.
    """
    work = tmp_path / "work"
    work.mkdir()
    if relative:
        given = raw
        state_file = work / raw
    else:
        state_file = work / raw
        given = str(state_file)
    assert not state_file.parent.exists()

    contract = _pipeline(mod)._checkpoint_contract(given, "sid-1")
    blocks = [
        b.split("```")[0] for b in contract.split("```bash")[1:]
    ]
    assert len(blocks) == 3, f"{mod}: found {len(blocks)} shell blocks"

    expected = ["DONE", "BLOCKED_NEEDS_INPUT", "FAILED"]
    for block, status in zip(blocks, expected, strict=True):
        if state_file.parent.exists():
            shutil.rmtree(state_file.parent)

        script = _fill(block)
        assert not _PLACEHOLDER.search(script), (
            f"{mod}/{case}/{status}: unfilled placeholder in {script!r}"
        )
        run = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=work,
        )
        assert run.returncode == 0, (
            f"{mod}/{case}/{status}: snippet failed with exit "
            f"{run.returncode}:\n{run.stderr}\n--- script ---\n{script}"
        )
        assert state_file.is_file(), (
            f"{mod}/{case}/{status}: exit 0 but no checkpoint at "
            f"{state_file!r}; tree holds {sorted(map(str, work.rglob('*')))}"
        )
        data = json.loads(state_file.read_text())
        assert data["status"] == status
        assert data["session_id"] == "sid-1"
