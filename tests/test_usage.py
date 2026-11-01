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
        p = pause_for(_reading(status="rejected"), threshold=0.9, now=NOW)
        assert p is not None and p.until == NOW + 86400

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
