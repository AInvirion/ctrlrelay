"""The checkpoint snippets give an agent nothing to re-quote (#397).

The BLOCKED and FAILED snippets (and secops' DONE) were a ``printf`` whose
format string was several single-quoted pieces joined by backslash
continuations. Agents re-quoted it or dropped a continuation, and the
round ended with a malformed or missing ``state.json`` after the work was
done. The same ``%s`` also pasted the agent's text into the JSON
unescaped, so a question holding a double quote or a newline produced an
unreadable checkpoint even when the shell was copied exactly.

These tests run every text-carrying snippet the relay hands an agent —
each pipeline's contract and ``agent check``'s round 1 — under ``sh``
(dash on Debian and Ubuntu, not bash), with text chosen to break the old
shape, and read the file back.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from ctrlrelay.core import agent_check
from ctrlrelay.core.checkpoint import shell_checkpoint
from ctrlrelay.pipelines.dev import _blocked_and_failed_blocks
from tests.test_resume_prompt_contract import PIPELINES, _pipeline

# Text that broke the printf snippet, one hazard each:
# - a double quote closed the JSON string;
# - a newline put a raw control character in the JSON;
# - an apostrophe is what agents re-quote;
# - `$HOME` and a backtick would expand if the heredoc were unquoted;
# - `%s` would be a directive had an agent folded its text into the
#   format string, which is one of the ways the old snippet was rewritten;
# - a trailing backslash would escape the closing quotes of a raw string
#   written on one line.
HOSTILE_TEXT = (
    'Use "schema v2" or v1?\n'
    "It's $HOME and `id` — 100%s sure\n"
    "ends with a backslash \\"
)

_PLACEHOLDERS = ("<QUESTION>", "<ERROR>", "<SUMMARY>")


def _shell_blocks(prompt: str) -> list[str]:
    return [b.split("```")[0] for b in prompt.split("```bash")[1:]]


def _text_blocks(prompt: str) -> list[tuple[str, str]]:
    """(placeholder, block) for every block carrying a free-text placeholder."""
    out = []
    for block in _shell_blocks(prompt):
        hits = [p for p in _PLACEHOLDERS if p in block]
        if hits:
            assert len(hits) == 1, f"two placeholders in one block: {block}"
            out.append((hits[0], block))
    return out


def _run_sh(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=10, cwd=cwd
    )


_FIELD = {"<QUESTION>": "question", "<ERROR>": "error", "<SUMMARY>": "summary"}


@pytest.mark.parametrize("mod", PIPELINES)
def test_every_text_snippet_carries_hostile_text_exactly(
    mod: str, tmp_path: Path
) -> None:
    state_file = tmp_path / "it's here" / "state.json"
    contract = _pipeline(mod)._checkpoint_contract(str(state_file), "sid-1")
    blocks = _text_blocks(contract)
    # Every pipeline has at least BLOCKED and FAILED. Without this an
    # empty list would pass the loop below by doing nothing.
    assert {p for p, _ in blocks} >= {"<QUESTION>", "<ERROR>"}, mod

    for placeholder, block in blocks:
        if state_file.exists():
            state_file.unlink()
        run = _run_sh(block.replace(placeholder, HOSTILE_TEXT), tmp_path)
        assert run.returncode == 0, f"{mod}/{placeholder}: {run.stderr}"
        data = json.loads(state_file.read_text())
        assert data[_FIELD[placeholder]] == HOSTILE_TEXT, (mod, placeholder)
        assert data["session_id"] == "sid-1"


@pytest.mark.parametrize("mod", PIPELINES)
def test_no_snippet_has_a_line_continuation_or_printf(mod: str) -> None:
    """The shape agents mangled is gone from every block, DONE included.

    Read on the rendered contract, so a block written by hand later in
    the old shape fails here whatever it is called.
    """
    contract = _pipeline(mod)._checkpoint_contract("/wt/state.json", "sid-1")
    blocks = _shell_blocks(contract)
    assert len(blocks) == 3, mod
    for block in blocks:
        assert "printf" not in block, f"{mod}: {block}"
        for line in block.splitlines():
            assert not line.rstrip().endswith("\\"), f"{mod}: {line!r}"


def test_agent_check_round1_runs_the_pipelines_snippet() -> None:
    """``agent check`` uses the shell real runs use, byte for byte.

    A string comparison, not a run: running the snippet is covered by the
    tests either side of this one, for the dev block and the check's own.

    The dev contract's BLOCKED block with its placeholder filled by the
    check's fixed question must be exactly the check's round-1 block. If
    either side changes shape alone, the check would pass for a backend
    that cannot write a real run's checkpoint.
    """
    path = "/tmp/x y/state.json"
    sid = "sid-1"
    dev_blocked = _shell_blocks(
        _blocked_and_failed_blocks(
            shlex.quote(str(Path(path).parent)), shlex.quote(path), sid
        )
    )[0]
    check = _shell_blocks(
        agent_check.resume_round1_prompt(path, sid, "deadbeef")
    )
    assert len(check) == 1
    assert check[0] == dev_blocked.replace(
        "<QUESTION>", agent_check.RESUME_QUESTION
    )


def test_agent_check_round1_writes_its_blocked_state(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    (block,) = _shell_blocks(
        agent_check.resume_round1_prompt(str(state_file), "sid-1", "deadbeef")
    )
    run = _run_sh(block, tmp_path)
    assert run.returncode == 0, run.stderr
    data = json.loads(state_file.read_text())
    assert data["status"] == agent_check.ROUND1_EXPECTED
    assert data["question"] == agent_check.RESUME_QUESTION


def test_shell_checkpoint_needs_no_extra_quoting_in_the_shell(
    tmp_path: Path,
) -> None:
    """Only two shell commands: the mkdir and the heredoc'd python3.

    Everything else is inside a heredoc whose delimiter is quoted, so the
    shell expands nothing in it. Asserted by running it with a `$`-laden
    text and an env var of that name set to something else.
    """
    state_file = tmp_path / "d" / "s.json"
    block = shell_checkpoint(
        shlex.quote(str(state_file.parent)),
        shlex.quote(str(state_file)),
        session_id="sid-1",
        status="FAILED",
        field="error",
        text="$TRAP ${TRAP} $(echo no)",
    )
    run = subprocess.run(
        ["sh", "-c", block],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "TRAP": "expanded"},
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(state_file.read_text())["error"] == "$TRAP ${TRAP} $(echo no)"
