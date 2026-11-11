"""Usage reading for generic agent plugins (#387).

The JSONL fixtures are real output, captured on 2026-11-10:

- Codex CLI 0.166.0, ``codex exec --json`` then ``codex exec resume --json``
  on the same thread. Lines are verbatim.
- Copilot CLI 1.0.109, ``copilot -p ... --output-format json`` then
  ``--resume`` on the same session. The ``session.usage_checkpoint`` lines
  are trimmed to the keys read here (the real ones also carry model cache
  state and per-model metrics); every value kept is as printed.

Both are running totals for the native session: the resumed run reports
the first run's tokens again, plus its own.
"""

import asyncio
import json
from pathlib import Path

import pytest

from ctrlrelay.core.budget import BudgetScope, RunUsage, is_budget_reason
from ctrlrelay.core.config import BudgetConfig, GenericCliConfig, GenericUsageConfig

CODEX_FRESH = "\n".join([
    '{"type":"thread.started","thread_id":"01a1c8bd-5305-7030-9970-5e39f8b5be48"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":15397,"cached_input_tokens":12160,'
    '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}',
])
CODEX_RESUMED = "\n".join([
    '{"type":"thread.started","thread_id":"01a1c8bd-5305-7030-9970-5e39f8b5be48"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"AGAIN"}}',
    '{"type":"turn.completed","usage":{"input_tokens":30812,"cached_input_tokens":27392,'
    '"cache_write_input_tokens":0,"output_tokens":11,"reasoning_output_tokens":0}}',
])
CODEX_THREAD = "01a1c8bd-5305-7030-9970-5e39f8b5be48"


def _copilot_checkpoint(premium: int, inp: int, read: int, write: int, out: int) -> str:
    return json.dumps({
        "type": "session.usage_checkpoint",
        "data": {
            "totalNanoAiu": 118876000,
            "totalPremiumRequests": premium,
            "accountingSnapshot": {
                "totalPremiumRequestCost": premium,
                "tokenDetails": {
                    "input": {"tokenCount": inp},
                    "cache_read": {"tokenCount": read},
                    "cache_write": {"tokenCount": write},
                    "output": {"tokenCount": out},
                },
            },
        },
        "id": "00fad2ce-2327-4dce-ba43-82cf543b81d3",
        "timestamp": "2026-11-11T03:53:44.048Z",
    }, separators=(",", ":"))


COPILOT_FRESH = "\n".join([
    '{"type":"assistant.message","data":{"content":"OK"},'
    '"id":"0eddbbb8-b1f6-45bd-a578-7ab4fa608fd1"}',
    _copilot_checkpoint(1, 5197, 7168, 0, 5),
    '{"type":"result","timestamp":"2026-11-11T03:53:44.053Z",'
    '"sessionId":"2a00a77c-51f5-490a-a5d4-78e54368b685","exitCode":0,'
    '"usage":{"premiumRequests":1,"totalApiDurationMs":1964,"sessionDurationMs":7147}}',
])
COPILOT_RESUMED = "\n".join([
    _copilot_checkpoint(2, 5200, 16082, 4498, 22),
    '{"type":"result","timestamp":"2026-11-11T03:54:20.000Z",'
    '"sessionId":"2a00a77c-51f5-490a-a5d4-78e54368b685","exitCode":0,'
    '"usage":{"premiumRequests":2,"totalApiDurationMs":3965,"sessionDurationMs":29608}}',
])
COPILOT_SESSION = "2a00a77c-51f5-490a-a5d4-78e54368b685"

# The tested usage mappings for Codex and Copilot, checked against the real
# output above. A preset that measures either CLI should copy these values.
CODEX_USAGE = {
    "event_type": "turn.completed",
    "reading": "last",
    "covers": "native_session",
    "input_includes_cache_read": True,
    "fields": {
        "input_tokens": "usage.input_tokens",
        "cache_read_tokens": "usage.cached_input_tokens",
        "cache_creation_tokens": "usage.cache_write_input_tokens",
        "output_tokens": "usage.output_tokens",
    },
}
_TD = "data.accountingSnapshot.tokenDetails"
COPILOT_USAGE = {
    "event_type": "session.usage_checkpoint",
    "reading": "last",
    "covers": "native_session",
    "fields": {
        "input_tokens": f"{_TD}.input.tokenCount",
        "cache_read_tokens": f"{_TD}.cache_read.tokenCount",
        "cache_creation_tokens": f"{_TD}.cache_write.tokenCount",
        "output_tokens": f"{_TD}.output.tokenCount",
    },
}


def _spec(**kw) -> GenericUsageConfig:
    return GenericUsageConfig(**kw)


# --- the manifest -------------------------------------------------------------


class TestUsageConfig:
    def test_both_preset_mappings_load(self) -> None:
        for usage in (CODEX_USAGE, COPILOT_USAGE):
            GenericCliConfig(binary="x", usage=usage)

    def test_input_and_output_must_both_be_mapped(self) -> None:
        # Either left out would report a total that omits it as exact.
        with pytest.raises(ValueError, match="must map input_tokens"):
            _spec(event_type="e", fields={"output_tokens": "o"})
        with pytest.raises(ValueError, match="must map output_tokens"):
            _spec(event_type="e", fields={"input_tokens": "i"})
        _spec(event_type="e", fields={"input_tokens": "i", "output_tokens": "o"})

    def test_an_unknown_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown usage field 'reasoning'"):
            _spec(event_type="e", fields={"reasoning": "a.b"})

    def test_no_fields_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must map input_tokens and output_tokens"):
            _spec(event_type="e", fields={})

    @pytest.mark.parametrize("path", ["a..b", ".a", "a.", "a. b", "a.b ", " a", "a\tb"])
    def test_a_broken_path_is_refused(self, path: str) -> None:
        with pytest.raises(ValueError, match="not a dotted path"):
            _spec(event_type="e", fields={"input_tokens": "i", "output_tokens": path})

    @pytest.mark.parametrize("path", ["", "a..b", "type ", "data. type"])
    def test_a_broken_event_type_path_is_refused(self, path: str) -> None:
        with pytest.raises(ValueError, match="not a dotted path"):
            _spec(
                event_type="e", event_type_path=path,
                fields={"input_tokens": "i", "output_tokens": "o"},
            )

    @pytest.mark.parametrize("fields,etp", [
        ({"input_tokens": "u.i", "output_tokens": "u.i"}, "type"),
        ({"input_tokens": "u", "output_tokens": "u.o"}, "type"),
        ({"input_tokens": "u.i", "output_tokens": "type"}, "type"),
        ({"input_tokens": "msg.i", "output_tokens": "o"}, "msg"),
    ])
    def test_overlapping_paths_are_refused(self, fields, etp) -> None:
        with pytest.raises(ValueError, match="overlap"):
            _spec(event_type="e", event_type_path=etp, fields=fields)

    @pytest.mark.parametrize("event_type", [" turn.completed", "turn.completed ", "  "])
    def test_an_event_type_with_surrounding_whitespace_is_refused(self, event_type) -> None:
        with pytest.raises(ValueError, match="surrounding whitespace"):
            _spec(event_type=event_type, fields={"input_tokens": "i", "output_tokens": "o"})

    def test_a_good_nested_path_is_accepted(self) -> None:
        # The control for the two above.
        _spec(
            event_type="e", event_type_path="msg.kind",
            fields={"input_tokens": "a.b_c.d-1", "output_tokens": "o"},
        )

    def test_an_extra_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="units"):
            _spec(
                event_type="e", fields={"input_tokens": "i", "output_tokens": "o"},
                units="requests",
            )

    def test_cache_read_subtraction_needs_both_counts(self) -> None:
        with pytest.raises(ValueError, match="input_includes_cache_read"):
            _spec(
                event_type="e", input_includes_cache_read=True,
                fields={"input_tokens": "i", "output_tokens": "o"},
            )

    def test_a_session_running_total_must_be_read_last(self) -> None:
        with pytest.raises(ValueError, match="set reading: 'last'"):
            _spec(
                event_type="e", covers="native_session",
                fields={"input_tokens": "i", "output_tokens": "o"},
            )
        with pytest.raises(ValueError, match="set reading: 'last'"):
            _spec(
                event_type="e", covers="native_session", reading="sum",
                fields={"input_tokens": "i", "output_tokens": "o"},
            )


# --- reading one output -------------------------------------------------------


class TestReadUsageEvents:
    def test_codex_input_has_its_cache_reads_taken_out(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        got = read_usage_events(CODEX_FRESH, _spec(**CODEX_USAGE))
        assert got == RunUsage(
            input_tokens=15397 - 12160, cache_read_tokens=12160,
            cache_creation_tokens=0, output_tokens=5,
        )
        assert got.complete

    def test_copilot_tokens_come_from_the_checkpoint(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        got = read_usage_events(COPILOT_FRESH, _spec(**COPILOT_USAGE))
        assert got == RunUsage(
            input_tokens=5197, cache_read_tokens=7168,
            cache_creation_tokens=0, output_tokens=5,
        )

    def test_no_usage_line_is_unknown_not_zero(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        got = read_usage_events('{"type":"turn.started"}\nnot json\n', _spec(**CODEX_USAGE))
        assert got.unreadable == 1 and not got.complete
        assert got.counted == 0

    def test_a_usage_line_missing_a_count_is_unknown(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        broken = CODEX_FRESH.replace('"output_tokens":5,', "")
        got = read_usage_events(broken, _spec(**CODEX_USAGE))
        # The line adds nothing: its other counts are not charged either.
        assert not got.complete and got == RunUsage(unreadable=1)

    @pytest.mark.parametrize("bad", ["-1", "true", "5.0", str(10**15), '"7"', "null"])
    @pytest.mark.parametrize("key,real", [
        ("output_tokens", "5"),
        ("input_tokens", "15397"),
        ("cached_input_tokens", "12160"),
        ("cache_write_input_tokens", "0"),
    ])
    def test_a_count_that_is_not_a_plausible_token_count_is_unknown(
        self, bad: str, key: str, real: str
    ) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        old = f'"{key}":{real}'
        assert old in CODEX_FRESH
        line = CODEX_FRESH.replace(old, f'"{key}":{bad}')
        got = read_usage_events(line, _spec(**CODEX_USAGE))
        assert got == RunUsage(unreadable=1)

    def test_a_sum_past_the_ceiling_is_unknown(self) -> None:
        from ctrlrelay.core.generic_cli import MAX_TOKEN_COUNT, read_usage_events

        line = f'{{"t":"u","i":0,"n":{MAX_TOKEN_COUNT}}}'
        spec = _spec(
            event_type="u", event_type_path="t", reading="sum",
            fields={"input_tokens": "i", "output_tokens": "n"},
        )
        assert read_usage_events(line, spec).output_tokens == MAX_TOKEN_COUNT
        assert read_usage_events(line + "\n" + line, spec) == RunUsage(unreadable=1)

    def test_the_largest_plausible_count_is_kept(self) -> None:
        # The control: the ceiling itself is a count.
        from ctrlrelay.core.generic_cli import MAX_TOKEN_COUNT, read_usage_events

        line = CODEX_FRESH.replace('"output_tokens":5', f'"output_tokens":{MAX_TOKEN_COUNT}')
        got = read_usage_events(line, _spec(**CODEX_USAGE))
        assert got.complete and got.output_tokens == MAX_TOKEN_COUNT

    def test_cache_reads_above_input_are_unknown(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        odd = CODEX_FRESH.replace('"cached_input_tokens":12160', '"cached_input_tokens":99999')
        got = read_usage_events(odd, _spec(**CODEX_USAGE))
        assert not got.complete and got.counted == 0

    def test_sum_adds_each_line_and_last_takes_the_last(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        lines = (
            '{"t":"u","i":0,"n":3}\n{"t":"u","i":0,"n":4}\n'
            '{"t":"other","i":0,"n":100}\n'
        )
        common = {
            "event_type": "u", "event_type_path": "t",
            "fields": {"input_tokens": "i", "output_tokens": "n"},
        }
        assert read_usage_events(lines, _spec(**common, reading="sum")).output_tokens == 7
        assert read_usage_events(lines, _spec(**common, reading="last")).output_tokens == 4

    def test_a_bad_line_in_a_sum_is_counted_unreadable_and_the_rest_kept(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        lines = '{"t":"u","i":0,"n":3}\n{"t":"u","i":0,"n":"many"}\n'
        got = read_usage_events(lines, _spec(
            event_type="u", event_type_path="t",
            fields={"input_tokens": "i", "output_tokens": "n"},
        ))
        assert got.output_tokens == 3 and got.unreadable == 1

    def test_a_cut_off_usage_line_after_a_good_one_is_unknown(self) -> None:
        # A valid running total followed by a final one cut off mid-line:
        # the earlier total is not the run's last, so it is only a floor.
        from ctrlrelay.core.generic_cli import read_usage_events

        cut = CODEX_RESUMED.splitlines()[-1][:60]
        got = read_usage_events(CODEX_FRESH + "\n" + cut, _spec(**CODEX_USAGE))
        assert not got.complete
        assert got.output_tokens == 5

    def test_a_line_cut_off_inside_its_type_is_unknown(self) -> None:
        # The type value is cut short, so the line cannot be matched to the
        # event, yet it could have been the last usage line.
        from ctrlrelay.core.generic_cli import read_usage_events

        got = read_usage_events(
            CODEX_FRESH + '\n{"type":"turn.comp', _spec(**CODEX_USAGE),
        )
        assert not got.complete and got.output_tokens == 5

    def test_a_line_cut_off_before_its_type_key_is_unknown(self) -> None:
        # Nothing of the type was written yet: an event with its usage
        # object first would be cut here.
        from ctrlrelay.core.generic_cli import read_usage_events

        got = read_usage_events(
            CODEX_FRESH + '\n{"usage":{"input_to', _spec(**CODEX_USAGE),
        )
        assert not got.complete and got.output_tokens == 5

    def test_prose_lines_do_not_make_it_unknown(self) -> None:
        from ctrlrelay.core.generic_cli import read_usage_events

        got = read_usage_events(
            "Reading prompt from stdin...\n" + CODEX_FRESH + "\ndone\n",
            _spec(**CODEX_USAGE),
        )
        assert got.complete


# --- through the real spawn path ----------------------------------------------


def _fake_cli(tmp_path: Path, name: str, script: str) -> Path:
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\n" + script)
    binary.chmod(0o755)
    return binary


def _printing(tmp_path: Path, name: str, stdout: str, then: str = "") -> Path:
    out = tmp_path / f"{name}.out"
    out.write_text(stdout + "\n")
    return _fake_cli(tmp_path, name, f"cat '{out}'\n{then}")


def _adapter(binary: Path, usage: dict | None, budget: BudgetConfig | None = None, **kw):
    from ctrlrelay.core.generic_cli import GenericCliDispatcher

    adapter = GenericCliDispatcher(
        agent_type="codex",
        binary=str(binary),
        args=("exec", "--json", "{prompt}"),
        resume_args=("exec", "resume", "--json", "{resume_session_id}", "{prompt}"),
        session_id_path=kw.pop("session_id_path", "thread_id"),
        usage=GenericUsageConfig(**usage) if usage is not None else None,
        usage_store=None,
        default_timeout=30,
        **kw,
    )
    adapter.stuck_policy = None
    adapter.budget = budget
    adapter.budget_poll_seconds = 0.05
    return adapter


async def _spawn(
    adapter, tmp_path: Path, *, scope=None, resume=None, repo=None, timeout=None,
    session_id="dev-x-1-abc",
):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return await adapter.spawn_session(
        github_access=False, session_id=session_id, prompt="p",
        working_dir=worktree, state_file=worktree / "state.json",
        budget_scope=scope, resume_session_id=resume, repo=repo, timeout=timeout,
    )


class TestSpawnMeasuresTheRun:
    def test_capability_follows_the_mapping(self, tmp_path: Path) -> None:
        binary = _printing(tmp_path, "cli", "")
        assert _adapter(binary, CODEX_USAGE).capabilities.usage is True
        assert _adapter(binary, None).capabilities.usage is False

    @pytest.mark.asyncio
    async def test_a_fresh_run_is_measured(self, tmp_path: Path) -> None:
        adapter = _adapter(_printing(tmp_path, "codex", CODEX_FRESH), CODEX_USAGE)
        result = await _spawn(adapter, tmp_path)
        assert result.agent_session_id == CODEX_THREAD
        assert result.usage == RunUsage(
            input_tokens=3237, cache_read_tokens=12160, output_tokens=5,
        )

    @pytest.mark.asyncio
    async def test_without_a_mapping_nothing_is_measured(self, tmp_path: Path) -> None:
        adapter = _adapter(
            _printing(tmp_path, "codex", CODEX_FRESH), None,
            BudgetConfig(hard_tokens=1),
        )
        result = await _spawn(adapter, tmp_path)
        assert result.usage is None and result.stuck_reason is None

    @pytest.mark.asyncio
    async def test_a_resumed_round_is_charged_only_what_it_added(
        self, tmp_path: Path
    ) -> None:
        # One adapter, two rounds of one native session, the session scope
        # carrying the first round's total into the second as dev.py does.
        first = _printing(tmp_path, "codex1", CODEX_FRESH)
        adapter = _adapter(first, CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)

        adapter.binary = str(_printing(tmp_path, "codex2", CODEX_RESUMED))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        # The session total is the CLI's own running total: nothing twice.
        assert r2.usage == RunUsage(
            input_tokens=30812 - 27392, cache_read_tokens=27392, output_tokens=11,
        )
        assert r2.usage.complete

    @pytest.mark.asyncio
    async def test_copilot_resume_is_charged_only_what_it_added(
        self, tmp_path: Path
    ) -> None:
        kw = {"session_id_path": "sessionId"}
        adapter = _adapter(_printing(tmp_path, "cp1", COPILOT_FRESH), COPILOT_USAGE, **kw)
        r1 = await _spawn(adapter, tmp_path)
        assert r1.agent_session_id == COPILOT_SESSION
        adapter.binary = str(_printing(tmp_path, "cp2", COPILOT_RESUMED))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=COPILOT_SESSION,
        )
        assert r2.usage == RunUsage(
            input_tokens=5200, cache_read_tokens=16082,
            cache_creation_tokens=4498, output_tokens=22,
        )
        assert r2.usage.complete

    @pytest.mark.asyncio
    async def test_a_resume_with_no_remembered_total_charges_nothing(
        self, tmp_path: Path
    ) -> None:
        # The relay restarted between rounds: the first round's running
        # total is gone, and nothing says which part of the CLI's total is
        # new (the native session may even predate this relay session). So
        # nothing is charged and the total shows as unknown, never exact.
        before = RunUsage(input_tokens=3237, cache_read_tokens=12160, output_tokens=5)
        adapter = _adapter(_printing(tmp_path, "codex2", CODEX_RESUMED), CODEX_USAGE)
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=before), resume=CODEX_THREAD,
        )
        assert not r2.usage.complete
        assert r2.usage.counted == before.counted

    @pytest.mark.asyncio
    async def test_a_resume_of_a_session_older_than_the_relay_session(
        self, tmp_path: Path
    ) -> None:
        # An empty, exact `before` and a native total from earlier work:
        # charging the total would bill this session for that work.
        adapter = _adapter(_printing(tmp_path, "codex2", CODEX_RESUMED), CODEX_USAGE)
        r = await _spawn(adapter, tmp_path, resume=CODEX_THREAD)
        assert not r.usage.complete and r.usage.counted == 0

    @pytest.mark.asyncio
    async def test_the_round_after_an_unremembered_resume_is_exact(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(_printing(tmp_path, "codex2", CODEX_RESUMED), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path, resume=CODEX_THREAD)
        more = CODEX_RESUMED.replace('"output_tokens":11', '"output_tokens":20')
        adapter.binary = str(_printing(tmp_path, "codex3", more))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        # Unknown stays unknown for the session, but this round is charged
        # exactly what it added: 9 output, nothing else.
        assert r1.usage == RunUsage(unreadable=1)
        assert r2.usage == RunUsage(output_tokens=9, unreadable=1)

    @pytest.mark.asyncio
    async def test_two_native_sessions_keep_their_own_totals(
        self, tmp_path: Path
    ) -> None:
        # Fresh A, fresh B (another thread), then a resume of A: A's resume
        # is charged against A's last total, not B's, and B is added, not
        # taken as a running total of A.
        other = "01a1c8bd-0000-7030-9970-5e39f8b5be48"
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        ra = await _spawn(adapter, tmp_path)
        # B reports a different total from A's, so a resume of A measured
        # against B's total would give a different number.
        b_out = CODEX_FRESH.replace(CODEX_THREAD, other).replace(
            '"output_tokens":5', '"output_tokens":1'
        )
        adapter.binary = str(_printing(tmp_path, "b1", b_out))
        rb = await _spawn(adapter, tmp_path, scope=BudgetScope(before=ra.usage))
        assert rb.agent_session_id == other
        assert rb.usage.counted == ra.usage.counted + 3238 and rb.usage.complete
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED))
        ra2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=rb.usage), resume=CODEX_THREAD,
        )
        # B's round plus A's whole running total.
        assert ra2.usage.complete
        assert ra2.usage.counted == 3238 + (30812 - 27392) + 11

    @pytest.mark.asyncio
    async def test_a_native_id_reused_in_another_repo_has_its_own_total(
        self, tmp_path: Path
    ) -> None:
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        ra = await _spawn(adapter, tmp_path, repo="o/a")
        # Repo b's run happens to report a much larger total under the
        # same thread id; it must not become repo a's baseline.
        bigger = CODEX_RESUMED.replace('"output_tokens":11', '"output_tokens":9000')
        adapter.binary = str(_printing(tmp_path, "b1", bigger))
        await _spawn(adapter, tmp_path, repo="o/b")
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED))
        ra2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=ra.usage),
            resume=CODEX_THREAD, repo="o/a",
        )
        assert ra2.usage.complete and ra2.usage.output_tokens == 11

    @pytest.mark.asyncio
    async def test_a_total_seen_by_another_relay_session_is_not_its_baseline(
        self, tmp_path: Path
    ) -> None:
        # Relay session one ran the native session; relay session two now
        # resumes it with nothing recorded. Session one's total is not a
        # baseline session two has charged against, so a difference from it
        # cannot be called session two's exact usage: unknown, not exact.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        await _spawn(adapter, tmp_path, session_id="dev-x-1-one")
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED))
        r = await _spawn(
            adapter, tmp_path, session_id="dev-x-1-two", resume=CODEX_THREAD,
        )
        assert not r.usage.complete and r.usage.counted == 0

    @pytest.mark.asyncio
    async def test_a_fresh_round_reporting_a_measured_id_is_charged_the_difference(
        self, tmp_path: Path
    ) -> None:
        # A CLI that carries a session on under the same id when started
        # fresh: charging the whole reading would charge round one again.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED))
        r2 = await _spawn(adapter, tmp_path, scope=BudgetScope(before=r1.usage))
        assert r2.usage.complete
        assert r2.usage.counted == (30812 - 27392) + 11

    @pytest.mark.asyncio
    async def test_an_inexact_fresh_round_under_a_measured_id_adds_up(
        self, tmp_path: Path
    ) -> None:
        # The same, ending badly: the difference as a floor, and the reading
        # becomes the baseline, so the session never passes the CLI's total.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED, "exit 1\n"))
        r2 = await _spawn(adapter, tmp_path, scope=BudgetScope(before=r1.usage))
        assert not r2.usage.complete
        assert r2.usage.counted == (30812 - 27392) + 11
        more = CODEX_RESUMED.replace('"output_tokens":11', '"output_tokens":20')
        adapter.binary = str(_printing(tmp_path, "a3", more))
        r3 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r2.usage), resume=CODEX_THREAD,
        )
        assert r3.usage.counted == (30812 - 27392) + 20

    @pytest.mark.asyncio
    async def test_a_fresh_round_below_the_remembered_total_is_a_new_session(
        self, tmp_path: Path
    ) -> None:
        # Same id, lower total: a new native session that reused the id.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_RESUMED), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_FRESH))
        r2 = await _spawn(adapter, tmp_path, scope=BudgetScope(before=r1.usage))
        assert r2.usage.complete
        assert r2.usage.counted == r1.usage.counted + 3242

    @pytest.mark.asyncio
    async def test_a_running_total_that_went_backwards_charges_nothing(
        self, tmp_path: Path
    ) -> None:
        # Input moves back, output moves forward: neither difference says
        # what this round used.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_RESUMED), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        odd = CODEX_FRESH.replace('"output_tokens":5', '"output_tokens":50')
        adapter.binary = str(_printing(tmp_path, "a2", odd))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert not r2.usage.complete
        assert r2.usage.counted == r1.usage.counted
        # The higher total stays the baseline: a third round reporting a
        # little more than the first is charged only that little.
        more = CODEX_RESUMED.replace('"output_tokens":11', '"output_tokens":15')
        adapter.binary = str(_printing(tmp_path, "a3", more))
        r3 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r2.usage), resume=CODEX_THREAD,
        )
        assert r3.usage.counted == r1.usage.counted + 4

    @pytest.mark.asyncio
    async def test_an_unclean_resumed_round_is_charged_its_difference_as_a_floor(
        self, tmp_path: Path
    ) -> None:
        # Exited non-zero after reporting: its last report is at most what
        # it used, so the difference from the exact baseline is a floor.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED, "exit 3\n"))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert not r2.usage.complete
        assert r2.usage.counted == (30812 - 27392) + 11
        # The next exact round is charged from that report, so the session
        # adds up to the CLI's own total: nothing charged twice.
        more = CODEX_RESUMED.replace('"output_tokens":11', '"output_tokens":20')
        adapter.binary = str(_printing(tmp_path, "a3", more))
        r3 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r2.usage), resume=CODEX_THREAD,
        )
        assert r3.usage.counted == (30812 - 27392) + 20

    @pytest.mark.asyncio
    async def test_an_unreadable_resumed_round_charges_nothing_and_keeps_the_baseline(
        self, tmp_path: Path
    ) -> None:
        # No usage line at all reads as all zeros, below the remembered
        # total, so it takes the moved-backwards path: nothing charged. The
        # next exact round, charged from the first round's total, picks up
        # whatever this one used.
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(tmp_path, "a2", '{"type":"turn.started"}'))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert not r2.usage.complete and r2.usage.counted == r1.usage.counted
        adapter.binary = str(_printing(tmp_path, "a3", CODEX_RESUMED))
        r3 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r2.usage), resume=CODEX_THREAD,
        )
        assert r3.usage.counted == (30812 - 27392) + 11

    @pytest.mark.asyncio
    async def test_an_all_zero_baseline_charges_the_next_round_in_full(
        self, tmp_path: Path
    ) -> None:
        # A fresh round with no usage line: charged nothing, unknown, and its
        # all-zero reading is the native session's start, so the next exact
        # round is charged its whole total, once.
        adapter = _adapter(
            _printing(tmp_path, "a1", CODEX_FRESH.splitlines()[0]), CODEX_USAGE,
        )
        r1 = await _spawn(adapter, tmp_path)
        assert r1.agent_session_id == CODEX_THREAD
        assert r1.usage == RunUsage(unreadable=1)
        adapter.binary = str(_printing(tmp_path, "a2", CODEX_RESUMED))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert r2.usage.counted == (30812 - 27392) + 11

    @pytest.mark.asyncio
    async def test_a_renamed_session_after_a_backwards_total_keeps_the_higher_one(
        self, tmp_path: Path
    ) -> None:
        renamed = "01a1c8bd-2222-7030-9970-5e39f8b5be48"
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_RESUMED), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(
            tmp_path, "a2", CODEX_FRESH.replace(CODEX_THREAD, renamed),
        ))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert r2.usage.counted == r1.usage.counted
        more = CODEX_RESUMED.replace(CODEX_THREAD, renamed).replace(
            '"output_tokens":11', '"output_tokens":15'
        )
        adapter.binary = str(_printing(tmp_path, "a3", more))
        r3 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r2.usage), resume=renamed,
        )
        # Charged from round one's higher total, under the new id.
        assert r3.usage.counted == r1.usage.counted + 4

    @pytest.mark.asyncio
    async def test_a_cli_that_renames_its_session_on_resume_keeps_its_baseline(
        self, tmp_path: Path
    ) -> None:
        # The resumed round reports a new id; the next round resumes that
        # id and must be measured against the total reported under it.
        renamed = "01a1c8bd-1111-7030-9970-5e39f8b5be48"
        adapter = _adapter(_printing(tmp_path, "a1", CODEX_FRESH), CODEX_USAGE)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(
            tmp_path, "a2", CODEX_RESUMED.replace(CODEX_THREAD, renamed),
        ))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert r2.agent_session_id == renamed and r2.usage.complete
        more = CODEX_RESUMED.replace(CODEX_THREAD, renamed).replace(
            '"output_tokens":11', '"output_tokens":20'
        )
        adapter.binary = str(_printing(tmp_path, "a3", more))
        r3 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r2.usage), resume=renamed,
        )
        assert r3.usage.complete
        assert r3.usage.counted == (30812 - 27392) + 20

    @pytest.mark.asyncio
    async def test_a_run_that_exits_non_zero_is_a_floor(self, tmp_path: Path) -> None:
        adapter = _adapter(
            _printing(tmp_path, "codex", CODEX_FRESH, "exit 1\n"), CODEX_USAGE,
        )
        result = await _spawn(adapter, tmp_path)
        assert result.exit_code == 1
        assert not result.usage.complete and result.usage.counted == 3242

    @pytest.mark.asyncio
    async def test_a_timed_out_run_leaves_the_total_unknown(self, tmp_path: Path) -> None:
        seen: list[RunUsage] = []

        async def on_usage(total, limits):
            seen.append(total)

        before = RunUsage(input_tokens=3237, cache_read_tokens=12160, output_tokens=5)
        adapter = _adapter(
            _printing(tmp_path, "codex", CODEX_FRESH, "exec sleep 60\n"), CODEX_USAGE,
        )
        result = await _spawn(
            adapter, tmp_path, timeout=1,
            scope=BudgetScope(before=before, on_usage=on_usage),
        )
        assert result.timed_out
        assert not result.usage.complete and result.usage.counted == before.counted
        assert seen and not seen[-1].complete

    @pytest.mark.asyncio
    async def test_a_timed_out_run_without_a_mapping_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        # A budget is configured, so the exit-reading guard is what keeps
        # the round from being recorded.
        adapter = _adapter(
            _printing(tmp_path, "codex", CODEX_FRESH, "exec sleep 60\n"), None,
            BudgetConfig(hard_tokens=1_000),
        )
        result = await _spawn(adapter, tmp_path, timeout=1)
        assert result.timed_out and result.usage is None

    @pytest.mark.asyncio
    async def test_the_hard_limit_applies_to_the_next_round(
        self, tmp_path: Path, caplog
    ) -> None:
        # Round one passes the limit; it cannot be stopped mid-way, since
        # the reading arrives at exit, and that is logged. The next round is
        # refused before its CLI starts.
        budget = BudgetConfig(rules=[{"labels": ["size:S"], "hard_tokens": 1_000}])
        adapter = _adapter(_printing(tmp_path, "codex1", CODEX_FRESH), CODEX_USAGE, budget)
        with caplog.at_level("INFO"):
            r1 = await _spawn(adapter, tmp_path, scope=BudgetScope(labels=["size:S"]))
        assert r1.usage.counted == 3242
        assert r1.stuck_reason is None
        assert "budget.hard_limit_passed_at_exit" in caplog.text

        marker = tmp_path / "ran"
        adapter.binary = str(_printing(
            tmp_path, "codex2", CODEX_RESUMED, f"touch '{marker}'\n",
        ))
        r2 = await _spawn(
            adapter, tmp_path,
            scope=BudgetScope(labels=["size:S"], before=r1.usage), resume=CODEX_THREAD,
        )
        assert is_budget_reason(r2.stuck_reason)
        assert "hard limit 1,000" in r2.stuck_reason
        assert r2.state is None and r2.usage == r1.usage
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_under_the_limit_the_next_round_runs(self, tmp_path: Path) -> None:
        # The control for the two tests above: the same rounds and the same
        # marker with room, so the marker's absence there means not run.
        marker = tmp_path / "ran"
        budget = BudgetConfig(hard_tokens=1_000_000)
        adapter = _adapter(_printing(tmp_path, "codex1", CODEX_FRESH), CODEX_USAGE, budget)
        r1 = await _spawn(adapter, tmp_path)
        adapter.binary = str(_printing(
            tmp_path, "codex2", CODEX_RESUMED, f"touch '{marker}'\n",
        ))
        r2 = await _spawn(
            adapter, tmp_path, scope=BudgetScope(before=r1.usage), resume=CODEX_THREAD,
        )
        assert r2.stuck_reason is None and r2.exit_code == 0
        assert r2.usage.output_tokens == 11
        assert marker.exists()

    @pytest.mark.asyncio
    async def test_a_cancelled_round_leaves_the_total_unknown(self, tmp_path: Path) -> None:
        seen: list[RunUsage] = []

        async def on_usage(total, limits):
            seen.append(total)

        started = tmp_path / "started"
        adapter = _adapter(
            _fake_cli(tmp_path, "codex", f"touch '{started}'\nexec sleep 60\n"),
            CODEX_USAGE,
        )
        before = RunUsage(input_tokens=3237, output_tokens=5)
        task = asyncio.create_task(_spawn(
            adapter, tmp_path, scope=BudgetScope(before=before, on_usage=on_usage),
        ))
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen and not seen[-1].complete
        assert seen[-1].counted == before.counted

    @pytest.mark.asyncio
    async def test_unreadable_pipes_leave_the_total_unknown(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        seen: list[RunUsage] = []

        async def on_usage(total, limits):
            seen.append(total)

        proc = AsyncMock()
        proc.communicate.side_effect = OSError("pipe went away")
        proc.returncode = 0
        adapter = _adapter(_printing(tmp_path, "codex", CODEX_FRESH), CODEX_USAGE)
        before = RunUsage(input_tokens=3237, output_tokens=5)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(OSError):
                await _spawn(
                    adapter, tmp_path, scope=BudgetScope(before=before, on_usage=on_usage),
                )
        assert seen and not seen[-1].complete

    @pytest.mark.asyncio
    async def test_the_session_row_hook_gets_the_total(self, tmp_path: Path) -> None:
        seen: list[RunUsage] = []

        async def on_usage(total, limits):
            seen.append(total)

        adapter = _adapter(_printing(tmp_path, "codex", CODEX_FRESH), CODEX_USAGE)
        await _spawn(adapter, tmp_path, scope=BudgetScope(on_usage=on_usage))
        assert seen and seen[-1].counted == 3242


def test_the_config_mapping_reaches_the_adapter(tmp_path: Path) -> None:
    """Through the factory a node uses, not by constructing the adapter."""
    from ctrlrelay.core.config import AgentConfig
    from ctrlrelay.core.dispatcher import _make_adapter

    binary = _printing(tmp_path, "codex", "")
    agent = AgentConfig(type="codex", plugins={"codex": {
        "binary": str(binary), "args": ["exec", "--json", "{prompt}"],
        "session_id_path": "thread_id", "usage": CODEX_USAGE,
    }})
    adapter = _make_adapter(agent, outcome_hook=None, usage_store=None)
    assert adapter.usage == GenericUsageConfig(**CODEX_USAGE)
    assert adapter.capabilities.usage is True

