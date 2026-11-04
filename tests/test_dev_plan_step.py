"""The dev pipeline's plan step (#220).

With ``steps.plan`` set, one planning session runs before the code
session. It changes no files and returns a plan whose ``## Acceptance
criteria`` list is what "done" means. The relay saves the plan under the
contexts directory (outside the worktree, so it is never committed),
posts the criteria on the issue, and puts the plan in the code session's
prompt. Unset is ``off``: no plan session, and the prompt as before.

Each case drives the real ``run_dev_issue`` (or ``resume_dev_from_pending``)
with a dispatcher that answers the plan prompt and the code prompt
differently, and checks what the operator would see.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import ctrlrelay.pipelines.dev as dev
from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.config import (
    CodeReviewConfig,
    ConfigError,
    DevStepsConfig,
    load_config,
)
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.state import StateDB
from tests.test_dev_step_switches import GREEN, _fakes, _verifier, _write

GOOD_PLAN = """## Change
Make the widget round.

## Files
- src/widget.py

## Risks
Square callers.

## Acceptance criteria
1. `pytest tests/test_widget.py` passes.
2. The diff changes `src/widget.py` only.
"""

NO_CRITERIA_PLAN = """## Change
Make the widget round.

## Files
- src/widget.py

## Risks
None.

## Acceptance criteria
Whatever seems right.
"""

PLAN_MARK = "You are planning issue"
REVIEW_OFF = CodeReviewConfig(method="off")


def _checkpoint(
    session_id: str,
    status: CheckpointStatus,
    outputs: dict | None = None,
    question: str | None = None,
) -> SessionResult:
    return SessionResult(
        session_id=session_id,
        exit_code=0,
        stdout="",
        stderr="",
        state=CheckpointState(
            version="1",
            status=status,
            session_id=session_id,
            timestamp="2026-11-03T12:00:00Z",
            summary="ok",
            question=question,
            outputs=outputs or {},
        ),
    )


class _Agent:
    """Answers plan prompts with ``plans`` (in order) and code prompts
    with DONE. A plan entry is a plan string, or ``BLOCKED`` to ask."""

    BLOCKED = object()

    def __init__(self, *plans) -> None:
        self.plans = list(plans)
        self.prompts: list[str] = []
        self.dispatcher = AsyncMock()
        self.dispatcher.spawn_session.side_effect = self._spawn

    async def _spawn(self, **kwargs) -> SessionResult:
        prompt = kwargs["prompt"]
        sid = kwargs["session_id"]
        self.prompts.append(prompt)
        is_plan = PLAN_MARK in prompt or "Continue planning" in prompt
        if not is_plan:
            return _checkpoint(sid, CheckpointStatus.DONE)
        plan = self.plans.pop(0)
        if plan is self.BLOCKED:
            return _checkpoint(
                sid, CheckpointStatus.BLOCKED_NEEDS_INPUT,
                question="Round or oval?",
            )
        return _checkpoint(sid, CheckpointStatus.DONE, {"plan": plan})

    @property
    def plan_prompts(self) -> list[str]:
        return [
            p for p in self.prompts
            if PLAN_MARK in p or "Continue planning" in p
        ]

    @property
    def code_prompts(self) -> list[str]:
        return [p for p in self.prompts if p not in self.plan_prompts]


def _plan_comments(github) -> list[str]:
    return [
        c.kwargs["body"]
        for c in github.comment_on_issue.await_args_list
        if "Plan for this issue" in c.kwargs.get("body", "")
        or "Planning did not" in c.kwargs.get("body", "")
    ]


async def _run(
    tmp_path: Path, agent: _Agent, plan=None, transport=None, fakes=None
):
    worktree, github = fakes or _fakes(tmp_path)
    steps = None if plan is None else DevStepsConfig(plan=plan)
    result = await dev.run_dev_issue(
        claims=None,
        repo="owner/repo",
        issue_number=123,
        branch_template="fix/issue-{n}",
        dispatcher=agent.dispatcher,
        github=github,
        worktree=worktree,
        dashboard=None,
        state_db=StateDB(tmp_path / "s.db"),
        transport=transport,
        contexts_dir=tmp_path / "contexts",
        pr_verifier=_verifier(GREEN),
        code_review=REVIEW_OFF,
        steps=steps,
    )
    return result, github


def _plan_file(tmp_path: Path) -> Path:
    return tmp_path / "contexts" / "owner-repo" / "plans" / "issue-123.md"


# --- off ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [None, "off"])
async def test_without_plan_no_plan_session_runs(tmp_path, plan) -> None:
    agent = _Agent(GOOD_PLAN)
    result, github = await _run(tmp_path, agent, plan=plan)

    assert result.success, result
    assert agent.plan_prompts == []
    assert len(agent.code_prompts) == 1
    assert "## Plan" not in agent.code_prompts[0]
    assert _plan_comments(github) == []
    assert not _plan_file(tmp_path).exists()


# --- on, good plan -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_good_plan_is_saved_posted_and_given_to_the_coder(
    tmp_path,
) -> None:
    agent = _Agent(GOOD_PLAN)
    result, github = await _run(tmp_path, agent, plan="on")

    assert result.success, result
    assert result.outputs["pr_number"] == 42
    # One plan session, then one code session, in that order.
    assert len(agent.plan_prompts) == 1
    assert agent.prompts[0] == agent.plan_prompts[0]
    assert "Change no files" in agent.plan_prompts[0]
    assert len(agent.code_prompts) == 1

    # Saved under contexts, not in the worktree.
    saved = _plan_file(tmp_path)
    assert saved.read_text().strip() == GOOD_PLAN.strip()
    worktree_dir = tmp_path / "worktree"
    assert not any(
        "issue-123.md" in p.name for p in worktree_dir.rglob("*")
    )

    # One comment, carrying the criteria and not the rest of the plan.
    comments = _plan_comments(github)
    assert len(comments) == 1
    assert "1. `pytest tests/test_widget.py` passes." in comments[0]
    assert "2. The diff changes `src/widget.py` only." in comments[0]
    assert "Square callers" not in comments[0]

    # The coder's prompt has the plan, criteria included, under ## Plan.
    code = agent.code_prompts[0]
    assert "## Plan" in code
    assert "1. `pytest tests/test_widget.py` passes." in code
    assert 'what "done" means' in code


# --- on / required, plan without criteria ------------------------------------


@pytest.mark.asyncio
async def test_on_a_plan_without_criteria_is_reported_and_code_runs(
    tmp_path,
) -> None:
    agent = _Agent(NO_CRITERIA_PLAN)
    result, github = await _run(tmp_path, agent, plan="on")

    assert result.success, result
    assert len(agent.code_prompts) == 1
    assert "## Plan" not in agent.code_prompts[0]
    comments = _plan_comments(github)
    assert len(comments) == 1
    assert "Planning did not produce a usable plan" in comments[0]
    assert "no numbered acceptance criteria" in comments[0]
    assert not _plan_file(tmp_path).exists()


@pytest.mark.asyncio
async def test_required_a_plan_without_criteria_fails_the_run(
    tmp_path,
) -> None:
    agent = _Agent(NO_CRITERIA_PLAN)
    result, github = await _run(tmp_path, agent, plan="required")

    assert not result.success
    assert not result.blocked
    assert "plan is required" in result.summary
    assert "no numbered acceptance criteria" in (result.error or "")
    assert agent.code_prompts == []
    github.create_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_a_good_plan_hands_over(tmp_path) -> None:
    agent = _Agent(GOOD_PLAN)
    result, _ = await _run(tmp_path, agent, plan="required")
    assert result.success, result
    assert "## Plan" in agent.code_prompts[0]


@pytest.mark.parametrize(
    "plan",
    [
        "",
        "## Acceptance criteria\n",
        "## Change\nx\n\n## Acceptance criteria\n- a bullet, not numbered\n",
        # Numbered items under another heading are not criteria.
        "## Acceptance criteria\nlater\n\n## Risks\n1. something\n",
    ],
)
def test_plan_criteria_refuses_a_plan_without_numbered_criteria(plan) -> None:
    assert dev.plan_criteria(plan) is None


# --- blocked -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_blocked_plan_asks_once_then_resumes_with_the_answer(
    tmp_path,
) -> None:
    agent = _Agent(_Agent.BLOCKED, GOOD_PLAN)
    transport = AsyncMock()
    seen_at_ask: dict = {}

    async def ask(question, **kwargs):
        seen_at_ask["code_sessions"] = len(agent.code_prompts)
        seen_at_ask["prs"] = github_box[0].create_pr.await_count
        return "Round."

    transport.ask.side_effect = ask
    worktree, github = _fakes(tmp_path)
    github_box = [github]
    result, _ = await _run(
        tmp_path, agent, plan="on", transport=transport,
        fakes=(worktree, github),
    )

    assert transport.ask.await_count == 1
    assert transport.ask.await_args.args[0] == "Round or oval?"
    # While the question was open: no code session, no pull request.
    assert seen_at_ask == {"code_sessions": 0, "prs": 0}
    # The resume carried the answer under the PLAN contract.
    assert len(agent.plan_prompts) == 2
    resumed = agent.plan_prompts[1]
    assert "User answered: Round." in resumed
    assert '"plan": r' in resumed
    assert "pr_title" not in resumed
    # Then the code ran with the plan.
    assert result.success, result
    assert "## Plan" in agent.code_prompts[0]


@pytest.mark.asyncio
async def test_a_plan_question_answered_later_resumes_the_plan(
    tmp_path,
) -> None:
    """No transport: the run stops BLOCKED with no PR. The answer arriving
    later resumes the planning session, then codes with the plan."""
    agent = _Agent(_Agent.BLOCKED, GOOD_PLAN)
    worktree, github = _fakes(tmp_path)
    state_db = StateDB(tmp_path / "s.db")
    steps = DevStepsConfig(plan="required")
    common = dict(
        repo="owner/repo", dispatcher=agent.dispatcher, github=github,
        worktree=worktree, dashboard=None, state_db=state_db,
        transport=None, contexts_dir=tmp_path / "contexts",
        pr_verifier=_verifier(GREEN), code_review=REVIEW_OFF, steps=steps,
        branch_template="fix/issue-{n}",
    )
    first = await dev.run_dev_issue(claims=None, issue_number=123, **common)

    assert first.blocked, first
    assert first.question == "Round or oval?"
    assert agent.code_prompts == []
    github.create_pr.assert_not_awaited()

    second = await dev.resume_dev_from_pending(
        session_id=first.session_id, answer="Round.", **common,
    )

    assert second.success, second
    assert len(agent.plan_prompts) == 2
    assert "User answered: Round." in agent.plan_prompts[1]
    assert len(agent.code_prompts) == 1
    assert "## Plan" in agent.code_prompts[0]
    assert _plan_file(tmp_path).exists()
    assert not _plan_file(tmp_path).with_suffix(".pending").exists()
    github.create_pr.assert_awaited_once()


# --- config ------------------------------------------------------------------


def test_plan_bogus_is_refused_at_load(
    tmp_path: Path, sample_config_dict: dict
) -> None:
    path = _write(
        tmp_path,
        sample_config_dict,
        """
        repos:
          - name: owner/repo
            local_path: /tmp/owner-repo
            steps:
              plan: bogus
        """,
    )
    with pytest.raises(ConfigError, match=r"steps\.plan"):
        load_config(path)


@pytest.mark.parametrize("mode", ["off", "on", "required"])
def test_plan_accepts_its_three_modes(
    tmp_path: Path, sample_config_dict: dict, mode: str
) -> None:
    path = _write(
        tmp_path,
        sample_config_dict,
        f"""
        repos:
          - name: owner/repo
            local_path: /tmp/owner-repo
            steps:
              plan: {mode}
        """,
    )
    assert load_config(path).repos[0].steps.plan == mode


def test_plan_unset_is_none() -> None:
    assert DevStepsConfig().plan is None
