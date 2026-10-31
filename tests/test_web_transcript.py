"""Tests for reading an agent's phase from its live transcript."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctrlrelay.web import transcript as transcript_module
from ctrlrelay.web.transcript import (
    AgentActivity,
    Phase,
    classify,
    read_activity,
    transcript_dir_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
WORKTREE = "/home/u/.ctrlrelay/worktrees/example-org-alpha-dev-215-49a5"

# The fixture's last tool use, read off the file by hand: the agent opening its PR.
LAST_COMMAND = (
    "gh pr create --repo example-org/alpha --base main"
    " --head fix/issue-1 --title \"...\" --body \"$(cat <<'BODY' ... BODY )\""
    " 2>&1 | tail -3"
)
LAST_STEP_TS = int(datetime(2026, 10, 31, 3, 59, 16, 739000, tzinfo=UTC).timestamp())


def _transcript_dir(home: Path) -> Path:
    d = transcript_dir_for(WORKTREE, home)
    d.mkdir(parents=True)
    return d


def _assistant_row(name: str, tool_input: dict, ts: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": name, "input": tool_input}
                ],
            },
        }
    )


def test_transcript_dir_matches_measured_layout() -> None:
    assert transcript_dir_for(WORKTREE, Path("/home/u")) == Path(
        "/home/u/.claude/projects/-home-u--ctrlrelay-worktrees-example-org-alpha-dev-215-49a5"
    )


@pytest.mark.parametrize(
    ("name", "tool_input", "expected"),
    [
        ("Read", {}, Phase.READING),
        ("Edit", {}, Phase.CODING),
        ("Bash", {"command": "uv run pytest -q"}, Phase.TESTING),
        ("Bash", {"command": "npx playwright test"}, Phase.TESTING),
        ("Bash", {"command": "git push -u origin x"}, Phase.PR),
        ("Bash", {"command": "gh pr create --fill"}, Phase.PR),
        # The push is the step the command ends on, so it wins over the run before it.
        ("Bash", {"command": "uv run pytest -q && git push"}, Phase.PR),
        ("Bash", {"command": "sed -n '1,60p' f.py"}, Phase.READING),
        ("TodoWrite", {}, None),
    ],
)
def test_classify(name: str, tool_input: dict, expected: Phase | None) -> None:
    assert classify(name, tool_input) is expected


def test_read_activity_from_real_fixture(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    shutil.copy(FIXTURE, d / "d9f6c0f2.jsonl")

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity == AgentActivity(
        phase=Phase.PR,
        last_command=LAST_COMMAND,
        last_step_ts=LAST_STEP_TS,
        transcript=str(d / "d9f6c0f2.jsonl"),
        reason=None,
    )


def test_half_written_last_line_is_ignored(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    path = d / "d9f6c0f2.jsonl"
    shutil.copy(FIXTURE, path)
    whole = read_activity(WORKTREE, home=tmp_path)

    # A step the agent CLI is still writing: valid JSON up to the cut.
    pending = _assistant_row("Edit", {"file_path": "/x.py"}, "2026-10-31T04:10:00.000Z")
    with path.open("a") as fh:
        fh.write(pending[: len(pending) // 2])

    assert read_activity(WORKTREE, home=tmp_path) == whole
    assert whole.phase is Phase.PR


def test_a_tool_use_outside_an_assistant_row_is_not_a_step(tmp_path: Path) -> None:
    # Real transcripts also carry `api-request-blob` rows whose `message` holds
    # the same tool_use entries; only `assistant` rows are steps.
    d = _transcript_dir(tmp_path)
    path = d / "d9f6c0f2.jsonl"
    shutil.copy(FIXTURE, path)
    blob = json.loads(_assistant_row("Read", {"file_path": "/y.py"}, "2026-10-31T04:10:00.000Z"))
    blob["type"] = "api-request-blob"
    with path.open("a") as fh:
        fh.write(json.dumps(blob) + "\n")

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.phase is Phase.PR
    assert activity.last_command == LAST_COMMAND


def test_no_transcript_is_unknown(tmp_path: Path) -> None:
    _transcript_dir(tmp_path)

    activity = read_activity(WORKTREE, home=tmp_path)
    assert activity.phase is Phase.UNKNOWN
    assert activity.reason == "no transcript"
    assert activity.last_command is None

    gone = read_activity("/home/u/.ctrlrelay/worktrees/never-made", home=tmp_path)
    assert gone.phase is Phase.UNKNOWN
    assert gone.reason == "no transcript"

    none = read_activity(None, home=tmp_path)
    assert none.phase is Phase.UNKNOWN
    assert none.reason == "no worktree"


def test_reads_only_the_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _transcript_dir(tmp_path)
    path = d / "big.jsonl"
    filler = _assistant_row("Edit", {"file_path": "/a.py"}, "2026-10-31T03:00:00.000Z")
    with path.open("w") as fh:
        while fh.tell() < 5 * 1024 * 1024:
            fh.write(filler + "\n")
        fh.write(
            _assistant_row("Bash", {"command": "uv run pytest -q"}, "2026-10-31T03:30:00.000Z")
            + "\n"
        )
    assert path.stat().st_size > 5 * 1024 * 1024

    asked: list[int | None] = []
    real_open = open

    class Spy:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n: int | None = -1):
            asked.append(None if n is None or n < 0 else n)
            return self._fh.read(n)

        def __getattr__(self, attr):
            return getattr(self._fh, attr)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    monkeypatch.setattr(
        transcript_module, "open", lambda *a, **k: Spy(real_open(*a, **k)), raising=False
    )

    tail_bytes = 65_536
    activity = read_activity(WORKTREE, home=tmp_path, tail_bytes=tail_bytes)

    assert activity.phase is Phase.TESTING
    assert asked, "the reader did not go through open().read"
    assert None not in asked
    assert sum(asked) <= tail_bytes


def test_last_command_truncated_to_200(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    long_command = "echo start\n" + "x" * 500 + "\necho end"
    (d / "t.jsonl").write_text(
        _assistant_row("Bash", {"command": long_command}, "2026-10-31T03:00:00.000Z") + "\n"
    )

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.last_command is not None
    assert len(activity.last_command) == 200
    assert "\n" not in activity.last_command
    assert activity.last_command.startswith("echo start x")
