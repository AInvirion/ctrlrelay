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
    redact,
    transcript_dir_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
WORKTREE = "/home/u/.ctrlrelay/worktrees/example-org-alpha-dev-215-49a5"

# The fixture's last tool use, read off the file by hand: the agent opening its PR.
LAST_COMMAND = (
    "gh pr create --repo example-org/alpha --base main"
    " --head fix/issue-1 --title \"...\" --body \"$(cat <<\u2026"
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

    on_row = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes)
    inside_row = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes - 1)
    before_row = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes + 5)

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
    assert max(asked) <= tail_bytes
    assert sum(asked) <= tail_bytes + 1  # plus the one byte before the cut


def test_last_command_truncated_to_200(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    long_command = "echo start\n" + "x " * 300 + "\necho end"
    (d / "t.jsonl").write_text(
        _assistant_row("Bash", {"command": long_command}, "2026-10-31T03:00:00.000Z") + "\n"
    )

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.last_command is not None
    assert len(activity.last_command) == 200
    assert "\n" not in activity.last_command
    assert activity.last_command.startswith("echo start x")


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("git status --short", "git status --short"),
        ("uv run pytest -q tests/test_x.py", "uv run pytest -q tests/test_x.py"),
        (
            'export OPENAI_API_KEY=abc SECRET_KEY="a b" LEVEL=3 && make',
            "export OPENAI_API_KEY=*** SECRET_KEY=*** LEVEL=3 && make",
        ),
        ("curl --token abc123 https://x", "curl --token *** https://x"),
        ("tool --auth-token=xyz run", "tool --auth-token=*** run"),
        ("mysql -u app -pHunter2 db", "mysql -u app -p*** db"),
        ("psql -pHunter2 db", "psql -p*** db"),
        ("tool --password hunter2 run", "tool --password *** run"),
        ("pytest -p no:warnings -q", "pytest -p no:warnings -q"),
        ("uv run pytest -p no:cacheprovider", "uv run pytest -p no:cacheprovider"),
        ("python -m pytest -p xdist", "python -m pytest -p xdist"),
        ("mysql -u x -p secret", "mysql -u x -p ***"),
        ("sshpass -p secret ssh h", "sshpass -p *** ssh h"),
        ("somecmd -p secret", "somecmd -p ***"),
        ("pytest -q && somecmd -p secret", "pytest -q && somecmd -p ***"),
        ("mariadb -pHunter2 db", "mariadb -p*** db"),
        ("pg_dump -pHunter2 db", "pg_dump -p*** db"),
        ("sshpass -pHunter2 ssh h", "sshpass -p*** ssh h"),
        ("echo configuration_management_module", "echo configuration_management_module"),
        ("echo deadbeef12deadbeef34", "echo ***"),
        ("echo Ab12-Cd34_Ef56-Gh78-Ij90", "echo ***"),
        ("echo AbC1+dEf2/GhI3+jKl4/MnO5+pQr6/StU7+vWx8/Yz0=", "echo ***"),
        (
            "cat /home/u/.ctrlrelay/worktrees/example-org-alpha-service-dev-example-org"
            "-alpha-service-304-c1446f76/src/app.py",
            "cat /home/u/.ctrlrelay/worktrees/example-org-alpha-service-dev-example-org"
            "-alpha-service-304-c1446f76/src/app.py",
        ),
        (
            "ls /home/u/.ctrlrelay/worktrees/example-org-tools-secops-example-org-tools-6cbd20da",
            "ls /home/u/.ctrlrelay/worktrees/example-org-tools-secops-example-org-tools-6cbd20da",
        ),
        ("git show 5bf7858dbe7a3e679e17b9176ae099f225b76c77", "git show ***"),
        ("curl https://x/r/" + "a1B2" * 10 + "/raw", "curl https://x/r/***/raw"),
        ("git clone https://ghp_abcdef123@x/r", "git clone https://***@x/r"),
        ("echo gho_abc ghs_def github_pat_11ABC_def", "echo *** *** ***"),
        ("echo sk-proj-abc123", "echo ***"),
        ("echo xoxb-123-456-abc", "echo ***"),
        ('curl -H "Authorization: Bearer abc.def"', 'curl -H "Authorization: Bearer ***"'),
        ("aws s3 ls # AKIAABCDEFGHIJKLMNOP", "aws s3 ls # ***"),
        ("echo " + "a1B2" * 8, "echo ***"),
        ("git commit -F - <<'MSG'\nprivate words\nMSG", "git commit -F - <<\u2026"),
    ],
)
def test_redact(raw: str, shown: str) -> None:
    assert redact(raw) == shown


def test_last_command_is_redacted_on_the_way_out(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    push = _assistant_row(
        "Bash", {"command": "GH_TOKEN=abc gh pr create --fill"}, "2026-10-31T03:00:00.000Z"
    )
    (d / "t.jsonl").write_text(push + "\n")

    assert read_activity(WORKTREE, home=tmp_path).last_command == (
        "GH_TOKEN=*** gh pr create --fill"
    )

    grep = _assistant_row("Grep", {"pattern": "ghp_abcdef123"}, "2026-10-31T03:01:00.000Z")
    (d / "t.jsonl").write_text(push + "\n" + grep + "\n")

    activity = read_activity(WORKTREE, home=tmp_path)
    assert activity.last_command == "***"
    assert activity.phase is Phase.READING
