"""Usage readings from the agent CLI's stream-json output (#247).

``fixtures/stream_json_rate_limit.jsonl`` is a real run of the agent CLI
(version 2.1.316) with ``--output-format stream-json --verbose``, trimmed
to the lines that matter and with paths replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctrlrelay.core.usage import (
    UsageReading,
    UsageStore,
    Window,
    parse_agent_session_id,
    parse_rate_limit_event,
    pause_for,
    usage_json,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stream_json_rate_limit.jsonl"
RECORDED = FIXTURE.read_text()
NOW = 1_793_490_000


def _reading(**over) -> UsageReading:
    windows = {
        "five_hour": Window(0.28, NOW + 3600),
        "seven_day": Window(0.35, NOW + 86400),
    }
    windows.update(over.pop("windows", {}))
    return UsageReading(
        status=over.pop("status", "allowed"), windows=windows, read_at=NOW,
    )


class TestParsingARecordedRun:
    def test_reads_both_windows_and_the_status(self) -> None:
        r = parse_rate_limit_event(RECORDED, now=NOW)
        assert r is not None
        assert r.status == "allowed"
        assert r.windows["five_hour"] == Window(0.28, 1793503200)
        assert r.windows["seven_day"] == Window(0.35, 1793955600)
        assert r.read_at == NOW

    def test_reads_the_session_id_from_the_result_line(self) -> None:
        assert parse_agent_session_id(RECORDED) == "3173130c-4a4c-4546-8050-88936d4238d6"

    def test_still_reads_the_session_id_from_the_old_single_json_output(self) -> None:
        assert parse_agent_session_id('{"type":"result","session_id":"abc"}') == "abc"
        assert parse_agent_session_id(json.dumps({"session_id": "abc"}, indent=2)) == "abc"

    def test_a_run_cut_short_still_yields_a_session_id(self) -> None:
        first_two = "\n".join(RECORDED.splitlines()[:2])
        assert parse_agent_session_id(first_two) == "3173130c-4a4c-4546-8050-88936d4238d6"

    def test_the_last_event_wins(self) -> None:
        later = RECORDED.splitlines()[2].replace('"utilization": 0.28', '"utilization": 0.91')
        r = parse_rate_limit_event(RECORDED + later + "\n", now=NOW)
        assert r.windows["five_hour"].used == 0.91


class TestAMissingOrChangedFieldIsUnknownNeverZero:
    @pytest.mark.parametrize("text", [
        "",
        "not json at all",
        '{"type":"result","session_id":"x"}',
        # the field renamed
        RECORDED.replace('"unifiedWindows"', '"windows"'),
        # utilization as a string
        RECORDED.replace('"utilization": 0.28', '"utilization": "0.28"')
                .replace('"utilization": 0.35', '"utilization": "0.35"'),
        # status gone
        RECORDED.replace('"status": "allowed", "resetsAt"', '"resetsAt"'),
        # true is not a number here
        RECORDED.replace('"utilization": 0.28', '"utilization": true')
                .replace('"utilization": 0.35', '"utilization": false'),
    ])
    def test_unparseable_is_none(self, text: str) -> None:
        assert parse_rate_limit_event(text, now=NOW) is None

    def test_unknown_is_published_as_unknown(self) -> None:
        assert usage_json(None) == "unknown"

    def test_one_window_missing_is_unknown_for_that_window_only(self) -> None:
        text = RECORDED.replace('"seven_day"', '"weekly"')
        out = usage_json(parse_rate_limit_event(text, now=NOW))
        assert out["seven_day"] == "unknown"
        assert out["five_hour"]["used"] == 0.28


class TestPause:
    def test_below_the_threshold_and_allowed_does_not_pause(self) -> None:
        assert pause_for(_reading(), threshold=0.9, now=NOW) is None

    def test_at_the_threshold_pauses_until_that_window_resets(self) -> None:
        p = pause_for(
            _reading(windows={"five_hour": Window(0.9, NOW + 600)}),
            threshold=0.9, now=NOW,
        )
        assert p is not None and p.until == NOW + 600
        assert "5-hour" in p.reason

    def test_a_status_that_is_not_allowed_pauses(self) -> None:
        # No reset of its own: an hour, not the 7-day window's reset.
        p = pause_for(_reading(status="rejected"), threshold=0.9, now=NOW)
        assert p is not None and p.until == NOW + 3600

    def test_after_the_reset_the_old_reading_does_not_pause(self) -> None:
        r = _reading(windows={"five_hour": Window(0.99, NOW + 600)})
        assert pause_for(r, threshold=0.9, now=NOW + 600) is None

    def test_both_windows_over_waits_for_the_later_reset(self) -> None:
        r = _reading(windows={
            "five_hour": Window(0.95, NOW + 600),
            "seven_day": Window(0.95, NOW + 86400),
        })
        assert pause_for(r, threshold=0.9, now=NOW).until == NOW + 86400

    def test_unknown_does_not_pause(self) -> None:
        assert pause_for(None, threshold=0.9, now=NOW) is None


class TestStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        store = UsageStore(tmp_path / "usage.json")
        assert store.read() is None
        store.write(_reading())
        assert store.read() == _reading()

    def test_a_corrupt_file_reads_as_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.json"
        path.write_text("{nope")
        assert UsageStore(path).read() is None

    def test_a_write_that_fails_does_not_raise(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("")
        UsageStore(blocker / "usage.json").write(_reading())


class TestGate:
    @pytest.mark.asyncio
    async def test_one_line_when_it_pauses_and_one_when_it_resumes(
        self, tmp_path: Path,
    ) -> None:
        from ctrlrelay.core.usage import UsageGate

        store = UsageStore(tmp_path / "usage.json")
        said: list[str] = []

        async def notify(text: str) -> None:
            said.append(text)

        now = [float(NOW)]
        gate = UsageGate(store, threshold=0.9, notify=notify, clock=lambda: now[0])
        store.write(_reading(windows={"five_hour": Window(0.95, NOW + 600)}))
        assert await gate.check() is not None
        assert await gate.check() is not None  # still paused: no second line
        assert len(said) == 1 and "Pausing" in said[0] and "5-hour" in said[0]
        now[0] = NOW + 600  # the window reset
        assert await gate.check() is None
        assert len(said) == 2 and "taking new work" in said[1]
        assert await gate.check() is None
        assert len(said) == 2

    @pytest.mark.asyncio
    async def test_a_chat_failure_does_not_stop_the_gate(self, tmp_path: Path) -> None:
        from ctrlrelay.core.usage import UsageGate

        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading(status="rejected"))

        async def broken(_t: str) -> None:
            raise OSError("socket gone")

        gate = UsageGate(store, threshold=0.9, notify=broken, clock=lambda: NOW)
        assert await gate.check() is not None


class TestTheDispatcherKeepsTheReading:
    def _dispatcher(self, store):
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        return ClaudeDispatcher(claude_binary="claude", usage_store=store)

    @pytest.mark.asyncio
    async def test_it_runs_stream_json_and_stores_the_reading(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        store = UsageStore(tmp_path / "usage.json")
        proc = AsyncMock()
        proc.communicate.return_value = (RECORDED.encode(), b"")
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
            result = await self._dispatcher(store).spawn_session(
                github_access=False, session_id="s", prompt="p",
                working_dir=tmp_path, state_file=tmp_path / "state.json",
            )
        args = spawn.call_args.args
        assert args[args.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in args
        assert result.agent_session_id == "3173130c-4a4c-4546-8050-88936d4238d6"
        assert store.read().windows["five_hour"].used == 0.28

    @pytest.mark.asyncio
    async def test_a_run_with_no_reading_records_unknown(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading())
        proc = AsyncMock()
        proc.communicate.return_value = (b'{"type":"result","session_id":"x"}', b"")
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await self._dispatcher(store).spawn_session(
                github_access=False, session_id="s", prompt="p",
                working_dir=tmp_path, state_file=tmp_path / "state.json",
            )
        # The older number would otherwise go on pausing the machine.
        assert store.read() is None
        assert json.loads(store.path.read_text())["unknown"] is True

    def test_every_daemon_and_command_dispatcher_says_where_usage_goes(self) -> None:
        import inspect

        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        param = inspect.signature(make_agent_dispatcher).parameters["usage_store"]
        assert param.default is inspect.Parameter.empty
        from ctrlrelay.core.config import AgentConfig

        with pytest.raises(TypeError, match="usage_store"):
            make_agent_dispatcher(AgentConfig(), outcome_hook=None)


class TestProbe:
    @pytest.mark.asyncio
    async def test_the_probe_stores_the_reading_from_a_real_shaped_run(
        self, tmp_path: Path,
    ) -> None:
        from ctrlrelay.core.usage import probe_usage

        fake = tmp_path / "agent"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s' \"$*\" > {tmp_path}/args\n"
            f"cat {FIXTURE}\n"
        )
        fake.chmod(0o755)
        store = UsageStore(tmp_path / "usage.json")
        reading = await probe_usage(str(fake), store)
        assert reading is not None and store.read() == reading
        args = (tmp_path / "args").read_text()
        assert "--tools  --strict-mcp-config --setting-sources  " in args
        assert "--max-turns 1" in args

    @pytest.mark.asyncio
    async def test_a_probe_with_no_reading_records_unknown(
        self, tmp_path: Path,
    ) -> None:
        from ctrlrelay.core.usage import probe_usage

        fake = tmp_path / "agent"
        fake.write_text("#!/bin/sh\necho 'rate limited, try later' >&2\nexit 1\n")
        fake.chmod(0o755)
        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading(windows={"five_hour": Window(0.97, NOW + 600)}))
        assert await probe_usage(str(fake), store) is None
        assert store.read() is None  # an older high number no longer pauses

    @pytest.mark.asyncio
    async def test_a_missing_binary_is_logged_not_raised(self, tmp_path: Path) -> None:
        from ctrlrelay.core.usage import probe_usage

        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading())
        assert await probe_usage(str(tmp_path / "nope"), store) is None
        assert store.read() == _reading()  # could not run: nothing learned


class TestTheStatusBlock:
    def _config(self, tmp_path: Path):
        from ctrlrelay.core.config import Config

        return Config.model_validate({
            "node_id": "node-a",
            "paths": {
                "state_db": str(tmp_path / "state.db"),
                "worktrees": str(tmp_path / "wt"),
                "bare_repos": str(tmp_path / "repos"),
                "contexts": str(tmp_path / "ctx"),
                "skills": str(tmp_path / "skills"),
            },
            "transport": {"type": "file_mock", "file_mock": {
                "inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out"),
            }},
        })

    def _node(self, tmp_path: Path) -> dict:
        from ctrlrelay.web.server import StatusServer

        server = StatusServer.__new__(StatusServer)
        server.config = self._config(tmp_path)
        return server.node()

    def test_unknown_before_any_reading(self, tmp_path: Path) -> None:
        block = self._node(tmp_path)
        assert block["name"] == "node-a"
        assert block["usage"] == "unknown"
        assert block["paused"] is None

    def test_numbers_reset_times_and_pause(self, tmp_path: Path) -> None:
        import time

        now = int(time.time())
        UsageStore.beside(tmp_path / "state.db").write(UsageReading(
            status="allowed",
            windows={"five_hour": Window(0.93, now + 600), "seven_day": Window(0.4, now + 9000)},
            read_at=now,
        ))
        block = self._node(tmp_path)
        assert block["usage"]["five_hour"]["used"] == 0.93
        assert block["usage"]["five_hour"]["resets_at"].endswith("Z")
        assert block["paused"]["reason"] == "5-hour usage at 93%"

    def test_a_node_reader_that_raises_is_null_with_an_error(self, tmp_path: Path) -> None:
        from ctrlrelay.web.status import build_status

        def boom() -> dict:
            raise RuntimeError("disk gone")

        out = build_status(
            db_path=tmp_path / "missing.db", waiting={}, server=dict,
            version="v", host="h", node=boom,
        )
        assert out["node"] is None
        assert any("node: RuntimeError" in e for e in out["errors"])


class TestAPausedMachineTakesTheIssueAfterTheReset:
    @pytest.mark.asyncio
    async def test_left_until_the_reset_then_offered_again(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        import ctrlrelay.core.poller as poller_mod
        from ctrlrelay.cli import defer_for_usage
        from ctrlrelay.core.poller import IssuePoller
        from ctrlrelay.core.usage import UsageGate

        now = [float(NOW)]
        monkeypatch.setattr(poller_mod.time, "time", lambda: now[0])
        gh = MagicMock()
        gh.list_assigned_issues = AsyncMock(return_value=[{"number": 7, "title": "t"}])
        gh.list_issues_by_label = AsyncMock(return_value=[])
        gh.list_assignment_events = AsyncMock(return_value=[{
            "event": "assigned", "actor": {"login": "alice"},
            "assignee": {"login": "alice"},
        }])
        poller = IssuePoller(
            github=gh, username="alice", repos=["o/r"],
            state_file=tmp_path / "poller_state.json",
        )
        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading(windows={"five_hour": Window(0.95, NOW + 600)}))
        gate = UsageGate(store, threshold=0.9, clock=lambda: now[0])

        [item] = await poller.poll()
        pause = await gate.check()
        assert pause is not None
        defer_for_usage(poller, "o/r", item["issue"]["number"], pause)

        now[0] = NOW + 599
        assert await poller.poll() == []
        now[0] = NOW + 600
        assert [i["issue"]["number"] for i in await poller.poll()] == [7]
        assert await gate.check() is None



class TestRoundOneFixes:
    @pytest.mark.parametrize("bad", ["1e999", "NaN", "Infinity", "-1", "99999999999"])
    def test_a_reset_that_is_not_a_time_is_unknown_not_a_crash(self, bad: str) -> None:
        text = RECORDED.replace('"resetsAt": 1793503200, "status"', f'"resetsAt": {bad}, "status"')
        r = parse_rate_limit_event(text, now=NOW)
        assert r is not None and r.windows["five_hour"].resets_at is None
        assert usage_json(r)["five_hour"]["resets_at"] is None

    def test_a_utilization_that_is_not_finite_is_unknown(self) -> None:
        text = RECORDED.replace('"utilization": 0.28', '"utilization": NaN')
        r = parse_rate_limit_event(text, now=NOW)
        assert "five_hour" not in r.windows

    def test_a_stored_file_with_garbage_numbers_reads_safely(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.json"
        path.write_text(
            '{"status": "allowed", "read_at": 1793490000,'
            ' "five_hour": {"used": 0.5, "resets_at": 1e999}}'
        )
        r = UsageStore(path).read()
        assert r.windows["five_hour"].resets_at is None

    def test_a_refusal_ends_at_its_own_reset_not_the_weekly_one(self) -> None:
        text = RECORDED.replace(
            '"status": "allowed", "resetsAt": 1793503200',
            '"status": "rejected", "resetsAt": 1793503200',
        )
        r = parse_rate_limit_event(text, now=NOW)
        assert r.resets_at == 1793503200
        assert pause_for(r, threshold=0.9, now=NOW).until == 1793503200

    def test_only_the_last_event_counts_even_when_it_is_broken(self) -> None:
        broken = '{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}'
        assert parse_rate_limit_event(RECORDED + broken + "\n", now=NOW) is None
        cut_short = '{"type": "rate_limit_event", "rate_limit_info": {"stat'
        assert parse_rate_limit_event(RECORDED + cut_short + "\n", now=NOW) is None

    @pytest.mark.asyncio
    async def test_a_refused_probe_still_reports_the_usage_that_refused_it(
        self, tmp_path: Path,
    ) -> None:
        from ctrlrelay.core.usage import probe_usage

        fake = tmp_path / "agent"
        refused = tmp_path / "refused.jsonl"
        refused.write_text(RECORDED.replace(
            '"status": "allowed", "resetsAt": 1793503200',
            '"status": "rejected", "resetsAt": 1793503200',
        ))
        fake.write_text(f"#!/bin/sh\ncat {refused}\nexit 2\n")
        fake.chmod(0o755)
        store = UsageStore(tmp_path / "usage.json")
        assert await probe_usage(str(fake), store) is not None
        assert store.read().status == "rejected"

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_anything_returns_none(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from ctrlrelay.core.usage import probe_usage

        with patch("asyncio.create_subprocess_exec", side_effect=ValueError("bad")):
            assert await probe_usage("agent", UsageStore(tmp_path / "u.json")) is None

    @pytest.mark.asyncio
    async def test_a_probe_that_times_out_is_killed(self, tmp_path: Path) -> None:
        import os

        from ctrlrelay.core.usage import probe_usage

        fake = tmp_path / "agent"
        fake.write_text(f"#!/bin/sh\necho $$ > {tmp_path}/pid\nexec sleep 30\n")
        fake.chmod(0o755)
        assert await probe_usage(str(fake), UsageStore(tmp_path / "u.json"), timeout=1) is None
        pid = int((tmp_path / "pid").read_text())
        with pytest.raises(ProcessLookupError):  # killed and reaped, not left behind
            os.kill(pid, 0)

    @pytest.mark.asyncio
    async def test_resuming_wakes_the_issues_left_for_later(self, tmp_path: Path) -> None:
        from ctrlrelay.core.usage import UsageGate

        store = UsageStore(tmp_path / "usage.json")
        woke: list[bool] = []
        gate = UsageGate(store, threshold=0.9, on_resume=lambda: woke.append(True),
                         clock=lambda: NOW)
        store.write(_reading(windows={"five_hour": Window(0.95, NOW + 600)}))
        await gate.check()
        assert woke == []
        store.write(_reading())  # a probe reads it lower before the reset
        await gate.check()
        assert woke == [True]



class TestRoundTwoFixes:
    def test_a_number_with_hundreds_of_digits_is_unknown(self, tmp_path: Path) -> None:
        huge = "9" * 400
        text = RECORDED.replace('"utilization": 0.28', f'"utilization": {huge}')
        r = parse_rate_limit_event(text, now=NOW)
        assert "five_hour" not in r.windows
        path = tmp_path / "usage.json"
        path.write_text(
            '{"status": "allowed", "read_at": 1793490000, '
            f'"five_hour": {{"used": 0.5, "resets_at": {huge}}}}}'
        )
        assert UsageStore(path).read().windows["five_hour"].resets_at is None

    def test_an_event_cut_off_inside_its_type_is_unreadable(self) -> None:
        assert parse_rate_limit_event(RECORDED + '{"type": "rate_lim\n', now=NOW) is None

    def test_a_high_window_with_no_reset_time_pauses_for_an_hour_only(self) -> None:
        r = _reading(windows={"five_hour": Window(0.99, None)})
        assert pause_for(r, threshold=0.9, now=NOW).until == NOW + 3600
        assert pause_for(r, threshold=0.9, now=NOW + 3600) is None

    def test_a_refusal_with_no_reset_time_pauses_for_an_hour(self) -> None:
        r = UsageReading(
            status="rejected", windows={"five_hour": Window(0.99, None)}, read_at=NOW,
        )
        assert pause_for(r, threshold=0.9, now=NOW).until == NOW + 3600
        assert pause_for(r, threshold=0.9, now=NOW + 3600) is None

    def test_a_dispatcher_cannot_be_built_without_saying_where_usage_goes(self) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        with pytest.raises(TypeError, match="usage_store"):
            ClaudeDispatcher(claude_binary="claude")



class TestRoundThreeFixes:
    @pytest.mark.parametrize("line", [
        '{"type": "result", "session_id": "x", "n": ' + "9" * 5000 + "}",
        '{"a":' * 100000 + "1" + "}" * 100000,
    ])
    def test_hostile_json_is_unreadable_not_a_crash(self, line: str) -> None:
        assert parse_agent_session_id(line + "\n" + RECORDED) is not None
        assert parse_rate_limit_event(RECORDED + line + "\n", now=NOW) is None
        assert parse_agent_session_id(line) is None

    @pytest.mark.asyncio
    async def test_a_hung_chat_send_does_not_hold_the_gate(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import asyncio

        import ctrlrelay.core.usage as usage_mod
        from ctrlrelay.core.usage import UsageGate

        monkeypatch.setattr(usage_mod, "NOTIFY_TIMEOUT_SECONDS", 0.05)
        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading(status="rejected"))

        async def hang(_t: str) -> None:
            await asyncio.Event().wait()

        gate = UsageGate(store, threshold=0.9, notify=hang, clock=lambda: NOW)
        assert await asyncio.wait_for(gate.check(), timeout=2) is not None

    @pytest.mark.asyncio
    async def test_the_dispatcher_counts_its_running_sessions(self, tmp_path: Path) -> None:
        import asyncio
        from unittest.mock import AsyncMock, patch

        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        d = ClaudeDispatcher(claude_binary="claude", usage_store=None)
        seen: list[int] = []
        release = asyncio.Event()

        async def communicate():
            seen.append(d.active)
            await release.wait()
            return (b"", b"")

        proc = AsyncMock()
        proc.communicate.side_effect = communicate
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            task = asyncio.create_task(d.spawn_session(
                github_access=False, session_id="s", prompt="p",
                working_dir=tmp_path, state_file=tmp_path / "state.json",
            ))
            while not seen:
                await asyncio.sleep(0)
            release.set()
            await task
        assert seen == [1]
        assert d.active == 0
        assert d.started == 1



class TestRoundFourFixes:
    @pytest.mark.asyncio
    async def test_a_probe_drops_its_reading_when_a_run_started_meanwhile(
        self, tmp_path: Path,
    ) -> None:
        from ctrlrelay.core.usage import probe_usage

        fake = tmp_path / "agent"
        fake.write_text(f"#!/bin/sh\ncat {FIXTURE}\n")
        fake.chmod(0o755)
        store = UsageStore(tmp_path / "usage.json")
        store.write(_reading(windows={"five_hour": Window(0.97, NOW + 600)}))
        assert await probe_usage(str(fake), store, still_idle=lambda: False) is None
        assert store.read().windows["five_hour"].used == 0.97

    def test_a_file_that_is_not_utf8_reads_as_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.json"
        path.write_bytes(b'{"status": "\xff\xfe"}')
        assert UsageStore(path).read() is None



class TestColdReviewFixes:
    def test_a_stored_read_time_that_is_not_a_time_reads_as_unknown(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "usage.json"
        path.write_text('{"status": "rejected", "read_at": 100000000000000000000}')
        assert UsageStore(path).read() is None

    def test_a_reading_from_the_future_pauses_an_hour_from_now_at_most(self) -> None:
        r = UsageReading(status="rejected", windows={}, read_at=NOW + 86400)
        assert pause_for(r, threshold=0.9, now=NOW).until == NOW + 3600

    def test_an_unknown_reading_still_has_an_attempt_time(self, tmp_path: Path) -> None:
        store = UsageStore(tmp_path / "usage.json")
        assert store.last_attempt_at() is None
        store.mark_unknown(now=NOW)
        assert store.read() is None
        assert store.last_attempt_at() == NOW

    def test_the_session_id_comes_from_the_result_line(self) -> None:
        lines = [
            '{"type": "system", "subtype": "init", "session_id": "from-init"}',
            '{"type": "result", "subtype": "success", "session_id": "from-result"}',
        ]
        assert parse_agent_session_id("\n".join(lines)) == "from-result"
