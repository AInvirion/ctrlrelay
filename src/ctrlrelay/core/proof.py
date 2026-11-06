"""Proof that a dev run did what it says (#221).

A run used to count as done when the agent wrote DONE and CI went green.
Green CI says the change did not break what was already tested; it does
not say the issue was fixed, and an agent can weaken or delete a test to
get there. Here the relay checks the evidence itself.

The agent names its proof in the DONE checkpoint::

    "proof": {"command": ["pytest", "tests/test_x.py::test_y"],
              "tests": ["tests/test_x.py::test_y"]}

and the relay runs that command twice, each time in a fresh copy of
committed content (never the agent's worktree, for the reason in
``WorktreeManager.make_review_copy``):

- on the pushed head of the branch, where it must pass;
- on the base commit with only the branch's changes to test files
  applied, where it must fail.

Three outcomes, never two. ``proven``: fails before, passes after.
``failed``: does not pass on the branch, passes on the base too (so it
proves nothing), the diff has no real change, or the diff weakens tests.
``not proven``: no proof named, or it could not run, timed out, or a tool
was missing. "Could not run" is never a pass. A fourth label, ``not
required``, is an exemption and says which one: a docs-only diff, or a
repository that switched proof off in its config.

The proof command is code the agent wrote. It never runs with more
access than the agent had: under the same kind of bwrap confinement when
the agent was sandboxed (with no network as well), and without the
operator's GitHub token either way.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from ctrlrelay.core.agent_env import (
    agent_cache_env,
    build_agent_env,
    bwrap_prefix,
    resolve_bwrap,
)
from ctrlrelay.core.redact import redact_secrets, tail

PROVEN = "proven"
FAILED = "failed"
NOT_PROVEN = "not proven"
NOT_REQUIRED = "not required"

# Each run of the proof command, before and after. The command names one
# test or a few, not the suite.
PROOF_TIMEOUT_SECONDS = 600
_TAIL_CHARS = 1500
_GIT_TIMEOUT_SECONDS = 120

# The marker the PR comment carries, so a re-run edits it in place.
PROOF_COMMENT_MARKER = "<!-- ctrlrelay:proof -->"

# Shell and exec conventions for "the command was not found / could not
# be executed". The test did not run, so nothing was shown either way.
_COULD_NOT_EXEC = (126, 127)

_PY_SUFFIXES = frozenset({".py"})
_JS_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"})
# Test files that are data, not code: there is nothing to weaken in them,
# so they do not make the scan "not checked". They still go to the base
# copy with the rest of the test changes.
_DATA_SUFFIXES = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".csv",
    ".xml", ".html", ".md", ".snap", ".lock", ".env", "",
})
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_NAME = re.compile(
    r"^(?:test_.+\.py|.+_test\.py|conftest\.py"
    r"|.+\.(?:test|spec)\.[cm]?[jt]sx?"
    r"|.+_test\.go|.+_spec\.rb|.+Tests?\.(?:java|kt|cs))$"
)
_DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".adoc"})

_PY_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_JS_TEST_DEF = re.compile(r"""\b(?:it|test)(?:\.\w+)?\s*\(\s*(['"`])(.+?)\1""")
_PY_SKIP = re.compile(
    # Decorators, ``pytestmark = pytest.mark.skip(...)`` and marks in a
    # list alike, and ``from pytest import mark``.
    r"\b(?:pytest\.)?mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|xfail|importorskip)\s*\("
    r"|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
    r"|\.skipTest\s*\("
)
_JS_SKIP = re.compile(
    r"\b(?:it|test|describe|context)\.(?:skip|only|todo)\s*\("
    r"|\bx(?:it|test|describe|context)\s*\("
    r"|\bf(?:it|describe)\s*\("
)
_PY_ASSERT = re.compile(r"^\s*assert\b|\bself\.assert\w*\s*\(|\bpytest\.raises\s*\(")
_JS_ASSERT = re.compile(r"\bexpect\s*\(|\bassert(?:\.\w+)?\s*\(")

# Files that tell a test runner what to collect, and the keys in them
# that can deselect or skip tests. An added line with one of these keys is
# a hit: the scan cannot tell a narrowing from a harmless edit.
_RUNNER_CONFIG_NAME = re.compile(
    r"^(?:pytest\.ini|pyproject\.toml|setup\.cfg|tox\.ini|conftest\.py"
    r"|package\.json|(?:jest|vitest)\.config\.[cm]?[jt]s(?:on)?)$"
)
_RUNNER_CONFIG_KEY = re.compile(
    r"\baddopts\b|--deselect\b|--ignore\b|(?:^|[\s'\"=\[,])-k(?:[\s=]|$)"
    r"|\btestpaths\b|\bnorecursedirs\b|\bcollect_ignore"
    r"|\btestPathIgnorePatterns\b|\btestMatch\b|\btestRegex\b"
)

# Lines in the plan's acceptance criteria that the gate reads (#221).
# Each needs a reason after the colon; the bare words are not enough.
_PLAN_ALLOWS_TEST_REMOVAL = re.compile(
    r"test removal intended:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE
)
_PLAN_NO_TEST_POSSIBLE = re.compile(
    r"no test possible:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE
)


# --- what the agent claims ---------------------------------------------------


@dataclass(frozen=True)
class ProofClaim:
    """The agent's named proof: the command, and the tests it runs."""

    command: tuple[str, ...]
    tests: tuple[str, ...]


def parse_proof_claim(raw: Any) -> ProofClaim | str:
    """``raw`` (the checkpoint's ``proof`` output) as a claim, or a
    one-line reason it is not one. A string command is split as a shell
    would split it; it is still run without a shell."""
    if raw is None:
        return "the agent named no proof"
    if not isinstance(raw, dict):
        return f"proof must be an object, got {type(raw).__name__}"
    command = raw.get("command")
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError as e:
            return f"proof command could not be read ({e})"
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) and a for a in command)
    ):
        return "proof command must be a non-empty list of strings"
    tests = raw.get("tests")
    if isinstance(tests, str):
        tests = [tests]
    if (
        not isinstance(tests, list)
        or not tests
        or not all(isinstance(t, str) and t.strip() for t in tests)
    ):
        return "proof must name at least one test in 'tests'"
    return ProofClaim(command=tuple(command), tests=tuple(t.strip() for t in tests))


def plan_allows_test_removal(criteria: str | None) -> str | None:
    """The reason, when the plan's acceptance criteria say a test removal
    is intended (``Test removal intended: <why>``)."""
    match = _PLAN_ALLOWS_TEST_REMOVAL.search(criteria or "")
    return match.group(1).strip() if match else None


def plan_says_no_test_possible(criteria: str | None) -> str | None:
    """The reason, when the plan's acceptance criteria say no test is
    possible (``No test possible: <why>``)."""
    match = _PLAN_NO_TEST_POSSIBLE.search(criteria or "")
    return match.group(1).strip() if match else None


# --- classifying paths -------------------------------------------------------


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(_TEST_NAME.match(p.name)) or any(
        part in _TEST_DIRS for part in p.parts[:-1]
    )


def is_runner_config(path: str) -> bool:
    return bool(_RUNNER_CONFIG_NAME.match(PurePosixPath(path).name))


def _collected(path: str) -> bool | None:
    """Whether the test runner collects ``path`` by its name: True or
    False for a Python or JS/TS file, None for any other."""
    p = PurePosixPath(path)
    lang = test_language(path)
    if lang == "python":
        return bool(re.match(r"^(?:test_.+|.+_test)\.py$", p.name))
    if lang == "js":
        return bool(re.search(r"\.(?:test|spec)\.", p.name)) or (
            "__tests__" in p.parts[:-1]
        )
    return None


def _runner_collects(path: str) -> bool:
    """Whether a test runner picks ``path`` up as tests, by its name. A
    name in a language the scan does not read counts when it follows a
    test naming convention (``*_test.go``, ``*_spec.rb``); anything else,
    such as ``test_x.txt`` or ``test_x.py.disabled``, is not collected."""
    collected = _collected(path)
    if collected is not None:
        return collected
    name = PurePosixPath(path).name
    return bool(_TEST_NAME.match(name)) and name != "conftest.py"


def test_language(path: str) -> str | None:
    """``python`` or ``js`` for test code the scan reads, else None."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _PY_SUFFIXES:
        return "python"
    if suffix in _JS_SUFFIXES:
        return "js"
    return None


def is_docs_file(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in _DOC_SUFFIXES or (
        len(p.parts) > 1 and p.parts[0] == "docs"
    )


# --- the test weakening scan -------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One line of ``git diff --name-status -M``: ``status`` is the first
    letter (A, M, D, R, ...), ``old`` is set for a rename."""

    status: str
    path: str
    old: str | None = None


@dataclass(frozen=True)
class WeakeningScan:
    # Blockers: any of these makes the run not done unless the plan says so.
    hits: tuple[str, ...] = ()
    # Shown on the PR, never a blocker: an assert line edited in place.
    warnings: tuple[str, ...] = ()
    # Test files in a language the scan cannot read: not "clean".
    unchecked: tuple[str, ...] = ()

    def describe(self) -> str:
        parts: list[str] = []
        if self.hits:
            parts.append(f"{len(self.hits)} hit(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        if self.unchecked:
            parts.append(
                "not checked for " + ", ".join(f"`{p}`" for p in self.unchecked)
            )
        return "; ".join(parts) if parts else "clean"


Hunk = tuple[list[str], list[str]]


def _diff_hunks_by_file(diff: str) -> dict[str, list[Hunk]]:
    """``{path: [(removed lines, added lines), ...]}``, one entry per hunk
    of a ``-U0`` diff."""
    out: dict[str, list[Hunk]] = {}
    old = new = None
    hunks: list[Hunk] | None = None
    current: Hunk | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            old = new = None
            hunks = current = None
        elif hunks is None and line.startswith("--- "):
            old = line[4:]
            old = old[2:] if old.startswith("a/") else None
        elif hunks is None and line.startswith("+++ "):
            new = line[4:]
            new = new[2:] if new.startswith("b/") else None
            key = new or old
            if key is not None:
                hunks = out.setdefault(key, [])
        elif hunks is not None and line.startswith("@@"):
            current = ([], [])
            hunks.append(current)
        elif current is not None and line.startswith("-"):
            current[0].append(line[1:])
        elif current is not None and line.startswith("+"):
            current[1].append(line[1:])
    return out


def early_returns(base_src: str, head_src: str) -> list[str]:
    """Test functions that existed on the base and on the head begin with
    a bare ``return`` (after any docstring), so they pass without testing
    anything. Python only; a file that does not parse gives nothing."""
    import ast

    def starts(src: str) -> dict[str, bool]:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return {}
        out: dict[str, bool] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name.startswith("test")
            ):
                body = list(node.body)
                if (
                    body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body = body[1:]
                out[node.name] = bool(
                    body and isinstance(body[0], ast.Return)
                    and body[0].value is None
                )
        return out

    before = starts(base_src)
    return sorted(
        name for name, bare in starts(head_src).items()
        if bare and before.get(name) is False
    )


def scan_weakening(
    changes: Sequence[Change],
    diff: str,
    *,
    sources: dict[str, tuple[str, str]] | None = None,
) -> WeakeningScan:
    """Look for weakened tests in a diff.

    Blockers: a deleted test file (any language), a test file renamed to a
    name the runner does not collect or moved out of the tests, a removed
    test function, an added skip/xfail/only marker or ``importorskip``, an
    added runner-config line that can deselect or skip, an early bare
    ``return`` added to an existing Python test, and an assert line removed
    from a hunk that adds no assert.

    Lines are matched by their text across all the test files, so a test
    or an assert moved from one file to another is not a hit. An assert
    line removed from a hunk that also adds an assert is an edit: the scan
    cannot tell a corrected expectation from a loosened one, so it is a
    warning on the PR, not a blocker.

    ``sources`` maps a modified Python test file to its ``(base, head)``
    text, for the early-return check.
    """
    hits: list[str] = []
    warnings: list[str] = []
    unchecked: list[str] = []
    files = _diff_hunks_by_file(diff)
    # (kind, text) -> [(path, hunk index)], one entry per line, each side.
    removed_items: dict[tuple[str, str], list[tuple[str, int]]] = {}
    added_items: dict[tuple[str, str], list[tuple[str, int]]] = {}
    # (path, hunk index) -> asserts the hunk adds.
    hunk_adds: dict[tuple[str, int], int] = {}

    def note(side: dict, kind: str, text: str, where: tuple[str, int]) -> None:
        side.setdefault((kind, " ".join(text.split())), []).append(where)

    for change in changes:
        path = change.path
        old = change.old
        if (
            change.status == "D" and is_test_file(path)
            and PurePosixPath(path).suffix.lower() not in _DATA_SUFFIXES
        ):
            hits.append(f"deleted test file `{path}`")
            continue
        if change.status == "R" and old and is_test_file(old):
            if not is_test_file(path):
                hits.append(f"moved test file `{old}` out of the tests")
                continue
            if _runner_collects(old) and not _runner_collects(path):
                hits.append(
                    f"renamed test file `{old}` to `{path}`, which the test "
                    "runner does not collect"
                )
                continue
        hunks = files.get(path, [])
        if is_runner_config(path):
            for removed_lines, added_lines in hunks:
                for text in added_lines:
                    if _RUNNER_CONFIG_KEY.search(text):
                        hits.append(
                            f"runner config in `{path}` can deselect or skip "
                            f"tests: `{text.strip()[:120]}`"
                        )
        if not is_test_file(path):
            continue
        lang = test_language(path)
        if lang is None:
            if PurePosixPath(path).suffix.lower() not in _DATA_SUFFIXES:
                unchecked.append(path)
            continue
        def_re, group = (
            (_PY_TEST_DEF, 1) if lang == "python" else (_JS_TEST_DEF, 2)
        )
        skip_re = _PY_SKIP if lang == "python" else _JS_SKIP
        assert_re = _PY_ASSERT if lang == "python" else _JS_ASSERT
        for i, (removed, added) in enumerate(hunks):
            where = (path, i)
            hunk_adds[where] = sum(1 for t in added if assert_re.search(t))
            for side, texts in ((removed_items, removed), (added_items, added)):
                for text in texts:
                    m = def_re.search(text)
                    if m:
                        note(side, "test", m.group(group), where)
                    if skip_re.search(text):
                        note(side, "skip", text, where)
                    if assert_re.search(text):
                        note(side, "assert", text, where)
        if lang == "python" and sources and path in sources:
            base_src, head_src = sources[path]
            for name in early_returns(base_src, head_src):
                hits.append(
                    f"added an early `return` to `{name}` in `{path}`, so it "
                    "tests nothing"
                )

    def surplus(more: dict, fewer: dict, kind: str) -> list[tuple[str, str, int]]:
        out: list[tuple[str, str, int]] = []
        for (k, text), places in more.items():
            if k != kind:
                continue
            extra = len(places) - len(fewer.get((k, text), []))
            out += [(text, p, i) for p, i in places[:max(extra, 0)]]
        return out

    for name, path, _ in surplus(removed_items, added_items, "test"):
        hits.append(f"removed test `{name}` from `{path}`")
    for text, path, _ in surplus(added_items, removed_items, "skip"):
        hits.append(f"added skip/xfail/only marker in `{path}`: `{text[:120]}`")
    for text, path, i in surplus(removed_items, added_items, "assert"):
        if hunk_adds.get((path, i), 0) > 0:
            hunk_adds[(path, i)] -= 1
            warnings.append(f"edited assert line in `{path}`: `{text[:120]}`")
        else:
            hits.append(f"removed assert line in `{path}`: `{text[:120]}`")
    return WeakeningScan(
        hits=tuple(hits), warnings=tuple(warnings), unchecked=tuple(unchecked),
    )


# --- running the command -----------------------------------------------------


@dataclass(frozen=True)
class CommandRun:
    """One run of the proof command."""

    # From the exit code alone (run_proof_command): "passed", "failed",
    # "timed out" or "could not run". Once judged against the named tests
    # (run_claim): "passed" and "failed" mean the named tests ran and did
    # or did not pass, and "not shown" means there is no evidence they ran.
    status: str
    exit_code: int | None = None
    # Redacted and capped.
    output: str = ""
    detail: str = ""
    # The whole output, for finding the named tests in it. Never shown.
    full_output: str = field(default="", repr=False, compare=False)

    def describe(self) -> str:
        if self.status == "timed out":
            return f"timed out ({self.detail})" if self.detail else "timed out"
        if self.status in ("could not run", "not shown"):
            label = (
                "could not run" if self.status == "could not run"
                else "no evidence the named test ran"
            )
            code = f", exit {self.exit_code}" if self.exit_code is not None else ""
            return f"{label} ({self.detail}{code})"
        text = f"{self.status} (exit {self.exit_code})"
        return f"{text}: {self.detail}" if self.detail else text


@dataclass(frozen=True)
class Confinement:
    """How the proof command runs: the mode the agent ran in, and for
    ``bwrap`` what to hide and which cache the agent used."""

    mode: str  # "none" or "bwrap"
    bwrap: str | None = None
    hide_paths: tuple[str, ...] = ()
    cache_dir: Path | None = None

    def prefix(self, cwd: Path, extra_writable: Sequence[Path] = ()) -> list[str]:
        if self.mode != "bwrap":
            # The agent ran unconfined: it already executed arbitrary code
            # as this user, with network. Running its test command the
            # same way adds no exposure it did not already have. The
            # GitHub token is still stripped from the environment.
            return []
        assert self.bwrap is not None
        writable: list[str | Path] = [cwd, *extra_writable]
        if self.cache_dir is not None:
            writable.append(self.cache_dir)
        argv = bwrap_prefix(self.bwrap, self.hide_paths, writable=writable)
        # The same read-only copy of the machine the agent got, minus the
        # network: a test needs none, and the agent's own sandbox kept it
        # only for the model's API.
        assert argv[-1] == "--"
        return [*argv[:-1], "--unshare-net", "--chdir", str(cwd), "--"]

    def describe(self) -> str:
        return "bwrap, no network" if self.mode == "bwrap" else "unsandboxed"


def confinement_for(dispatcher: Any, repo: str) -> Confinement:
    """The confinement the agent ran this repo's session under. Raises
    ``ValueError`` when it cannot be told: a proof is then not run."""
    from ctrlrelay.core.dispatcher import RepoAgentRouter

    backend = dispatcher
    if isinstance(dispatcher, RepoAgentRouter):
        # Each repo may run on its own backend (#286).
        backend = dispatcher._backend(repo)
    mode = getattr(backend, "sandbox", None)
    if mode == "none":
        return Confinement(mode="none")
    if mode != "bwrap":
        raise ValueError(
            f"could not tell how the agent was confined (sandbox={mode!r})"
        )
    hide = tuple(getattr(backend, "sandbox_hide_paths", ()) or ())
    raw_cache = getattr(backend, "sandbox_cache_dir", None)
    cache = None
    if isinstance(raw_cache, str) and raw_cache:
        expanded = os.path.expandvars(os.path.expanduser(raw_cache))
        if "$" not in expanded:
            cache = Path(expanded)
            cache.mkdir(parents=True, exist_ok=True)
    return Confinement(
        mode="bwrap", bwrap=resolve_bwrap(), hide_paths=hide, cache_dir=cache,
    )


async def run_proof_command(
    command: Sequence[str],
    cwd: Path,
    confinement: Confinement,
    *,
    timeout: float = PROOF_TIMEOUT_SECONDS,
    writable: Sequence[Path] = (),
) -> CommandRun:
    """Run ``command`` in ``cwd``, bounded by ``timeout``, without the
    operator's GitHub token, under ``confinement``. ``writable`` names
    other directories the sandbox lets it write."""
    private = Path(tempfile.mkdtemp(prefix="ctrlrelay-proof-"))
    try:
        (private / "gh").mkdir()
        (private / "run").mkdir(mode=0o700)
        env, _ = build_agent_env(
            os.environ, gh_config_dir=private / "gh", runtime_dir=private / "run",
        )
        if confinement.mode == "bwrap" and confinement.cache_dir is not None:
            env.update(agent_cache_env(confinement.cache_dir))
        try:
            argv = [*confinement.prefix(cwd, writable), *command]
        except Exception as e:
            return CommandRun(
                status="could not run",
                detail=f"no sandbox: {type(e).__name__}: {str(e)[:200]}",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            return CommandRun(
                status="could not run", detail=f"{type(e).__name__}: {e}"[:200],
            )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
            return CommandRun(status="timed out", detail=f"after {timeout:g}s")
        full = stdout.decode(errors="replace")
        output = tail(redact_secrets(full), _TAIL_CHARS)
        code = proc.returncode
        raw = full.lstrip()
        if confinement.mode == "bwrap" and code != 0 and raw.startswith("bwrap:"):
            # The sandbox itself did not start: the test never ran.
            return CommandRun(
                status="could not run", exit_code=code, output=output,
                detail="the sandbox did not start",
            )
        if code in _COULD_NOT_EXEC:
            return CommandRun(
                status="could not run", exit_code=code, output=output,
                detail=f"exit {code}: command not found or not executable",
            )
        return CommandRun(
            status="passed" if code == 0 else "failed",
            exit_code=code, output=output, full_output=full,
        )
    finally:
        shutil.rmtree(private, ignore_errors=True)


# --- did the named tests run? ------------------------------------------------

# pytest's own exit codes past 1: interrupted (collection errors), internal
# error, usage error, no tests collected. ``uv run`` also exits 2 when it
# cannot start the program. None of them shows a named test ran.
_PYTEST_NOT_RUN = {
    2: "interrupted, often a collection error, or the runner could not start",
    3: "internal error",
    4: "usage error",
    5: "no tests collected",
}


def is_pytest_command(command: Sequence[str]) -> bool:
    """``pytest ...``, ``uv run pytest ...``, ``python -m pytest ...``."""
    for i, arg in enumerate(command):
        name = PurePosixPath(arg).name
        if name in ("pytest", "py.test"):
            return True
        if arg == "-m" and i + 1 < len(command) and command[i + 1] == "pytest":
            return True
    return False


def _junit_cases(xml_text: str) -> list[tuple[str, str, str]]:
    """``(classname, name, result)`` per testcase; result is ``passed``,
    ``failed`` (failure or error) or ``skipped``."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    out: list[tuple[str, str, str]] = []
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        result = (
            "failed" if tags & {"failure", "error"}
            else "skipped" if "skipped" in tags
            else "passed"
        )
        out.append((case.get("classname", ""), case.get("name", ""), result))
    return out


def _matching_cases(
    test_id: str, cases: list[tuple[str, str, str]],
) -> list[str]:
    """Results of the junit cases a pytest node id names: the file part
    as a dotted module, any class parts, and the function (all its
    parametrized cases when no ``[...]`` is given). A file alone names
    every case in it."""
    parts = test_id.split("::")
    module = parts[0].removesuffix(".py").replace("/", ".")
    if len(parts) == 1:
        return [
            r for c, _, r in cases if c == module or c.startswith(module + ".")
        ]
    classname = ".".join([module, *parts[1:-1]])
    func = parts[-1]
    return [
        r for c, n, r in cases
        if c == classname
        and (n == func or ("[" not in func and n.startswith(func + "[")))
    ]


def _module_of(test_id: str) -> str:
    return test_id.split("::", 1)[0].removesuffix(".py").replace("/", ".")


def _collection_errors_in_named_files(
    run: CommandRun, claim: ProofClaim, junit: str | None,
) -> list[str]:
    """The named tests' files that pytest could not collect, read from the
    junit report, when every named test is in one of them. Empty when there
    is no report, when pytest did not end as a collection failure (exit 2,
    or 4 for a node id), or when any named test is outside those files:
    then the collection errors say nothing about the named tests."""
    if junit is None or run.exit_code not in (2, 4):
        return []
    try:
        cases = _junit_cases(junit)
    except Exception:
        return []
    broken = {name for classname, name, result in cases
              if classname == "" and result == "failed"}
    files: list[str] = []
    for test in claim.tests:
        if _module_of(test) not in broken:
            return []
        path = test.split("::", 1)[0]
        if path not in files:
            files.append(path)
    return files


def judge(
    run: CommandRun, claim: ProofClaim, *, before: bool,
    junit: str | None, pytest_run: bool, junit_problem: str | None = None,
) -> CommandRun:
    """``run`` judged against the named tests.

    pytest: decided from its junit report. Exit 2-5, no report, or a named
    test missing from it is "not shown". On the branch each named test must
    have passed; on the base each must have failed or errored. Anything
    else other runners: the exit code (1 on the base, 0 on the branch) and
    each named test, the part after the last ``::``, in the output.
    """
    if run.status in ("timed out", "could not run"):
        return run

    def not_shown(why: str) -> CommandRun:
        return replace(run, status="not shown", detail=why)

    code = run.exit_code
    if pytest_run:
        if before:
            failed_import = _collection_errors_in_named_files(
                run, claim, junit,
            )
            if failed_import:
                # A test the branch added that imports code only the branch
                # has: on the base its module cannot be imported, which is
                # the test failing before. pytest exits 2 for a file, or 4
                # for a node id whose module did not collect, so the junit
                # report decides, not the exit code.
                return replace(
                    run, status="failed",
                    detail=(
                        "fails before: could not import on base (collection "
                        "error in " + ", ".join(f"`{f}`" for f in failed_import)
                        + ")"
                    ),
                )
        if code in _PYTEST_NOT_RUN:
            return not_shown(f"pytest {_PYTEST_NOT_RUN[code]}")
        if code not in (0, 1):
            return not_shown("unexpected exit code")
        if junit is None:
            return not_shown(junit_problem or "pytest wrote no junit report")
        try:
            cases = _junit_cases(junit)
        except Exception as e:
            return not_shown(f"the junit report could not be read ({type(e).__name__})")
        results: list[str] = []
        for test in claim.tests:
            found = _matching_cases(test, cases)
            if not found:
                return not_shown(f"`{test}` is not in the junit report")
            if "skipped" in found:
                return not_shown(f"`{test}` was skipped")
            results += found
        if before:
            if all(r == "failed" for r in results):
                return replace(run, status="failed", detail="the named tests failed")
            return replace(run, status="passed", detail="a named test passed")
        if all(r == "passed" for r in results):
            return replace(run, status="passed", detail="the named tests passed")
        return replace(run, status="failed", detail="a named test did not pass")
    missing = [
        t for t in claim.tests
        if t.rsplit("::", 1)[-1] not in run.full_output
    ]
    if missing:
        return not_shown(
            "not in the output: " + ", ".join(f"`{t}`" for t in missing)
        )
    if before and code == 1:
        return replace(run, status="failed")
    if not before and code == 0:
        return replace(run, status="passed")
    return not_shown(f"expected exit {1 if before else 0}")


# The junit report is written by code the agent controls. It is read
# only if it is a regular file (not a link, not a FIFO), no bigger than
# this, and the read happens off the event loop with a time limit.
JUNIT_MAX_BYTES = 5 * 1024 * 1024
JUNIT_READ_TIMEOUT_SECONDS = 30.0


def _read_report_blocking(path: Path, limit: int) -> str:
    import stat

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ValueError("pytest wrote no junit report") from None
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("the junit report is not a regular file")
    if st.st_size > limit:
        raise ValueError(f"the junit report is over {limit} bytes")
    # O_NOFOLLOW and O_NONBLOCK: a file swapped for a link or a FIFO after
    # the check is refused or opens without waiting, and fstat sees it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("the junit report is not a regular file")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"the junit report is over {limit} bytes")
    return data.decode(errors="replace")


async def read_junit_report(
    path: Path, *, limit: int = JUNIT_MAX_BYTES,
    timeout: float = JUNIT_READ_TIMEOUT_SECONDS,
) -> tuple[str | None, str | None]:
    """``(text, None)``, or ``(None, why it was not read)``."""
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_read_report_blocking, path, limit), timeout,
        )
    except ValueError as e:
        return None, str(e)
    except asyncio.TimeoutError:
        return None, "the junit report could not be read in time"
    except OSError as e:
        return None, f"the junit report could not be read ({type(e).__name__})"
    return text, None


async def run_claim(
    claim: ProofClaim, cwd: Path, confinement: Confinement, *,
    before: bool, timeout: float = PROOF_TIMEOUT_SECONDS,
) -> CommandRun:
    """Run the claim's command in ``cwd`` and judge whether the named tests
    ran and how they came out. A pytest command gets ``--junitxml`` in a
    directory of the relay's own, beside ``cwd`` rather than in it, which
    the sandbox lets it write."""
    pytest_run = is_pytest_command(claim.command)
    command = list(claim.command)
    report_dir = Path(tempfile.mkdtemp(prefix="ctrlrelay-junit-", dir=cwd.parent))
    try:
        report = report_dir / "report.xml"
        if pytest_run:
            command.append(f"--junitxml={report}")
        run = await run_proof_command(
            command, cwd, confinement, timeout=timeout,
            writable=(report_dir,) if pytest_run else (),
        )
        junit = problem = None
        if pytest_run:
            junit, problem = await read_junit_report(report)
        return judge(
            run, claim, before=before, junit=junit, pytest_run=pytest_run,
            junit_problem=problem,
        )
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)


# --- the evidence ------------------------------------------------------------


@dataclass(frozen=True)
class ProofReport:
    outcome: str
    reason: str
    claim: ProofClaim | None = None
    before: CommandRun | None = None
    after: CommandRun | None = None
    scan: WeakeningScan | None = None
    # The plan's reason, when it allows the scan's hits.
    weakening_allowed: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    confinement: str | None = None

    @property
    def passed(self) -> bool:
        """Only ``proven`` and an exemption let the PR go ready."""
        return self.outcome in (PROVEN, NOT_REQUIRED)


_GIT_FLAGS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "core.quotePath=false")


async def _git(cwd: Path, *args: str, ok: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """Git in a copy made by ``make_review_copy``, with the same guards it
    uses. Only ever before the agent's command has run in that copy."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "git", *_GIT_FLAGS, "-C", str(cwd), *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _GIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"git {args[0]} timed out") from None
    if proc.returncode not in ok:
        raise RuntimeError(
            f"git {args[0]} exited {proc.returncode}: "
            f"{err.decode(errors='replace').strip()[:200]}"
        )
    return proc.returncode, out.decode(errors="replace")


def _parse_name_status(text: str) -> list[Change]:
    changes: list[Change] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        status = parts[0][0]
        if status in ("R", "C") and len(parts) >= 3:
            changes.append(Change(status=status, path=parts[2], old=parts[1]))
        else:
            changes.append(Change(status=status, path=parts[1]))
    return changes


def _test_file_ops(changes: Sequence[Change]) -> tuple[list[str], list[str]]:
    """``(paths to take from the head, paths to remove)`` for the base
    copy: the branch's changes to test files, and nothing else."""
    take: list[str] = []
    remove: list[str] = []
    for c in changes:
        if c.status in ("R", "C") and c.old:
            if c.status == "R" and is_test_file(c.old):
                remove.append(c.old)
            if is_test_file(c.path):
                take.append(c.path)
        elif is_test_file(c.path):
            (remove if c.status == "D" else take).append(c.path)
    return take, remove


async def prove(
    *,
    worktree: Any,
    repo: str,
    head_sha: str,
    base_branch: str,
    base_sha: str,
    claim_raw: Any,
    plan_criteria: str | None,
    confinement: Confinement,
    timeout: float = PROOF_TIMEOUT_SECONDS,
) -> ProofReport:
    """Check the evidence for ``head_sha`` against ``base_sha`` (the base
    branch's head on GitHub; the diff is taken from its merge base with
    the head). Raises only for a fault of the relay's own (a copy that
    could not be made); every outcome about the change is a report."""
    copies: list[Path] = []
    common: dict[str, Any] = {
        "head_sha": head_sha, "confinement": confinement.describe(),
    }
    try:
        head_copy = await worktree.make_review_copy(
            repo, head_sha, base_branch=base_branch, base_sha=base_sha,
        )
        copies.append(head_copy)
        _, mb = await _git(head_copy, "merge-base", base_sha, head_sha)
        merge_base = mb.strip()
        common["base_sha"] = merge_base
        _, ns = await _git(
            head_copy, "diff", "--name-status", "-M", "--no-ext-diff",
            merge_base, head_sha,
        )
        changes = _parse_name_status(ns)
        code, _ = await _git(
            head_copy, "diff", "--quiet", "--no-ext-diff", "-w",
            "--ignore-blank-lines", merge_base, head_sha, ok=(0, 1),
        )
        if not changes or code == 0:
            return ProofReport(
                outcome=FAILED,
                reason=(
                    "no real change: the diff against the base is empty or "
                    "whitespace only"
                ),
                **common,
            )
        scanned = sorted({
            c.path for c in changes
            if is_test_file(c.path) or is_runner_config(c.path)
        } | {
            c.old for c in changes
            if c.old and c.status == "R" and is_test_file(c.old)
        })
        diff = ""
        if scanned:
            _, diff = await _git(
                head_copy, "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
                merge_base, head_sha, "--", *scanned,
            )
        sources: dict[str, tuple[str, str]] = {}
        for c in changes:
            if c.status == "M" and is_test_file(c.path) and (
                test_language(c.path) == "python"
            ):
                _, base_src = await _git(
                    head_copy, "show", f"{merge_base}:{c.path}",
                )
                _, head_src = await _git(head_copy, "show", f"{head_sha}:{c.path}")
                sources[c.path] = (base_src, head_src)
        scan = scan_weakening(changes, diff, sources=sources)
        common["scan"] = scan
        allowed = plan_allows_test_removal(plan_criteria) if scan.hits else None
        common["weakening_allowed"] = allowed
        if scan.hits and allowed is None:
            return ProofReport(
                outcome=FAILED,
                reason=(
                    "the change weakens tests, and the plan does not say "
                    "that is intended"
                ),
                **common,
            )
        if all(is_docs_file(c.path) for c in changes) and all(
            c.old is None or is_docs_file(c.old) for c in changes
        ):
            return ProofReport(
                outcome=NOT_REQUIRED, reason="not required: docs only", **common,
            )
        claim = parse_proof_claim(claim_raw)
        if isinstance(claim, str):
            why = plan_says_no_test_possible(plan_criteria)
            if why is not None and claim_raw is None:
                return ProofReport(
                    outcome=NOT_PROVEN,
                    reason=(
                        "held for a human: the plan says no test is "
                        f"possible ({why})"
                    ),
                    **common,
                )
            return ProofReport(outcome=NOT_PROVEN, reason=claim, **common)
        common["claim"] = claim
        outside = [
            t for t in claim.tests if not is_test_file(t.split("::", 1)[0])
        ]
        if outside:
            # The base copy has the branch's test files and nothing else
            # from it: a test that lives anywhere else is missing there,
            # and "fails before" would show only that it is missing.
            return ProofReport(
                outcome=NOT_PROVEN,
                reason=(
                    "the proof names tests that are not in test files: "
                    + ", ".join(f"`{t}`" for t in outside)
                ),
                **common,
            )

        after = await run_claim(
            claim, head_copy, confinement, before=False, timeout=timeout,
        )
        common["after"] = after
        if after.status in ("timed out", "could not run", "not shown"):
            return ProofReport(
                outcome=NOT_PROVEN,
                reason=f"on the branch, the proof command {after.describe()}",
                **common,
            )
        if after.status != "passed":
            # The named tests ran on the branch and did not pass.
            return ProofReport(
                outcome=FAILED,
                reason=f"the proof does not pass on the branch: {after.describe()}",
                **common,
            )

        base_copy = await worktree.make_review_copy(
            repo, head_sha, base_branch=base_branch, base_sha=base_sha,
        )
        copies.append(base_copy)
        await _git(base_copy, "checkout", "--quiet", "--detach", merge_base)
        take, remove = _test_file_ops(changes)
        if take:
            await _git(base_copy, "checkout", head_sha, "--", *take)
        for path in remove:
            await _git(base_copy, "rm", "-q", "--ignore-unmatch", "--", path)
        before = await run_claim(
            claim, base_copy, confinement, before=True, timeout=timeout,
        )
        common["before"] = before
        if before.status in ("timed out", "could not run", "not shown"):
            return ProofReport(
                outcome=NOT_PROVEN,
                reason=f"on the base, the proof command {before.describe()}",
                **common,
            )
        if before.status == "passed":
            return ProofReport(
                outcome=FAILED,
                reason=(
                    "the proof passes on the base too, so it does not show "
                    "the change did anything"
                ),
                **common,
            )
        return ProofReport(
            outcome=PROVEN,
            reason="fails on the base, passes on the branch",
            **common,
        )
    finally:
        for copy in copies:
            try:
                await worktree.remove_review_copy(copy)
            except Exception:
                shutil.rmtree(Path(copy).parent, ignore_errors=True)


# --- what the PR shows -------------------------------------------------------


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text.rstrip()}\n{ticks}"


def _run_block(label: str, run: CommandRun | None) -> list[str]:
    if run is None:
        return [f"- **{label}:** not run"]
    lines = [f"- **{label}:** {run.describe()}"]
    output = redact_secrets(run.output)
    if output.strip():
        lines += [
            "",
            "  <details><summary>output (tail)</summary>",
            "",
            _fence(output),
            "",
            "  </details>",
        ]
    return lines


def proof_comment(report: ProofReport) -> str:
    """The PR comment for ``report``. Every agent-written string in it has
    been redacted (output) or is the agent's own claim, shown as code."""
    lines = [
        f"### Proof of done: {report.outcome}",
        "",
        redact_secrets(report.reason),
        "",
    ]
    if report.head_sha:
        lines.append(f"- **Head:** `{report.head_sha}`")
    if report.base_sha:
        lines.append(f"- **Base (merge base):** `{report.base_sha}`")
    if report.claim is not None:
        tests = ", ".join(f"`{redact_secrets(t)}`" for t in report.claim.tests)
        lines.append(f"- **Proving test:** {tests}")
        lines.append(
            "- **Command:** `" + redact_secrets(shlex.join(report.claim.command))
            .replace("`", "'") + "`"
        )
    if report.confinement:
        lines.append(f"- **Ran:** {report.confinement}")
    if report.after is not None or report.before is not None:
        lines += _run_block("Pass after (branch head)", report.after)
        lines += _run_block(
            "Fail before (base with only the branch's test changes)",
            report.before,
        )
    if report.scan is not None:
        lines.append(f"- **Test weakening scan:** {report.scan.describe()}")
        for hit in report.scan.hits:
            lines.append(f"  - {hit}")
        for warning in report.scan.warnings:
            lines.append(f"  - warning: {warning}")
        if report.weakening_allowed:
            lines.append(
                "  - allowed by the plan: "
                + redact_secrets(report.weakening_allowed)
            )
    if not report.passed:
        lines += [
            "",
            "**This PR stays a draft.** The relay does not hand it over as "
            "ready without proof.",
        ]
    lines += ["", PROOF_COMMENT_MARKER]
    return "\n".join(lines)
