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
from dataclasses import dataclass
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
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|xfail)\s*\("
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
    hits: tuple[str, ...] = ()
    # Test files in a language the scan cannot read: not "clean".
    unchecked: tuple[str, ...] = ()

    def describe(self) -> str:
        parts: list[str] = []
        if self.hits:
            parts.append(f"{len(self.hits)} hit(s)")
        if self.unchecked:
            parts.append(
                "not checked for " + ", ".join(f"`{p}`" for p in self.unchecked)
            )
        return "; ".join(parts) if parts else "clean"


def _diff_lines_by_file(diff: str) -> dict[str, tuple[list[str], list[str]]]:
    """``{path: (removed lines, added lines)}`` from a ``-U0`` diff."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    old = new = None
    current: tuple[list[str], list[str]] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            old = new = None
            current = None
        elif line.startswith("--- "):
            old = line[4:]
            old = old[2:] if old.startswith("a/") else None
        elif line.startswith("+++ "):
            new = line[4:]
            new = new[2:] if new.startswith("b/") else None
            key = new or old
            if key is not None:
                current = out.setdefault(key, ([], []))
        elif current is not None and line.startswith("-"):
            current[0].append(line[1:])
        elif current is not None and line.startswith("+"):
            current[1].append(line[1:])
    return out


def scan_weakening(changes: Sequence[Change], diff: str) -> WeakeningScan:
    """Deleted test files and test functions, added skip/xfail/only
    markers, and removed assert lines, in the test files of a diff.

    Lines are matched by their text across all the test files, so a test
    or an assert moved from one file to another is not a hit. Anything
    else is: an assert line whose text changed counts as removed, because
    the scan cannot tell a corrected expectation from a loosened one. The
    plan says so when it is intended (``Test removal intended: <why>``).
    """
    hits: list[str] = []
    unchecked: list[str] = []
    lines = _diff_lines_by_file(diff)
    # (kind, text) -> paths, one entry per line, for each side.
    removed_items: dict[tuple[str, str], list[str]] = {}
    added_items: dict[tuple[str, str], list[str]] = {}

    def note(side: dict, kind: str, text: str, path: str) -> None:
        side.setdefault((kind, " ".join(text.split())), []).append(path)

    for change in changes:
        path = change.path
        if (
            change.status == "R" and change.old
            and is_test_file(change.old) and not is_test_file(path)
        ):
            hits.append(f"moved test file `{change.old}` out of the tests")
            continue
        if not is_test_file(path):
            continue
        lang = test_language(path)
        if lang is None:
            if PurePosixPath(path).suffix.lower() not in _DATA_SUFFIXES:
                unchecked.append(path)
            continue
        if change.status == "D":
            hits.append(f"deleted test file `{path}`")
            continue
        removed, added = lines.get(path, ([], []))
        def_re, group = (
            (_PY_TEST_DEF, 1) if lang == "python" else (_JS_TEST_DEF, 2)
        )
        skip_re = _PY_SKIP if lang == "python" else _JS_SKIP
        assert_re = _PY_ASSERT if lang == "python" else _JS_ASSERT
        for side, texts in ((removed_items, removed), (added_items, added)):
            for text in texts:
                m = def_re.search(text)
                if m:
                    note(side, "test", m.group(group), path)
                if skip_re.search(text):
                    note(side, "skip", text, path)
                if assert_re.search(text):
                    note(side, "assert", text, path)

    def surplus(more: dict, fewer: dict, kind: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for (k, text), paths in more.items():
            if k != kind:
                continue
            extra = len(paths) - len(fewer.get((k, text), []))
            out += [(text, p) for p in paths[:max(extra, 0)]]
        return out

    for name, path in surplus(removed_items, added_items, "test"):
        hits.append(f"removed test `{name}` from `{path}`")
    for text, path in surplus(added_items, removed_items, "skip"):
        hits.append(f"added skip/xfail/only marker in `{path}`: `{text[:120]}`")
    for text, path in surplus(removed_items, added_items, "assert"):
        hits.append(f"removed assert line in `{path}`: `{text[:120]}`")
    return WeakeningScan(hits=tuple(hits), unchecked=tuple(unchecked))


# --- running the command -----------------------------------------------------


@dataclass(frozen=True)
class CommandRun:
    """One run of the proof command."""

    # "passed", "failed", "timed out" or "could not run".
    status: str
    exit_code: int | None = None
    # Redacted and capped.
    output: str = ""
    detail: str = ""

    def describe(self) -> str:
        if self.status == "timed out":
            return f"timed out ({self.detail})" if self.detail else "timed out"
        if self.status == "could not run":
            return f"could not run ({self.detail})"
        return f"{self.status} (exit {self.exit_code})"


@dataclass(frozen=True)
class Confinement:
    """How the proof command runs: the mode the agent ran in, and for
    ``bwrap`` what to hide and which cache the agent used."""

    mode: str  # "none" or "bwrap"
    bwrap: str | None = None
    hide_paths: tuple[str, ...] = ()
    cache_dir: Path | None = None

    def prefix(self, cwd: Path) -> list[str]:
        if self.mode != "bwrap":
            # The agent ran unconfined: it already executed arbitrary code
            # as this user, with network. Running its test command the
            # same way adds no exposure it did not already have. The
            # GitHub token is still stripped from the environment.
            return []
        assert self.bwrap is not None
        writable: list[str | Path] = [cwd]
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
) -> CommandRun:
    """Run ``command`` in ``cwd``, bounded by ``timeout``, without the
    operator's GitHub token, under ``confinement``."""
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
            argv = [*confinement.prefix(cwd), *command]
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
        output = tail(redact_secrets(stdout.decode(errors="replace")), _TAIL_CHARS)
        code = proc.returncode
        raw = stdout.decode(errors="replace").lstrip()
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
            exit_code=code, output=output,
        )
    finally:
        shutil.rmtree(private, ignore_errors=True)


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
        test_paths = sorted({c.path for c in changes if is_test_file(c.path)} | {
            c.old for c in changes
            if c.old and c.status == "R" and is_test_file(c.old)
        })
        diff = ""
        if test_paths:
            _, diff = await _git(
                head_copy, "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
                merge_base, head_sha, "--", *test_paths,
            )
        scan = scan_weakening(changes, diff)
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

        after = await run_proof_command(
            claim.command, head_copy, confinement, timeout=timeout,
        )
        common["after"] = after
        if after.status in ("timed out", "could not run"):
            return ProofReport(
                outcome=NOT_PROVEN,
                reason=f"on the branch, the proof command {after.describe()}",
                **common,
            )
        if after.status != "passed":
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
        before = await run_proof_command(
            claim.command, base_copy, confinement, timeout=timeout,
        )
        common["before"] = before
        if before.status in ("timed out", "could not run"):
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
