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
    summarize_command,
    transcript_dir_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
WORKTREE = "/home/u/.ctrlrelay/worktrees/example-org-alpha-dev-215-49a5"

# The fixture's last tool use, read off the file by hand: the agent opening its PR.
LAST_COMMAND = "gh pr create \u2026 && tail \u2026"
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
    worktree = "/home/u/.ctrlrelay/worktrees/example_org-alpha-dev-215-49a5"
    assert transcript_dir_for(worktree, Path("/home/u")) == Path(
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
        # The phase is the last simple command that says something, not any word in the line.
        ("Bash", {"command": "uv run pytest -q && git push"}, Phase.PR),
        ("Bash", {"command": "git push && pytest -q"}, Phase.TESTING),
        ("Bash", {"command": "uv run pytest -q 2>&1 | tail -20"}, Phase.TESTING),
        ("Bash", {"command": "echo \"git push\""}, Phase.READING),
        ("Bash", {"command": "cat pytest.log"}, Phase.READING),
        ("Bash", {"command": "cd /w && FOO=1 python -m pytest tests"}, Phase.TESTING),
        ("Bash", {"command": "git commit -F - <<'MSG'\ngit push later\nMSG\n"}, Phase.READING),
        ("Bash", {"command": "npm run test:unit"}, Phase.TESTING),
        ("Bash", {"command": "go test ./..."}, Phase.TESTING),
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


def test_a_line_too_deep_to_parse_is_skipped(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    path = d / "d9f6c0f2.jsonl"
    shutil.copy(FIXTURE, path)
    whole = read_activity(WORKTREE, home=tmp_path)
    with pytest.raises(RecursionError):
        json.loads("[" * 100_000)
    with path.open("a") as fh:
        fh.write("[" * 100_000 + "\n")

    assert read_activity(WORKTREE, home=tmp_path) == whole


def _boundary_file(tmp_path: Path) -> int:
    d = _transcript_dir(tmp_path)
    last = _assistant_row("Bash", {"command": "uv run pytest -q"}, "2026-10-31T03:30:00.000Z")
    (d / "t.jsonl").write_text(
        _assistant_row("Edit", {"file_path": "/a.py"}, "2026-10-31T03:00:00.000Z")
        + "\n"
        + last
        + "\n"
    )
    return len(last.encode()) + 1


def test_a_tail_starting_exactly_on_a_row_keeps_that_row(tmp_path: Path) -> None:
    last_row_bytes = _boundary_file(tmp_path)

    # The first byte of the tail is the newline before the row: the row is whole.
    on_row = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes + 1)
    inside_row = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes)
    before_row = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes + 6)

    assert on_row.phase is Phase.TESTING
    assert inside_row.phase is Phase.UNKNOWN  # only part of the row is in the tail
    assert before_row.phase is Phase.TESTING


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


def test_a_huge_tail_bytes_reads_the_whole_file(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    shutil.copy(FIXTURE, d / "d9f6c0f2.jsonl")

    activity = read_activity(WORKTREE, home=tmp_path, tail_bytes=10**100)

    assert activity == read_activity(WORKTREE, home=tmp_path)
    assert activity.phase is Phase.PR


def test_last_command_truncated_to_200(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    long_command = "echo start\n" + "true && " * 100 + "echo end"
    (d / "t.jsonl").write_text(
        _assistant_row("Bash", {"command": long_command}, "2026-10-31T03:00:00.000Z") + "\n"
    )

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.last_command is not None
    assert len(activity.last_command) == 200
    assert "\n" not in activity.last_command
    assert activity.last_command.startswith("echo start && true && true")


@pytest.mark.parametrize(
    ("command", "shown"),
    [
        ("git status", "git status"),
        ("git push -u origin x", "git push \u2026"),
        ("uv run pytest -q tests/a.py", "pytest \u2026"),
        ("poetry run pytest", "pytest"),
        ("bunx vitest run", "vitest run"),
        ("python -m pytest -p xdist", "pytest \u2026"),
        (
            'export API_KEY=abc && curl -H "Bearer x" https://h',
            "export \u2026 && curl \u2026",
        ),
        ("cd /w && npm run test:unit", "npm run test:unit"),
        ("uv run pytest -q 2>&1 | tail -20", "pytest \u2026 && tail \u2026"),
        ("gh pr create --fill", "gh pr create \u2026"),
        ("./scripts/rotate-token.sh now", "*** now"),
        ("git commit -F - <<'MSG'\nprivate words\nMSG", "git commit \u2026"),
    ],
)
def test_summarize_command(command: str, shown: str) -> None:
    assert summarize_command(command) == shown


def _shown_for(tmp_path: Path, name: str, tool_input: dict) -> str | None:
    d = transcript_dir_for(WORKTREE, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "t.jsonl").write_text(_assistant_row(name, tool_input, "2026-10-31T03:00:00.000Z") + "\n")
    return read_activity(WORKTREE, home=tmp_path).last_command


# Every leak found by the two review rounds, through the path the page reads.
@pytest.mark.parametrize(
    ("command", "secrets"),
    [
        ("tool --verbose --password hunter2", ["hunter2"]),
        ("echo xoxs-123-456-abc", ["xoxs", "123-456"]),
        ("echo AbCdEfGh/IjKlMnOp+QrStUvWx/YzAbCdEf+GhIjKlMn==", ["AbCd", "IjKl"]),
        ('llm --prompt "Private customer details"', ["Private", "customer"]),
        ("export API_KEY=secret", ["API_KEY", "secret"]),
        ("mysql -psecret", ["secret"]),
        ("mysql -u x -p secret", ["secret"]),
        ("sshpass -p secret ssh h", ["secret"]),
        ("GH_TOKEN=abc gh pr create --fill", ["GH_TOKEN", "abc"]),
        ("git clone https://ghp_abcdef123@x/r", ["ghp_", "abcdef123"]),
        ('curl -H "Authorization: Bearer abc.def" h', ["abc.def", "Bearer"]),
        ("echo sk-proj-abc123", ["sk-proj"]),
        ("echo " + "a1b2" * 10, ["a1b2"]),
        ("tool login deploy-key-1234", ["deploy-key"]),
    ],
)
def test_no_secret_reaches_last_command(
    tmp_path: Path, command: str, secrets: list[str]
) -> None:
    shown = _shown_for(tmp_path, "Bash", {"command": command})

    assert shown
    for secret in secrets:
        assert secret not in shown


@pytest.mark.parametrize(
    ("name", "tool_input", "shown"),
    [
        ("Read", {"file_path": f"{WORKTREE}/src/app.py"}, "src/app.py"),
        ("Edit", {"file_path": f"{WORKTREE}/src/app.py", "old_string": "x"}, "src/app.py"),
        ("Write", {"file_path": "/etc/ssl/site.conf", "content": "x"}, "site.conf"),
        ("NotebookEdit", {"notebook_path": f"{WORKTREE}/nb/a.ipynb"}, "nb/a.ipynb"),
        ("Read", {"file_path": f"{WORKTREE}/secrets/prod.env"}, "***/prod.env"),
        ("Read", {"file_path": "/home/u/.config/tokens.json"}, "***"),
        ("Grep", {"pattern": "ghp_abcdef123", "path": "/x"}, "search"),
        ("Glob", {"pattern": "**/password*.txt"}, "search"),
    ],
)
def test_file_and_search_tools_show_a_safe_string(
    tmp_path: Path, name: str, tool_input: dict, shown: str
) -> None:
    assert _shown_for(tmp_path, name, tool_input) == shown
