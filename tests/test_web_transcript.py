"""Tests for reading an agent's phase from its live transcript."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctrlrelay.web import transcript as transcript_module
from ctrlrelay.web.transcript import (
    OUTSIDE_REPO,
    AgentActivity,
    Phase,
    classify,
    read_activity,
    summarize_command,
    summarize_path,
    transcript_dir_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
WORKTREE = "/home/u/.ctrlrelay/worktrees/example-org-alpha-dev-215-49a5"

# The fixture's last tool use, read off the file by hand: the agent opening its PR.
LAST_COMMAND = "gh pr \u2026 && tail \u2026"
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
        ("Bash", {"command": "echo \"git push\""}, None),
        ("Bash", {"command": "cat pytest.log"}, Phase.READING),
        ("Bash", {"command": "cd /w && FOO=1 python -m pytest tests"}, Phase.TESTING),
        ("Bash", {"command": "git commit -F - <<'MSG'\ngit push later\nMSG\n"}, Phase.CODING),
        ("Bash", {"command": "npm run test:unit"}, Phase.TESTING),
        ("Bash", {"command": "go test ./..."}, Phase.TESTING),
        ("Bash", {"command": "sed -n '1,60p' f.py"}, Phase.READING),
        # shlex hands ";\n" over as one token; it still ends the first command.
        ("Bash", {"command": "git push;\npytest"}, Phase.TESTING),
        ("Bash", {"command": "env FOO=bar pytest"}, Phase.TESTING),
        ("Bash", {"command": "env -i FOO=bar git push"}, Phase.PR),
        ("Bash", {"command": "env -u FOO pytest"}, Phase.TESTING),
        ("Bash", {"command": "env --chdir /w -0 pytest"}, Phase.TESTING),
        # Quoted text is a word, never an operator or a heredoc.
        ("Bash", {"command": 'pytest ";" git push'}, Phase.TESTING),
        ("Bash", {"command": "echo '<<EOF'\ngit push"}, Phase.PR),
        ("Bash", {"command": "cat <<-EOF\ngit push\nEOF\npytest -q"}, Phase.TESTING),
        ("Bash", {"command": "git commit -F - <<'M'\nit's done; git push\nM"}, Phase.CODING),
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
        furthest=Phase.PR,
        phase_ts=LAST_STEP_TS,
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


def test_a_probe_byte_that_is_a_newline_keeps_the_row_after_it(tmp_path: Path) -> None:
    last_row_bytes = _boundary_file(tmp_path)

    # One probe byte (the newline before the row) plus the row and its newline.
    activity = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes + 1)

    assert activity.phase is Phase.TESTING


def test_a_tail_too_short_for_the_last_row_finds_no_step(tmp_path: Path) -> None:
    last_row_bytes = _boundary_file(tmp_path)

    # The probe is the row's own first byte, so what follows is only part of it.
    activity = read_activity(WORKTREE, home=tmp_path, tail_bytes=last_row_bytes)

    assert activity.phase is Phase.UNKNOWN
    assert activity.reason == "no step in transcript tail"


def test_a_tail_starting_inside_a_row_never_parses_that_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cut row is the only step in the file; the row after it is not a step.
    d = _transcript_dir(tmp_path)
    step = _assistant_row("Bash", {"command": "uv run pytest -q"}, "2026-10-31T03:30:00.000Z")
    after = json.dumps({"type": "user", "timestamp": "2026-10-31T03:31:00.000Z"})
    (d / "t.jsonl").write_text(step + "\n" + after + "\n")
    tail_bytes = 1 + 6 + 1 + len(after) + 1  # probe, 6 bytes of the step, "\n", the row after
    fragment = step[-6:]

    parsed: list[str] = []
    real_loads = json.loads

    def spy(text, *args, **kwargs):
        parsed.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(transcript_module.json, "loads", spy)
    activity = read_activity(WORKTREE, home=tmp_path, tail_bytes=tail_bytes)

    assert after in parsed  # the read reached the rows it had
    assert fragment not in parsed
    assert activity.phase is Phase.UNKNOWN


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
    assert len(activity.last_command) <= 200
    assert activity.last_command.startswith("cmd \u2026 && cmd && cmd")
    assert activity.last_command.endswith("cmd \u2026")
    # Whole words only: nothing cut in half at the limit.
    assert set(activity.last_command.split()) == {"cmd", "&&", "\u2026"}


@pytest.mark.parametrize(
    ("command", "shown"),
    [
        ("git status", "git status"),
        ("git push", "git push"),
        ("git push -u origin x", "git push \u2026"),
        ("git clone https://h/r", "git \u2026"),
        ("npm test", "npm test"),
        ("npm run test:unit", "npm run \u2026"),
        ("cd /w && npm run test:unit", "npm run \u2026"),
        ("gh pr create --fill", "gh pr \u2026"),
        ("uv sync", "uv sync"),
        ("uv run pytest -q tests/a.py", "pytest \u2026"),
        ("poetry run pytest", "pytest"),
        ("bunx vitest run", "vitest \u2026"),
        ("python -m pytest -p xdist", "pytest \u2026"),
        ("env FOO=bar pytest -q", "pytest \u2026"),
        ("python3 manage.py check", "python3 \u2026"),
        ("make test", "make \u2026"),
        ("docker compose up", "docker compose \u2026"),
        ("export API_KEY=abc && curl -H \"Bearer x\" https://h", "cmd \u2026 && curl \u2026"),
        ("uv run pytest -q 2>&1 | tail -20", "pytest \u2026 && tail \u2026"),
        ("git push;\npytest", "git push && pytest"),
        ("git commit -F - <<'MSG'\nprivate words\nMSG", "git commit \u2026"),
        ("swordfishctl", "cmd"),
    ],
)
def test_summarize_command(command: str, shown: str) -> None:
    assert summarize_command(command) == shown


def _shown_for(tmp_path: Path, name: str, tool_input: dict) -> str | None:
    d = transcript_dir_for(WORKTREE, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "t.jsonl").write_text(_assistant_row(name, tool_input, "2026-10-31T03:00:00.000Z") + "\n")
    return read_activity(WORKTREE, home=tmp_path).last_command


# Every leak found by the three review rounds, through the path the page reads.
@pytest.mark.parametrize(
    ("command", "secrets"),
    [
        ("tool --verbose --password hunter2", ["hunter2", "tool"]),
        ("echo xoxs-123-456-abc", ["xoxs", "123-456"]),
        ("echo AbCdEfGh/IjKlMnOp+QrStUvWx/YzAbCdEf+GhIjKlMn==", ["AbCd", "IjKl"]),
        ('llm --prompt "Private customer details"', ["Private", "customer", "llm"]),
        ("export API_KEY=secret", ["API_KEY", "secret"]),
        ("mysql -psecret", ["secret", "mysql"]),
        ("mysql -u x -p secret", ["secret"]),
        ("sshpass -p secret ssh h", ["secret"]),
        ("GH_TOKEN=abc gh pr create --fill", ["GH_TOKEN", "abc"]),
        ("git clone https://ghp_abcdef123@x/r", ["ghp_", "abcdef123"]),
        ('curl -H "Authorization: Bearer abc.def" h', ["abc.def", "Bearer"]),
        ("echo sk-proj-abc123", ["sk-proj"]),
        ("echo " + "a1b2" * 10, ["a1b2"]),
        ("tool login deploy-key-1234", ["deploy-key", "login"]),
        ("tool login abcdef0123abcdef0123abcdef0123", ["abcdef", "0123"]),
        ("echo swordfish", ["swordfish"]),
        ("grep confidential notes.txt", ["confidential", "notes"]),
        ("acme-internal-deployer --target prod", ["acme", "prod"]),
        ("git commit -m swordfish", ["swordfish"]),
        ("gh pr create --title swordfish", ["swordfish", "create"]),
        ("npm run swordfish", ["swordfish"]),
        ("cat /tmp/swordfish", ["swordfish"]),
        ("env SECRET=swordfish pytest", ["SECRET", "swordfish"]),
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
        ("NotebookEdit", {"notebook_path": f"{WORKTREE}/nb/a.ipynb"}, "nb/a.ipynb"),
        ("Write", {"file_path": "/etc/ssl/site.conf", "content": "x"}, "file outside repo"),
        ("Read", {"file_path": "/tmp/swordfish"}, "file outside repo"),
        ("Read", {"file_path": f"{WORKTREE}/../../../tmp/swordfish"}, "file outside repo"),
        ("Read", {"file_path": f"{WORKTREE}-other/app.py"}, "file outside repo"),
        ("Read", {"file_path": WORKTREE}, "file outside repo"),
        ("Grep", {"pattern": "ghp_abcdef123", "path": "/x"}, "search"),
        ("Glob", {"pattern": "**/swordfish*.txt"}, "search"),
    ],
)
def test_file_and_search_tools_show_a_safe_string(
    tmp_path: Path, name: str, tool_input: dict, shown: str
) -> None:
    assert _shown_for(tmp_path, name, tool_input) == shown


def test_a_symlink_out_of_the_worktree_is_outside(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (tmp_path / "private").mkdir()
    (worktree / "src" / "link").symlink_to(tmp_path / "private")
    d = transcript_dir_for(str(worktree), tmp_path)
    d.mkdir(parents=True)
    ts = "2026-10-31T03:00:00.000Z"
    via_link = _assistant_row("Read", {"file_path": f"{worktree}/src/link/swordfish"}, ts)
    inside = _assistant_row("Read", {"file_path": f"{worktree}/src/app.py"}, ts)

    (d / "t.jsonl").write_text(inside + "\n")
    assert read_activity(str(worktree), home=tmp_path).last_command == "src/app.py"

    (d / "t.jsonl").write_text(via_link + "\n")
    assert read_activity(str(worktree), home=tmp_path).last_command == "file outside repo"


@pytest.mark.parametrize(
    ("relative", "shown"),
    [
        ("src/app.py", "src/app.py"),
        ("docs/tokenizer-notes.md", "docs/***"),
        ("tokens/ghp_abc123DEF456ghi789", "***/***"),
        ("fixtures/ghp_abc123DEF456ghi789.json", "fixtures/***"),
        ("fixtures/gho_x", "fixtures/***"),
        ("fixtures/ghs_x", "fixtures/***"),
        ("fixtures/github_pat_11AB", "fixtures/***"),
        ("keys/sk-live-abc", "keys/***"),
        ("chat/xoxb-1-2", "chat/***"),
        ("chat/xoxp-1-2", "chat/***"),
        ("aws/AKIAIOSFODNN7EXAMPLE", "aws/***"),
        ("cache/a1b2c3d4e5f6a7b8c9d0e1f2", "cache/***"),
        (".env", "***"),
        ("deploy/prod.env", "deploy/***"),
        ("deploy/.env.local", "deploy/***"),
        ("deploy/.env", "deploy/***"),
        ("deploy/.envrc", "deploy/***"),
        ("config/api_key.txt", "config/***"),
        ("config/api-key", "config/***"),
        ("config/APIKEY", "config/***"),
        ("config/apikey.json", "config/***"),
        ("config/private.json", "config/***"),
        ("config/passw.txt", "config/***"),
        ("certs/client.p12", "certs/***"),
        ("certs/client.pfx", "certs/***"),
        ("home/id_ed25519", "home/***"),
        ("home/.netrc", "home/***"),
        ("home/.npmrc", "home/***"),
        ("home/.pypirc", "home/***"),
        ("home/id_rsa", "home/***"),
        ("home/id_rsa.pub", "home/***"),
        ("certs/server.pem", "certs/***"),
        ("certs/server.key", "certs/***"),
        ("config/Secrets.yaml", "config/***"),
        ("config/db_password.txt", "config/***"),
        ("config/passwd", "config/***"),
        ("config/credentials.json", "config/***"),
        # Ordinary names that only resemble a rule stay readable.
        ("src/task-runner/main.py", "src/task-runner/main.py"),
        ("src/desk-lamp.py", "src/desk-lamp.py"),
        ("src/keyboard.py", "src/keyboard.py"),
        ("src/environment.py", "src/environment.py"),
        ("src/abcdefghijklmnopqrstuvwxyz.py", "src/abcdefghijklmnopqrstuvwxyz.py"),
        ("src/12345678901234567890123.txt", "src/12345678901234567890123.txt"),
    ],
)
def test_path_components_that_look_like_credentials_are_masked(
    relative: str, shown: str
) -> None:
    assert summarize_path(f"{WORKTREE}/{relative}", WORKTREE) == shown


def test_a_masked_path_never_shows_the_token_through_the_transcript(
    tmp_path: Path,
) -> None:
    secret = "ghp_abc123DEF456ghi789jkl012"
    shown = _shown_for(tmp_path, "Read", {"file_path": f"{WORKTREE}/tokens/{secret}"})
    assert shown == "***/***"
    assert "ghp_" not in shown


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Read-only programs and subcommands.
        *[(c, Phase.READING) for c in (
            "cat a", "head -5 a", "tail -f a", "less a", "ls -la", "grep -rn x .",
            "rg x", "find . -name x", "wc -l a", "diff a b", "tree", "stat a",
            "file a", "sed -n '1,5p' a", "sed 's/a/b/' f",
            "git status", "git diff HEAD", "git log -3", "git show HEAD",
            "git blame a", "git branch", "gh pr view 3", "gh pr list",
            "gh issue view 3", "gh issue list", "/usr/bin/cat a",
        )],
        # Mutating ones.
        *[(c, Phase.CODING) for c in (
            "rm -rf build", "mv a b", "cp a b", "touch a", "mkdir -p d", "rmdir d",
            "chmod +x a", "ln -s a b", "echo x | tee a", "patch -p1 < d",
            "sed -i 's/a/b/' f", "sed -i.bak 's/a/b/' f", "sed -Ei 's/a/b/' f",
            "sed --in-place 's/a/b/' f",
            "git add a", "git commit -m x", "git checkout -b x", "git switch x",
            "git restore a", "git stash", "git rebase main", "git merge x",
            "git reset --hard", "git cherry-pick abc", "git apply d",
            "npm install", "pnpm add x", "yarn add x", "yarn install",
            "uv add x", "uv sync", "uv pip install x", "pip install x",
        )],
        # Anything else says nothing.
        *[(c, None) for c in (
            "echo hi", "curl https://example.invalid", "git branch -D x",
            "git fetch", "gh api repos/x", "python script.py", "make build",
            "jq . a", "npm run build", "uv pip list", "pip list", "cd /w",
        )],
        # A read never outranks what else the line did.
        ("uv run pytest -q 2>&1 | tail -20", Phase.TESTING),
        ("git push && git status", Phase.PR),
        ("rm a && ls", Phase.CODING),
        ("cat a && pytest", Phase.TESTING),
        ("pytest && rm -rf .cache", Phase.CODING),
        ("echo x && cat a", Phase.READING),
    ],
)
def test_classify_shell_segments(command: str, expected: Phase | None) -> None:
    assert classify("Bash", {"command": command}) is expected


def test_a_shell_command_that_says_nothing_keeps_the_last_phase(
    tmp_path: Path,
) -> None:
    d = _transcript_dir(tmp_path)
    rows = [
        _assistant_row("Edit", {"file_path": f"{WORKTREE}/src/app.py"},
                       "2026-10-31T03:00:00.000Z"),
        _assistant_row("Bash", {"command": "echo done"}, "2026-10-31T03:00:05.000Z"),
    ]
    (d / "t.jsonl").write_text("\n".join(rows) + "\n")
    assert read_activity(WORKTREE, home=tmp_path).phase is Phase.CODING

    # A test run, then only commands nobody listed: still testing. Nothing
    # after the run changes files, so only the keep-phase rule gives this.
    rows = [
        _assistant_row("Bash", {"command": "uv run pytest -q"},
                       "2026-10-31T03:01:00.000Z"),
        _assistant_row("Bash", {"command": "curl -s http://127.0.0.1:8787/"},
                       "2026-10-31T03:01:05.000Z"),
        _assistant_row("Bash", {"command": "make build"}, "2026-10-31T03:01:09.000Z"),
    ]
    (d / "t.jsonl").write_text("\n".join(rows) + "\n")
    assert read_activity(WORKTREE, home=tmp_path).phase is Phase.TESTING


# --- #238: CI waits are CI, and the furthest phase is kept -------------------


@pytest.mark.parametrize("command", [
    "ctrlrelay ci wait --pr 239",
    "uv run ctrlrelay ci wait --repo o/r --pr 239",
    "gh pr checks 239 --watch",
    "gh run watch 123",
    "gh run view 123 --log-failed",
    "gh pr view 239 --json statusCheckRollup",
    "gh pr view 239 --json state,statusCheckRollup --jq .state",
    "gh pr view --json=statusCheckRollup",
    "sleep 30 && gh pr checks 239",
    "gh pr checks 239 | tail -5",
])
def test_ci_watching_commands_are_ci(command: str) -> None:
    assert classify("Bash", {"command": command}) is Phase.CI


@pytest.mark.parametrize(("command", "expected"), [
    # Neighbours that are not CI waits keep what they had.
    ("gh pr view 239", Phase.READING),
    ("gh pr view 239 --json title,body", Phase.READING),
    ("gh run list", None),
    ("ctrlrelay ci", None),
    ("ctrlrelay status", None),
    ("echo statusCheckRollup", None),
])
def test_neighbours_of_ci_commands_are_not_ci(command: str, expected: Phase | None) -> None:
    assert classify("Bash", {"command": command}) is expected


@pytest.mark.parametrize(("command", "shown"), [
    ("ctrlrelay ci wait --pr 239", "ctrlrelay …"),
    ("gh pr checks 239 --watch", "gh pr …"),
    ("gh run watch 123", "gh run …"),
    ("gh pr view 239 --json statusCheckRollup", "gh pr …"),
])
def test_ci_commands_summary_is_still_the_allowlist(command: str, shown: str) -> None:
    assert summarize_command(command) == shown


def _after_pr_rows() -> list[str]:
    """The measured shape: the agent pushes and opens its PR, then waits on
    CI and reads files outside its worktree."""
    return [
        _assistant_row("Edit", {"file_path": f"{WORKTREE}/src/app.py"},
                       "2026-10-31T03:00:00.000Z"),
        _assistant_row("Bash", {"command": "uv run pytest -q"}, "2026-10-31T03:01:00.000Z"),
        _assistant_row("Bash", {"command": "git push -u origin fix/237"},
                       "2026-10-31T03:02:00.000Z"),
        _assistant_row("Bash", {"command": "gh pr create --fill"}, "2026-10-31T03:02:30.000Z"),
        _assistant_row("Bash", {"command": "ctrlrelay ci wait --pr 239"},
                       "2026-10-31T03:03:00.000Z"),
        _assistant_row("Read", {"file_path": "/home/u/notes/README.md"},
                       "2026-10-31T03:04:00.000Z"),
    ]


def test_reads_after_the_pr_keep_the_furthest_phase(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    (d / "t.jsonl").write_text("\n".join(_after_pr_rows()) + "\n")

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.furthest is Phase.PR
    assert activity.phase is Phase.READING
    assert activity.last_command == OUTSIDE_REPO


def test_furthest_is_the_latest_when_work_only_moves_forward(tmp_path: Path) -> None:
    """Control: with no step back, furthest and latest agree, so the field
    is not simply pinned to PR."""
    d = _transcript_dir(tmp_path)
    (d / "t.jsonl").write_text("\n".join(_after_pr_rows()[:2]) + "\n")

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.furthest is Phase.TESTING
    assert activity.phase is Phase.TESTING


def test_a_ci_wait_after_the_pr_is_the_current_step(tmp_path: Path) -> None:
    d = _transcript_dir(tmp_path)
    (d / "t.jsonl").write_text("\n".join(_after_pr_rows()[:5]) + "\n")

    activity = read_activity(WORKTREE, home=tmp_path)

    assert activity.phase is Phase.CI
    assert activity.furthest is Phase.PR
    assert activity.last_command == "ctrlrelay …"
