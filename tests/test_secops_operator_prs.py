"""``repos[].automation.operator_prs``: the operator's own PRs in secops (#335)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from ctrlrelay.core.config import AutomationConfig, OperatorPrsPolicy, load_config
from ctrlrelay.pipelines.secops import SecopsPipeline, run_secops_all

# The rule every secops prompt carried before the setting existed. With the
# setting absent or ``ask`` it must still be there, word for word.
ASK_RULE = (
    "   - **Any other PR from `$OPERATOR`** (touches code, tests, configs\n"
    "     other than dependabot.yml): signal BLOCKED for operator approval.\n"
    "     Never auto-merge code changes, even from the trusted operator.\n"
)
IGNORE_RULE_START = (
    "   - **Any other PR from `$OPERATOR`** (touches anything other\n"
    "     than `.github/dependabot.yml`): out of scope on this repo;"
)
SCOPE_NOTE_START = "On this repo the step-4 results are in scope only after the"
DROP_STEP_START = "   On this repo, drop every step-4 result whose files include"
CARVE_OUT_START = "   - **PR authored by `$OPERATOR` touching ONLY `.github/dependabot.yml`**"
CARVE_OUT_END = "   - **Any other PR from `$OPERATOR`**"


def _pipeline() -> SecopsPipeline:
    return SecopsPipeline(
        dispatcher=MagicMock(),
        github=MagicMock(),
        worktree=MagicMock(),
        dashboard=None,
        state_db=MagicMock(),
        transport=None,
    )


def _prompt(automation: AutomationConfig | None) -> str:
    return _pipeline()._build_prompt(
        repo="o/r",
        session_id="s1",
        state_file=Path("/tmp/state.json"),
        automation=automation,
    )


def _carve_out(prompt: str) -> str:
    start = prompt.index(CARVE_OUT_START)
    end = prompt.index(CARVE_OUT_END, start)
    return prompt[start:end]


class TestConfig:
    def test_absent_is_ask(self) -> None:
        assert AutomationConfig().operator_prs is OperatorPrsPolicy.ASK

    def test_ignore_loads_from_the_config_file(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        sample_config_dict["repos"] = [
            {
                "name": "o/blog",
                "local_path": str(tmp_path / "blog"),
                "automation": {"operator_prs": "ignore"},
            },
            {"name": "o/other", "local_path": str(tmp_path / "other")},
        ]
        path = tmp_path / "orchestrator.yaml"
        path.write_text(yaml.dump(sample_config_dict))

        config = load_config(path)

        by_name = {r.name: r for r in config.repos}
        assert by_name["o/blog"].automation.operator_prs is OperatorPrsPolicy.IGNORE
        assert by_name["o/other"].automation.operator_prs is OperatorPrsPolicy.ASK

    def test_an_unknown_value_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AutomationConfig(operator_prs="skip")


class TestPrompt:
    def test_absent_and_ask_render_the_prompt_unchanged(self) -> None:
        unset = _prompt(None)

        assert _prompt(AutomationConfig()) == unset
        assert _prompt(AutomationConfig(operator_prs="ask")) == unset
        assert ASK_RULE in unset
        assert SCOPE_NOTE_START not in unset
        assert DROP_STEP_START not in unset
        assert IGNORE_RULE_START not in unset

    def test_ignore_takes_other_operator_prs_out_of_scope(self) -> None:
        prompt = _prompt(AutomationConfig(operator_prs="ignore"))

        assert ASK_RULE not in prompt
        assert "signal BLOCKED for operator approval" not in prompt
        rule_start = prompt.index(IGNORE_RULE_START)
        rule = prompt[rule_start:prompt.index("   - **PRs from anyone else**", rule_start)]
        assert "do not signal BLOCKED on them" in rule
        assert "do not\n     name them in the summary" in rule
        # The scope fence says the same, so it cannot call every step-4
        # result in scope.
        fence = prompt[prompt.index("**PR scope fence:**"):]
        assert SCOPE_NOTE_START in fence

    def test_ignore_drops_those_prs_in_step_4_before_any_rule_that_counts_them(
        self,
    ) -> None:
        """The rules after step 4 speak of "each open PR", BLOCKED questions,
        the summary and "what you left alone". The drop has to come first and
        say it governs all of them, or those rules pull the PRs back in."""
        prompt = _prompt(AutomationConfig(operator_prs="ignore"))

        drop = prompt.index(DROP_STEP_START)
        assert prompt.index("4. Check open PRs authored by the trusted operator") < drop
        assert drop < prompt.index("5. For each alert or PR:")
        assert drop < prompt.index("You have exactly three legitimate exits for each open PR")
        assert drop < prompt.index("Every BLOCKED question must contain ONLY items")
        step = prompt[drop:prompt.index("5. For each alert or PR:")]
        for governed in ('"each open PR"', "BLOCKED", "step-6 summary", '"what you left alone"'):
            assert governed in step
        assert "applies only to the PRs that remain" in step
        assert "A dropped PR is not an open PR for this run" in step

    def test_ignore_keeps_the_dependabot_yml_carve_out_word_for_word(self) -> None:
        ignore = _prompt(AutomationConfig(operator_prs="ignore"))
        ask = _prompt(AutomationConfig())

        carve_out = _carve_out(ignore)
        assert carve_out == _carve_out(ask)
        # The three checks before a merge are all still there.
        assert "**Author check**" in carve_out
        assert "**Diff check**" in carve_out
        assert "**CI check**" in carve_out
        # And the step that lists the operator's PRs is still asked for.
        assert '--author "$OPERATOR" --json number,title,files' in ignore

    def test_ignore_leaves_the_dependabot_policy_alone(self) -> None:
        ignore = _prompt(AutomationConfig(operator_prs="ignore"))

        assert "patch updates: AUTO-MERGE" in ignore
        assert "minor updates: ASK" in ignore
        assert "major updates: NEVER" in ignore
        assert '--author "app/dependabot"' in ignore


@pytest.mark.asyncio
async def test_a_sweep_sends_the_repos_setting_to_the_agent(tmp_path: Path) -> None:
    """The path a sweep takes: a repo's config, through run_secops_all,
    to the prompt the agent is given."""
    from ctrlrelay.core.checkpoint import CheckpointStatus
    from ctrlrelay.core.dispatcher import SessionResult

    async def fake_spawn(**kwargs):
        state = MagicMock()
        state.status = CheckpointStatus.DONE
        state.summary = "Done"
        state.outputs = {}
        state.error = None
        return SessionResult(session_id=kwargs["session_id"], exit_code=0, state=state)

    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = fake_spawn
    worktree = AsyncMock()
    worktree.ensure_bare_repo.return_value = tmp_path / "bare"
    worktree.create_worktree.side_effect = lambda repo, sid: tmp_path / repo.replace("/", "-")
    db = MagicMock()
    db.acquire_lock.return_value = True
    db.release_lock.return_value = True

    blog = MagicMock()
    blog.name = "o/blog"
    blog.automation = AutomationConfig(operator_prs="ignore")
    other = MagicMock()
    other.name = "o/other"
    other.automation = AutomationConfig()

    await run_secops_all(
        job_claims=None,
        hold_repeats=False,
        repos=[blog, other],
        dispatcher=dispatcher,
        github=MagicMock(),
        worktree=worktree,
        dashboard=None,
        state_db=db,
        transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    sent = [c.kwargs["prompt"] for c in dispatcher.spawn_session.call_args_list]
    assert len(sent) == 2
    by_repo = {
        ("o/blog" if "repository o/blog." in p else "o/other"): p for p in sent
    }
    assert set(by_repo) == {"o/blog", "o/other"}
    assert IGNORE_RULE_START in by_repo["o/blog"]
    assert ASK_RULE not in by_repo["o/blog"]
    assert ASK_RULE in by_repo["o/other"]
    assert IGNORE_RULE_START not in by_repo["o/other"]
