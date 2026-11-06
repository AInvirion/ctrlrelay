"""Tests for the per-run token budget (#223).

Transcript rows here have the shape measured from a real agent CLI
transcript (version 2.1.x): one API message written as several rows that
repeat the same ``message.usage``, and sub-agents in their own files under
``<session>/subagents/``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ctrlrelay.core.budget import (
    BudgetLimits,
    BudgetScope,
    BudgetWatch,
    RunUsage,
    TranscriptMeter,
    budget_reason,
    is_budget_reason,
    label_names,
    parse_result_cost,
    record_usage,
    resolve_limits,
    session_scope,
)
from ctrlrelay.core.config import AgentConfig, BudgetConfig
from ctrlrelay.core.state import StateDB

T0 = time.time()


def _ts(offset: float = 1.0) -> str:
    """Now plus ``offset``: always after ``T0`` and after a spawn that
    starts within the offset."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def _assistant(
    msg_id: str,
    *,
    inp: int = 2,
    write: int = 0,
    read: int = 0,
    out: int = 0,
    ts: str | None = None,
    block: str = "text",
) -> str:
    """One assistant row, as the CLI writes it (extra fields included)."""
    return json.dumps({
        "type": "assistant",
        "timestamp": ts or _ts(),
        "sessionId": "s",
        "message": {
            "model": "m", "id": msg_id, "type": "message", "role": "assistant",
            "content": [{"type": block, "text": "x"}],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": 1},
                "service_tier": "standard",
                "cache_creation": {"ephemeral_5m_input_tokens": write},
            },
        },
    })


def _write(path: Path, *rows: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for row in rows:
            fh.write(row + "\n")


# --- the parser ---------------------------------------------------------------


class TestMeter:
    def test_one_message_split_over_rows_is_counted_once(self, tmp_path: Path):
        _write(
            tmp_path / "s.jsonl",
            _assistant("msg_1", inp=2, write=28336, read=18358, out=132, block="thinking"),
            _assistant("msg_1", inp=2, write=28336, read=18358, out=132, block="tool_use"),
            json.dumps({"type": "user", "timestamp": _ts(), "message": {"content": []}}),
            _assistant("msg_2", inp=1, write=759, read=52617, out=347),
        )
        usage = TranscriptMeter([tmp_path], since=T0).read()
        assert usage.input_tokens == 3
        assert usage.cache_creation_tokens == 28336 + 759
        assert usage.cache_read_tokens == 18358 + 52617
        assert usage.output_tokens == 132 + 347
        # Cache reads recorded, not counted.
        assert usage.counted == 3 + 28336 + 759 + 132 + 347
        assert usage.complete

    def test_sub_agents_are_counted(self, tmp_path: Path):
        _write(tmp_path / "s.jsonl", _assistant("m1", inp=10, out=5))
        _write(
            tmp_path / "s" / "subagents" / "agent-a.jsonl",
            _assistant("m2", inp=100, out=50),
        )
        assert TranscriptMeter([tmp_path], since=T0).read().counted == 165

    def test_rows_before_the_spawn_are_not_charged_again(self, tmp_path: Path):
        # A resumed session's transcript repeats the earlier conversation.
        _write(
            tmp_path / "s.jsonl",
            _assistant("old", inp=1_000_000, ts=_ts(-3600)),
            _assistant("new", inp=7),
        )
        assert TranscriptMeter([tmp_path], since=T0).read().counted == 7

    def test_reads_only_what_is_new_and_waits_for_a_whole_row(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        meter = TranscriptMeter([tmp_path], since=T0)
        _write(path, _assistant("m1", inp=10))
        assert meter.read().counted == 10
        row = _assistant("m2", inp=5)
        with open(path, "a") as fh:
            fh.write(row[:20])  # half a row, still being written
        usage = meter.read()
        assert usage.counted == 10 and usage.complete
        with open(path, "a") as fh:
            fh.write(row[20:] + "\n")
        assert meter.read().counted == 15

    def test_a_row_without_usage_is_unknown_not_zero(self, tmp_path: Path):
        bad = json.loads(_assistant("m2"))
        del bad["message"]["usage"]
        _write(tmp_path / "s.jsonl", _assistant("m1", inp=10), json.dumps(bad))
        usage = TranscriptMeter([tmp_path], since=T0).read()
        assert usage.counted == 10  # a floor
        assert usage.unreadable == 1
        assert not usage.complete

    def test_a_count_that_is_not_a_number_is_unreadable(self, tmp_path: Path):
        bad = json.loads(_assistant("m1"))
        bad["message"]["usage"]["output_tokens"] = "12"
        _write(tmp_path / "s.jsonl", json.dumps(bad), "{not json")
        usage = TranscriptMeter([tmp_path], since=T0).read()
        assert usage.unreadable == 2
        assert usage.counted == 0 and not usage.complete


def test_cost_from_the_result_event():
    out = "\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"result","subtype":"success","total_cost_usd":0.4213,"session_id":"x"}',
    ])
    assert parse_result_cost(out) == pytest.approx(0.4213)
    assert parse_result_cost('{"type":"result","session_id":"x"}') is None
    assert parse_result_cost('{"type":"result","total_cost_usd":true}') is None
    assert parse_result_cost("") is None


def test_label_names():
    assert label_names({"labels": [{"name": "bug"}, {"name": "size:S"}, {}]}) == [
        "bug", "size:S",
    ]
    assert label_names({"labels": None}) == []
    assert label_names(None) == []


# --- config and limit selection ----------------------------------------------


class TestConfig:
    def test_no_limit_by_default(self):
        budget = AgentConfig().budget
        assert budget.soft_tokens is None and budget.hard_tokens is None
        assert not resolve_limits(budget, ["size:L"]).any
        assert not resolve_limits(None).any

    def test_hard_below_soft_is_refused(self):
        with pytest.raises(ValidationError, match="below soft_tokens"):
            BudgetConfig(soft_tokens=10, hard_tokens=5)

    def test_a_rule_that_inherits_into_hard_below_soft_is_refused(self):
        with pytest.raises(ValidationError, match=r"rules\[0\].*below soft_tokens"):
            BudgetConfig(
                soft_tokens=100, hard_tokens=200,
                rules=[{"labels": ["size:S"], "hard_tokens": 50}],
            )

    def test_typos_and_bad_values_are_refused(self):
        with pytest.raises(ValidationError):
            BudgetConfig(hard_token=5)
        with pytest.raises(ValidationError):
            BudgetConfig(rules=[{"labels": [], "hard_tokens": 5}])
        with pytest.raises(ValidationError):
            BudgetConfig(rules=[{"labels": [" "], "hard_tokens": 5}])
        with pytest.raises(ValidationError):
            BudgetConfig(hard_tokens=0)

    def test_from_yaml_shape(self):
        agent = AgentConfig.model_validate({
            "budget": {
                "soft_tokens": 1_000_000, "hard_tokens": 3_000_000,
                "rules": [{"labels": ["size:L", "size:XL"], "hard_tokens": 8_000_000}],
            },
        })
        assert agent.budget.rules[0].labels == ["size:L", "size:XL"]


class TestLimitSelection:
    CONFIG = BudgetConfig(
        soft_tokens=100, hard_tokens=200,
        rules=[
            {"labels": ["size:S"], "soft_tokens": 10, "hard_tokens": 20},
            {"labels": ["size:L", "epic"], "hard_tokens": 900},
            {"labels": ["no-limit"], "soft_tokens": None, "hard_tokens": None},
            {"labels": ["size:s"], "hard_tokens": 1_000},  # never reached
        ],
    )

    def test_top_level_without_a_matching_label(self):
        assert resolve_limits(self.CONFIG, ["bug"]) == BudgetLimits(100, 200)

    def test_size_label_rule(self):
        limits = resolve_limits(self.CONFIG, ["bug", "size:S"])
        assert (limits.soft, limits.hard) == (10, 20)
        assert limits.source == "agent.budget.rules[0]"

    def test_first_match_wins_and_case_does_not_matter(self):
        limits = resolve_limits(self.CONFIG, ["SIZE:S", "epic"])
        assert (limits.soft, limits.hard) == (10, 20)

    def test_a_rule_inherits_what_it_leaves_out(self):
        limits = resolve_limits(self.CONFIG, ["Epic"])
        assert (limits.soft, limits.hard) == (100, 900)

    def test_explicit_null_lifts_the_limit(self):
        assert not resolve_limits(self.CONFIG, ["no-limit"]).any


# --- the watch ----------------------------------------------------------------


def _watch(tmp_path: Path, limits: BudgetLimits, *, before: RunUsage | None = None):
    on_usage, on_soft = AsyncMock(), AsyncMock()
    scope = BudgetScope(
        before=before or RunUsage(), on_usage=on_usage, on_soft=on_soft,
    )
    watch = BudgetWatch(
        TranscriptMeter([tmp_path], since=T0), limits=limits, scope=scope,
        ident={"session_id": "s"}, poll_seconds=0.01,
    )
    return watch, on_usage, on_soft


class TestWatch:
    @pytest.mark.asyncio
    async def test_soft_limit_warns_once(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        watch, on_usage, on_soft = _watch(tmp_path, BudgetLimits(soft=100, hard=None))
        _write(path, _assistant("m1", inp=50))
        assert await watch.check() is None
        assert on_soft.await_count == 0
        _write(path, _assistant("m2", inp=60))
        assert await watch.check() is None
        _write(path, _assistant("m3", inp=60))
        assert await watch.check() is None
        assert await watch.check() is None
        assert on_soft.await_count == 1
        total = on_soft.await_args.args[0]
        assert total.counted == 110
        # Usage reported each time it changed, not on every poll.
        assert on_usage.await_count == 3

    @pytest.mark.asyncio
    async def test_no_second_warning_when_an_earlier_spawn_already_warned(
        self, tmp_path: Path
    ):
        watch, _, on_soft = _watch(
            tmp_path, BudgetLimits(soft=100), before=RunUsage(input_tokens=150),
        )
        _write(tmp_path / "s.jsonl", _assistant("m1", inp=5))
        await watch.check()
        assert on_soft.await_count == 0

    @pytest.mark.asyncio
    async def test_hard_limit_counts_earlier_spawns_of_the_session(self, tmp_path: Path):
        watch, _, _ = _watch(
            tmp_path, BudgetLimits(hard=1_000), before=RunUsage(input_tokens=990),
        )
        _write(tmp_path / "s.jsonl", _assistant("m1", inp=4, out=6))
        reason = await asyncio.wait_for(watch.run(), timeout=5)
        assert reason == "budget: used 1,000 tokens, hard limit 1,000"
        assert is_budget_reason(reason)

    @pytest.mark.asyncio
    async def test_cache_reads_do_not_reach_the_hard_limit(self, tmp_path: Path):
        watch, _, _ = _watch(tmp_path, BudgetLimits(hard=1_000))
        _write(tmp_path / "s.jsonl", _assistant("m1", inp=1, read=50_000))
        assert await watch.check() is None

    @pytest.mark.asyncio
    async def test_unreadable_usage_neither_stops_nor_reads_as_zero(
        self, tmp_path: Path
    ):
        bad = json.loads(_assistant("m1", inp=5_000))
        bad["message"]["usage"] = "lots"
        watch, on_usage, _ = _watch(tmp_path, BudgetLimits(soft=1, hard=1))
        _write(tmp_path / "s.jsonl", json.dumps(bad))
        assert await watch.check() is None
        total = on_usage.await_args.args[0]
        assert total.unreadable == 1 and not total.complete

    @pytest.mark.asyncio
    async def test_a_failing_hook_never_stops_the_run(self, tmp_path: Path):
        watch, on_usage, _ = _watch(tmp_path, BudgetLimits(hard=10))
        on_usage.side_effect = RuntimeError("db gone")
        _write(tmp_path / "s.jsonl", _assistant("m1", inp=5))
        assert await watch.check() is None


# --- the session row ----------------------------------------------------------


def _session_row(db: StateDB, sid: str) -> None:
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, worktree_path, status,
           started_at, issue_number) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, "dev", "o/r", "/w", "running", 1, 7),
    )
    db.commit()


class TestSessionScope:
    @pytest.mark.asyncio
    async def test_usage_is_kept_and_the_next_spawn_starts_from_it(self, tmp_path: Path):
        db = StateDB(tmp_path / "state.db")
        _session_row(db, "s1")
        assert session_scope(db, "s1").before == RunUsage()
        first = RunUsage(input_tokens=10, output_tokens=5, cache_read_tokens=99,
                         cost_usd=0.5)
        record_usage(db, "s1", first, BudgetLimits(soft=100, hard=200))
        row = db.get_session_row("s1")
        assert row["usage_tokens"] == 15
        detail = json.loads(row["usage_detail"])
        assert detail["cache_read_input_tokens"] == 99
        assert detail["hard_tokens"] == 200
        assert session_scope(db, "s1").before == first

    @pytest.mark.asyncio
    async def test_unreadable_detail_starts_from_the_floor_as_unknown(
        self, tmp_path: Path
    ):
        db = StateDB(tmp_path / "state.db")
        _session_row(db, "s1")
        db.execute(
            "UPDATE sessions SET usage_tokens = 40, usage_detail = '{' WHERE id = 's1'"
        )
        db.commit()
        before = session_scope(db, "s1").before
        assert before.counted == 40 and not before.complete

    @pytest.mark.asyncio
    async def test_soft_warning_goes_to_chat(self, tmp_path: Path):
        db = StateDB(tmp_path / "state.db")
        _session_row(db, "s1")
        transport = AsyncMock()
        scope = session_scope(db, "s1", transport=transport, repo="o/r", issue_number=7)
        await scope.on_soft(RunUsage(input_tokens=1_500), BudgetLimits(1_000, 2_000))
        text = transport.send.await_args.args[0]
        assert "#7 (o/r)" in text and "1,500" in text and "2,000" in text


# --- through the real spawn path ----------------------------------------------


class TestDispatcherStopsAnOverBudgetRun:
    def _setup(self, tmp_path: Path, budget: BudgetConfig | None, script: str = ""):
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        binary = tmp_path / "fake-claude"
        binary.write_text("#!/bin/sh\n" + (script or "exec sleep 60\n"))
        binary.chmod(0o755)
        config = tmp_path / "config"
        worktree = tmp_path / "wt"
        worktree.mkdir()
        dispatcher = ClaudeDispatcher(
            usage_store=None, claude_binary=str(binary), default_timeout=30,
            extra_env={"CLAUDE_CONFIG_DIR": str(config)},
        )
        dispatcher.stuck_policy = None
        dispatcher.budget = budget
        dispatcher.budget_poll_seconds = 0.05
        folder = dispatcher._transcript_dirs(
            worktree, {"CLAUDE_CONFIG_DIR": str(config)}
        )[0]
        folder.mkdir(parents=True)
        return dispatcher, worktree, folder

    async def _spawn(self, dispatcher, worktree, scope=None):
        return await dispatcher.spawn_session(
            github_access=False, session_id="dev-x-1-abc", prompt="p",
            working_dir=worktree, state_file=worktree / "state.json",
            budget_scope=scope,
        )

    @pytest.mark.asyncio
    async def test_over_the_hard_limit_is_stopped_with_the_reason(self, tmp_path: Path):
        dispatcher, worktree, folder = self._setup(
            tmp_path, BudgetConfig(soft_tokens=500, hard_tokens=1_000),
        )

        async def write_later():
            await asyncio.sleep(0.2)
            _write(folder / "s.jsonl", _assistant("m1", inp=600, out=500))

        writer = asyncio.create_task(write_later())
        started = time.monotonic()
        result = await self._spawn(dispatcher, worktree)
        await writer
        assert time.monotonic() - started < 15
        assert result.stuck_reason == "budget: used 1,100 tokens, hard limit 1,000"
        assert "Session stopped: budget: used 1,100" in result.stderr
        assert result.state is None and not result.timed_out
        assert result.usage is not None and result.usage.counted == 1_100

    @pytest.mark.asyncio
    async def test_a_label_rule_applies_to_the_spawn(self, tmp_path: Path):
        dispatcher, worktree, folder = self._setup(
            tmp_path,
            BudgetConfig(rules=[{"labels": ["size:S"], "hard_tokens": 100}]),
        )
        _write(folder / "s.jsonl", _assistant("m1", inp=150, ts=_ts(5)))
        result = await self._spawn(
            dispatcher, worktree, BudgetScope(labels=["size:S"]),
        )
        assert is_budget_reason(result.stuck_reason)

    @pytest.mark.asyncio
    async def test_no_limit_by_default_and_usage_is_still_recorded(self, tmp_path: Path):
        dispatcher, worktree, folder = self._setup(
            tmp_path, None,
            script=(
                "sleep 0.3\n"
                "echo '{\"type\":\"result\",\"total_cost_usd\":1.25,"
                "\"session_id\":\"x\"}'\n"
            ),
        )
        _write(folder / "s.jsonl", _assistant("m1", inp=10_000_000, ts=_ts(5)))
        seen = AsyncMock()
        result = await self._spawn(dispatcher, worktree, BudgetScope(on_usage=seen))
        assert result.stuck_reason is None
        assert result.usage.counted == 10_000_000
        assert result.usage.cost_usd == pytest.approx(1.25)
        assert seen.await_args.args[0].cost_usd == pytest.approx(1.25)

    @pytest.mark.asyncio
    async def test_unreadable_usage_does_not_stop_the_run(self, tmp_path: Path):
        dispatcher, worktree, folder = self._setup(
            tmp_path, BudgetConfig(hard_tokens=1),
        )
        dispatcher.default_timeout = 1
        bad = json.loads(_assistant("m1", inp=5_000, ts=_ts(5)))
        bad["message"]["usage"] = None
        _write(folder / "s.jsonl", json.dumps(bad))
        result = await self._spawn(dispatcher, worktree)
        assert result.timed_out
        assert result.stuck_reason is None


# --- what the pipelines and chat make of it -----------------------------------


def test_dev_reports_a_budget_stop_as_stopped_not_stuck():
    from unittest.mock import MagicMock

    from ctrlrelay.core.dispatcher import SessionResult
    from ctrlrelay.pipelines.dev import DevPipeline

    pipeline = DevPipeline(
        dispatcher=MagicMock(), github=MagicMock(), worktree=MagicMock(),
        dashboard=None, state_db=MagicMock(), transport=None,
    )
    reason = budget_reason(1_100, 1_000)
    out = pipeline._session_to_result(
        SessionResult(session_id="s", exit_code=-9, state=None, stuck_reason=reason)
    )
    assert not out.success
    assert out.stuck_reason == reason
    assert out.summary == f"Stopped: {reason}"


@pytest.mark.asyncio
async def test_chat_says_over_budget():
    from ctrlrelay.cli import announce_issue_outcome
    from ctrlrelay.pipelines.base import PipelineResult

    transport = AsyncMock()
    reason = budget_reason(1_100, 1_000)
    sent = await announce_issue_outcome(
        transport, repo="o/r", issue_number=7, is_task=False,
        result=PipelineResult(
            success=False, session_id="s", summary=f"Stopped: {reason}",
            stuck_reason=reason,
        ),
    )
    assert sent == [f"💸 Over budget on #7, stopped: {reason}"]


@pytest.mark.asyncio
async def test_dev_spawn_hands_the_issue_labels_and_session_to_the_budget(
    tmp_path: Path,
):
    from unittest.mock import MagicMock

    from ctrlrelay.core.dispatcher import SessionResult
    from ctrlrelay.pipelines.base import PipelineContext
    from ctrlrelay.pipelines.dev import DevPipeline

    db = StateDB(tmp_path / "state.db")
    _session_row(db, "s1")
    record_usage(db, "s1", RunUsage(input_tokens=42), BudgetLimits())
    dispatcher = MagicMock()
    dispatcher.spawn_session = AsyncMock(
        return_value=SessionResult(session_id="s1", exit_code=0, state=None)
    )
    pipeline = DevPipeline(
        dispatcher=dispatcher, github=MagicMock(), worktree=MagicMock(),
        dashboard=None, state_db=db, transport=None,
    )
    ctx = PipelineContext(
        session_id="s1", repo="o/r", worktree_path=tmp_path,
        context_path=tmp_path / "c", state_file=tmp_path / "s.json",
        issue_number=7, extra={"issue_labels": ["size:S"]},
    )
    await pipeline._spawn(ctx, "p", resume=False)
    scope = dispatcher.spawn_session.await_args.kwargs["budget_scope"]
    assert list(scope.labels) == ["size:S"]
    assert scope.before.counted == 42


def test_the_outcome_event_names_a_budget_stop(tmp_path: Path):
    from ctrlrelay.core.events import emit_outcome

    db = StateDB(tmp_path / "state.db")
    reason = budget_reason(1_100, 1_000)
    emit_outcome(db, lambda: {
        "status": "failed", "pipeline": "dev", "repo": "o/r",
        "issue_number": 7, "session_id": "s1", "stuck_reason": reason,
    })
    detail = db.list_events()[0]["detail"]
    assert detail["budget"] == reason
    assert "stuck" not in detail
