"""Proof of done (#221): the relay checks the evidence, not the agent's word.

The evidence tests build a real repository in ``tmp_path``, push a base
and a branch to a bare clone where ``WorktreeManager`` looks for it, and
run a real pytest command through :func:`ctrlrelay.core.proof.prove`. The
gate tests drive the real ``run_dev_issue`` and its proof step, with the
evidence function replaced, and check what the PR and the operator see.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ctrlrelay.pipelines.dev as dev
from ctrlrelay.cli import announce_issue_outcome
from ctrlrelay.core.config import AutomationConfig
from ctrlrelay.core.proof import (
    FAILED,
    NOT_PROVEN,
    NOT_REQUIRED,
    PROOF_COMMENT_MARKER,
    PROVEN,
    Change,
    CommandRun,
    Confinement,
    ProofClaim,
    ProofReport,
    confinement_for,
    is_docs_file,
    is_test_file,
    parse_proof_claim,
    plan_allows_test_removal,
    plan_says_no_test_possible,
    proof_comment,
    prove,
    run_proof_command,
    scan_weakening,
)
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager
from tests.test_dev_step_switches import GREEN, PR, _dispatcher, _fakes, _verifier

REPO = "o/r"

# --- the claim and the plan --------------------------------------------------


def test_no_proof_named_is_a_reason_not_a_claim() -> None:
    assert parse_proof_claim(None) == "the agent named no proof"


@pytest.mark.parametrize(
    "raw",
    [
        "pytest",
        {"command": [], "tests": ["tests/test_a.py::t"]},
        {"command": ["pytest", 3], "tests": ["tests/test_a.py::t"]},
        {"command": ["pytest"]},
        {"command": ["pytest"], "tests": []},
    ],
)
def test_a_malformed_proof_is_a_reason(raw) -> None:
    assert isinstance(parse_proof_claim(raw), str)


def test_a_string_command_is_split_like_a_shell_would() -> None:
    claim = parse_proof_claim(
        {"command": "pytest 'tests/a b.py::t'", "tests": "tests/a b.py::t"}
    )
    assert claim == ProofClaim(
        command=("pytest", "tests/a b.py::t"), tests=("tests/a b.py::t",),
    )


def test_plan_markers_need_a_reason() -> None:
    criteria = "1. x\n2. Test removal intended: the feature is gone\n"
    assert plan_allows_test_removal(criteria) == "the feature is gone"
    assert plan_allows_test_removal("1. Test removal intended:\n") is None
    assert plan_says_no_test_possible("3. No test possible: UI only") == "UI only"
    assert plan_says_no_test_possible(None) is None


def test_paths_are_classified() -> None:
    assert is_test_file("tests/test_a.py")
    assert is_test_file("src/pkg/test_thing.py")
    assert is_test_file("web/app.spec.ts")
    assert is_test_file("web/__tests__/x.js")
    assert not is_test_file("src/pkg/thing.py")
    assert is_docs_file("README.md")
    assert is_docs_file("docs/guide/setup.html")
    assert not is_docs_file("src/readme.py")


# --- the weakening scan ------------------------------------------------------


def _diff(path: str, removed: list[str], added: list[str]) -> str:
    out = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    out += [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(out) + "\n"


def test_a_deleted_test_file_is_a_hit() -> None:
    scan = scan_weakening([Change("D", "tests/test_a.py")], "")
    assert scan.hits == ("deleted test file `tests/test_a.py`",)


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("tests/test_a.py", "@pytest.mark.skip(reason='later')"),
        ("tests/test_a.py", "@pytest.mark.xfail"),
        ("tests/test_a.py", "@unittest.skip('x')"),
        ("tests/test_a.py", "        self.skipTest('x')"),
        ("web/a.test.ts", "it.skip('adds', () => {"),
        ("web/a.test.ts", "xit('adds', () => {"),
        ("web/a.test.ts", "it.only('adds', () => {"),
    ],
)
def test_an_added_skip_marker_is_a_hit(path: str, line: str) -> None:
    scan = scan_weakening([Change("M", path)], _diff(path, [], [line]))
    assert len(scan.hits) == 1 and "skip/xfail/only" in scan.hits[0], scan
    # The same line already there, moved within the diff, is not a hit.
    moved = scan_weakening([Change("M", path)], _diff(path, [line], [line]))
    assert moved.hits == ()


def test_a_removed_assert_is_a_hit() -> None:
    path = "tests/test_a.py"
    scan = scan_weakening(
        [Change("M", path)], _diff(path, ["    assert add(1, 2) == 3"], []),
    )
    assert scan.hits == (
        f"removed assert line in `{path}`: `assert add(1, 2) == 3`",
    )


def test_a_changed_assert_counts_as_removed() -> None:
    """The scan cannot tell a corrected expectation from a loosened one;
    the plan says when the change is intended."""
    path = "tests/test_a.py"
    scan = scan_weakening(
        [Change("M", path)],
        _diff(path, ["    assert add(1, 2) == 3"], ["    assert add(1, 2)"]),
    )
    assert len(scan.hits) == 1 and "removed assert line" in scan.hits[0]


def test_a_removed_test_function_is_a_hit() -> None:
    path = "tests/test_a.py"
    scan = scan_weakening(
        [Change("M", path)], _diff(path, ["def test_add():"], []),
    )
    assert scan.hits == ("removed test `test_add` from `tests/test_a.py`",)


def test_a_test_moved_to_another_file_is_clean() -> None:
    diff = _diff(
        "tests/test_a.py",
        ["def test_add():", "    assert add(1, 2) == 3"],
        [],
    ) + _diff(
        "tests/test_b.py",
        [],
        ["def test_add():", "    assert add(1, 2) == 3"],
    )
    scan = scan_weakening(
        [Change("M", "tests/test_a.py"), Change("M", "tests/test_b.py")], diff,
    )
    assert scan.hits == () and scan.unchecked == ()
    assert scan.describe() == "clean"


def test_an_unknown_language_is_not_checked_rather_than_clean() -> None:
    scan = scan_weakening(
        [Change("M", "pkg/thing_test.go"), Change("M", "tests/data/x.json")], "",
    )
    assert scan.unchecked == ("pkg/thing_test.go",)
    assert scan.describe() != "clean"


# --- a real repository -------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def git_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (home / ".gitconfig").write_text(
        "[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n"
    )
    return home


BUGGY = "def add(a, b):\n    return a - b\n\n\ndef neg(a):\n    return -a\n"
FIXED = BUGGY.replace("a - b", "a + b")
EXISTING_TEST = (
    "from calc import neg\n\n\ndef test_neg():\n    assert neg(2) == -2\n"
)
NEW_TEST = "\n\ndef test_add():\n    from calc import add\n\n    assert add(1, 2) == 3\n"


class Repo:
    """A source repo with a base commit on main, a bare clone where the
    manager looks, and helpers to commit a branch and push it there."""

    def __init__(self, tmp_path: Path) -> None:
        self.src = tmp_path / "src"
        self.src.mkdir()
        self.mgr = WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "bare",
        )
        _git("init", "-q", cwd=self.src)
        self.write("calc.py", BUGGY)
        self.write("tests/test_calc.py", EXISTING_TEST)
        self.write("README.md", "# calc\n")
        self.base = self.commit("base")
        self.bare = self.mgr._get_bare_repo_path(REPO)
        _git("clone", "-q", "--bare", str(self.src), str(self.bare), cwd=tmp_path)
        _git("checkout", "-q", "-b", "fix", cwd=self.src)

    def write(self, rel: str, text: str) -> None:
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, msg: str) -> str:
        _git("add", "-A", cwd=self.src)
        _git("commit", "-qm", msg, "--allow-empty", cwd=self.src)
        return _git("rev-parse", "HEAD", cwd=self.src)

    def push(self) -> str:
        head = _git("rev-parse", "HEAD", cwd=self.src)
        _git("push", "-q", str(self.bare), "fix", cwd=self.src)
        return head

    async def prove(
        self, claim, *, plan: str | None = None, timeout: float = 120,
        confinement: Confinement | None = None,
    ) -> ProofReport:
        head = self.push()
        return await prove(
            worktree=self.mgr,
            repo=REPO,
            head_sha=head,
            base_branch="main",
            base_sha=self.base,
            claim_raw=claim,
            plan_criteria=plan,
            confinement=confinement or Confinement(mode="none"),
            timeout=timeout,
        )


def _pytest_claim(test: str = "tests/test_calc.py::test_add") -> dict:
    return {
        "command": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
        "tests": [test],
    }


@pytest.mark.asyncio
async def test_fails_on_base_and_passes_on_head_is_proven(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write("tests/test_calc.py", EXISTING_TEST + NEW_TEST)
    repo.commit("fix add")
    report = await repo.prove(_pytest_claim())
    assert report.outcome == PROVEN, report
    assert report.after.status == "passed"
    assert report.before.status == "failed"
    # The base run used the branch's test against the base's code.
    assert "assert -1 == 3" in report.before.output
    assert report.base_sha == repo.base
    assert report.scan.describe() == "clean"
    # Nothing left behind.
    assert not list((tmp_path / "review").glob("ctrlrelay-review-*"))


@pytest.mark.asyncio
async def test_a_test_that_passes_on_base_too_is_failed(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write(
        "tests/test_calc.py",
        EXISTING_TEST + "\n\ndef test_neg_zero():\n    assert neg(0) == 0\n",
    )
    repo.commit("fix add, test something else")
    report = await repo.prove(_pytest_claim("tests/test_calc.py::test_neg_zero"))
    assert report.outcome == FAILED, report
    assert "passes on the base too" in report.reason
    assert report.before.status == "passed"


@pytest.mark.asyncio
async def test_a_test_that_does_not_pass_on_the_branch_is_failed(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", BUGGY + "\n# touched\n")
    repo.write("tests/test_calc.py", EXISTING_TEST + NEW_TEST)
    repo.commit("test only")
    report = await repo.prove(_pytest_claim())
    assert report.outcome == FAILED, report
    assert report.after.status == "failed"
    assert report.before is None


@pytest.mark.asyncio
async def test_no_proof_is_not_proven(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.commit("fix add")
    report = await repo.prove(None)
    assert report.outcome == NOT_PROVEN
    assert report.reason == "the agent named no proof"


@pytest.mark.asyncio
async def test_the_plan_saying_no_test_is_possible_holds_for_a_human(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.commit("fix add")
    report = await repo.prove(None, plan="1. No test possible: timing only")
    assert report.outcome == NOT_PROVEN
    assert "held for a human" in report.reason and "timing only" in report.reason


@pytest.mark.asyncio
async def test_a_timeout_is_not_proven(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write("tests/test_calc.py", EXISTING_TEST + NEW_TEST)
    repo.commit("fix add")
    claim = {
        "command": [sys.executable, "-c", "import time; time.sleep(30)"],
        "tests": ["tests/test_calc.py::test_add"],
    }
    report = await repo.prove(claim, timeout=1)
    assert report.outcome == NOT_PROVEN, report
    assert report.after.status == "timed out"


@pytest.mark.asyncio
async def test_a_missing_tool_is_not_proven(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write("tests/test_calc.py", EXISTING_TEST + NEW_TEST)
    repo.commit("fix add")
    claim = {
        "command": ["ctrlrelay-no-such-test-runner"],
        "tests": ["tests/test_calc.py::test_add"],
    }
    report = await repo.prove(claim)
    assert report.outcome == NOT_PROVEN, report
    assert report.after.status == "could not run"


@pytest.mark.asyncio
async def test_a_proof_naming_a_test_outside_the_test_files_is_not_proven(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED + "\n\ndef test_inline():\n    assert add(1, 2) == 3\n")
    repo.commit("fix add with an inline test")
    report = await repo.prove(_pytest_claim("calc.py::test_inline"))
    assert report.outcome == NOT_PROVEN, report
    assert report.after is None


@pytest.mark.asyncio
async def test_a_removed_assert_fails_the_run(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write("tests/test_calc.py", EXISTING_TEST.replace(
        "    assert neg(2) == -2\n", "    neg(2)\n") + NEW_TEST)
    repo.commit("fix add, drop an assert")
    report = await repo.prove(_pytest_claim())
    assert report.outcome == FAILED, report
    assert "weakens tests" in report.reason
    assert report.scan.hits
    assert report.after is None  # nothing of the agent's ran


@pytest.mark.asyncio
async def test_the_plan_can_allow_a_test_removal(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write("tests/test_calc.py", EXISTING_TEST.replace(
        "    assert neg(2) == -2\n", "    neg(2)\n") + NEW_TEST)
    repo.commit("fix add, drop an assert")
    report = await repo.prove(
        _pytest_claim(), plan="1. Test removal intended: neg is deprecated",
    )
    assert report.outcome == PROVEN, report
    assert report.weakening_allowed == "neg is deprecated"


@pytest.mark.asyncio
async def test_a_deleted_test_file_fails_the_run(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    (repo.src / "tests" / "test_calc.py").unlink()
    repo.write("tests/test_add.py", NEW_TEST)
    repo.commit("swap tests")
    report = await repo.prove(_pytest_claim("tests/test_add.py::test_add"))
    assert report.outcome == FAILED
    assert "deleted test file `tests/test_calc.py`" in report.scan.hits


@pytest.mark.asyncio
async def test_docs_only_needs_no_fail_before(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.write("README.md", "# calc\n\nAdds numbers.\n")
    repo.commit("docs")
    report = await repo.prove(None)
    assert report.outcome == NOT_REQUIRED
    assert report.reason == "not required: docs only"
    assert report.passed


@pytest.mark.asyncio
async def test_a_whitespace_only_change_is_failed(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", BUGGY.replace("return a - b", "return a - b   ") + "\n\n")
    repo.commit("whitespace")
    report = await repo.prove(_pytest_claim())
    assert report.outcome == FAILED
    assert "no real change" in report.reason


@pytest.mark.asyncio
async def test_an_empty_diff_is_failed(tmp_path: Path, git_home: Path) -> None:
    repo = Repo(tmp_path)
    repo.commit("nothing")
    report = await repo.prove(_pytest_claim())
    assert report.outcome == FAILED
    assert "no real change" in report.reason


# --- how the command runs ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_github_token_never_reaches_the_proof(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "y" * 36)
    run = await run_proof_command(
        [sys.executable, "-c",
         "import os; print(os.environ.get('GH_TOKEN', 'absent'),"
         " os.environ.get('GITHUB_TOKEN', 'absent'))"],
        tmp_path, Confinement(mode="none"),
    )
    assert run.status == "passed"
    assert run.output.strip() == "absent absent"


def test_an_unknown_confinement_is_refused() -> None:
    with pytest.raises(ValueError):
        confinement_for(SimpleNamespace(), REPO)
    assert confinement_for(SimpleNamespace(sandbox="none"), REPO).mode == "none"


def _bwrap_works() -> bool:
    found = shutil.which("bwrap")
    if not found:
        return False
    probe = subprocess.run(
        [found, "--ro-bind", "/", "/", "--unshare-net", "--", "true"],
        capture_output=True,
    )
    return probe.returncode == 0


needs_bwrap = pytest.mark.skipif(
    not _bwrap_works(), reason="bwrap missing or user namespaces not allowed",
)


@needs_bwrap
def test_a_sandboxed_run_uses_the_bwrap_prefix_with_no_network(
    tmp_path: Path,
) -> None:
    confinement = confinement_for(
        SimpleNamespace(
            sandbox="bwrap",
            sandbox_hide_paths=("~/.ssh",),
            sandbox_cache_dir=str(tmp_path / "cache"),
        ),
        REPO,
    )
    argv = confinement.prefix(tmp_path)
    assert argv[0].endswith("bwrap")
    assert "--unshare-net" in argv and argv[-1] == "--"
    assert ["--bind", str(tmp_path.resolve()), str(tmp_path.resolve())] == argv[
        argv.index("--bind"): argv.index("--bind") + 3
    ]


@needs_bwrap
@pytest.mark.asyncio
async def test_a_sandboxed_proof_runs_confined_and_still_proves(
    tmp_path: Path, git_home: Path
) -> None:
    repo = Repo(tmp_path)
    repo.write("calc.py", FIXED)
    repo.write("tests/test_calc.py", EXISTING_TEST + NEW_TEST)
    repo.commit("fix add")
    confinement = confinement_for(
        SimpleNamespace(
            sandbox="bwrap", sandbox_hide_paths=(),
            sandbox_cache_dir=str(tmp_path / "cache"),
        ),
        REPO,
    )
    # Outside /tmp, which the sandbox replaces with its own.
    outside = Path(__file__).parent / f".proof-probe-{os.getpid()}"
    report = await repo.prove(_pytest_claim(), confinement=confinement)
    assert report.outcome == PROVEN, report
    assert report.confinement == "bwrap, no network"
    # And it is confined: a write outside the copy fails.
    run = await run_proof_command(
        [sys.executable, "-c", f"open({str(outside)!r}, 'w').write('x')"],
        tmp_path / "src", confinement,
    )
    try:
        assert run.status == "failed" and not outside.exists(), run
    finally:
        outside.unlink(missing_ok=True)


# --- what the PR shows -------------------------------------------------------


def test_the_comment_shows_the_test_the_command_and_both_results() -> None:
    report = ProofReport(
        outcome=PROVEN,
        reason="fails on the base, passes on the branch",
        claim=ProofClaim(command=("pytest", "tests/t.py::a"), tests=("tests/t.py::a",)),
        before=CommandRun(status="failed", exit_code=1, output="assert 1 == 2"),
        after=CommandRun(status="passed", exit_code=0, output="1 passed"),
        head_sha="a" * 40,
        base_sha="b" * 40,
    )
    body = proof_comment(report)
    assert "Proof of done: proven" in body
    assert "`tests/t.py::a`" in body and "`pytest tests/t.py::a`" in body
    assert "failed (exit 1)" in body and "passed (exit 0)" in body
    assert body.rstrip().endswith(PROOF_COMMENT_MARKER)
    assert "stays a draft" not in body


def test_the_comment_redacts_the_output() -> None:
    token = "ghp_" + "z" * 36
    report = ProofReport(
        outcome=FAILED, reason="x",
        after=CommandRun(status="failed", exit_code=1, output=f"token={token}"),
    )
    assert token not in proof_comment(report)


# --- the gate, through the real run ------------------------------------------


def _proof_dispatcher():
    d = _dispatcher()
    d.sandbox = "none"
    return d


async def _gate_run(tmp_path: Path, monkeypatch, report, *, require_proof=True, **kw):
    fake = AsyncMock(return_value=report)
    monkeypatch.setattr(dev, "prove", fake)
    worktree, github = _fakes(tmp_path)
    github.list_issue_comments = AsyncMock(return_value=[])
    github.edit_issue_comment = AsyncMock()
    state_db = StateDB(tmp_path / "s.db")
    result = await dev.run_dev_issue(
        claims=None,
        repo="owner/repo",
        issue_number=123,
        branch_template="fix/issue-{n}",
        dispatcher=_proof_dispatcher(),
        github=github,
        worktree=worktree,
        dashboard=None,
        state_db=state_db,
        transport=None,
        contexts_dir=tmp_path / "contexts",
        pr_verifier=_verifier(GREEN),
        require_proof=require_proof,
        **kw,
    )
    return result, github, fake


def _proof_comments(github) -> list[str]:
    return [
        c.args[2] for c in github.comment_on_pr.await_args_list
        if PROOF_COMMENT_MARKER in c.args[2]
    ]


def _ready_calls(github) -> list:
    return [c for c in github.set_pr_draft.await_args_list if c.args[2] is False]


async def _announced(result) -> list[str]:
    return await announce_issue_outcome(
        AsyncMock(), repo="owner/repo", issue_number=123, is_task=False,
        result=result,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        ProofReport(outcome=NOT_PROVEN, reason="the agent named no proof"),
        ProofReport(outcome=FAILED, reason="the proof passes on the base too"),
    ],
)
async def test_a_run_without_proof_stays_a_draft_and_says_so(
    tmp_path: Path, monkeypatch, report
) -> None:
    result, github, _ = await _gate_run(tmp_path, monkeypatch, report)
    assert github.create_pr.await_args.kwargs.get("draft") is True
    assert _ready_calls(github) == []
    assert not result.success
    assert result.summary == f"PR #{PR} held as a draft: {report.outcome}"
    assert result.error == report.reason
    [body] = _proof_comments(github)
    assert report.reason in body and "stays a draft" in body
    lines = await _announced(result)
    assert any(f"held as a draft: {report.outcome}" in line for line in lines), lines
    assert not any("PR ready" in line for line in lines)


@pytest.mark.asyncio
async def test_a_proven_run_is_marked_ready(tmp_path: Path, monkeypatch) -> None:
    report = ProofReport(outcome=PROVEN, reason="fails on the base, passes on the branch")
    result, github, fake = await _gate_run(tmp_path, monkeypatch, report)
    assert result.success, result
    assert github.create_pr.await_args.kwargs.get("draft") is True
    assert len(_ready_calls(github)) == 1
    assert len(_proof_comments(github)) == 1
    kwargs = fake.await_args.kwargs
    assert kwargs["head_sha"] == "c0ffee" and kwargs["base_sha"] == "b" * 40
    assert kwargs["confinement"].mode == "none"


@pytest.mark.asyncio
async def test_a_proven_run_whose_evidence_cannot_be_posted_stays_a_draft(
    tmp_path: Path, monkeypatch
) -> None:
    report = ProofReport(outcome=PROVEN, reason="ok")
    fake = AsyncMock(return_value=report)
    monkeypatch.setattr(dev, "prove", fake)
    worktree, github = _fakes(tmp_path)
    github.list_issue_comments = AsyncMock(return_value=[])
    github.comment_on_pr = AsyncMock(side_effect=RuntimeError("gh down"))
    result = await dev.run_dev_issue(
        claims=None, repo="owner/repo", issue_number=123,
        branch_template="fix/issue-{n}", dispatcher=_proof_dispatcher(),
        github=github, worktree=worktree, dashboard=None,
        state_db=StateDB(tmp_path / "s.db"), transport=None,
        contexts_dir=tmp_path / "contexts", pr_verifier=_verifier(GREEN),
        require_proof=True,
    )
    assert not result.success
    assert _ready_calls(github) == []


@pytest.mark.asyncio
async def test_the_evidence_comment_is_edited_in_place_on_a_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    fake = AsyncMock(return_value=ProofReport(outcome=PROVEN, reason="ok"))
    monkeypatch.setattr(dev, "prove", fake)
    worktree, github = _fakes(tmp_path)
    github.list_issue_comments = AsyncMock(return_value=[
        {"id": 7, "body": "unrelated"},
        {"id": 9, "body": f"old proof\n{PROOF_COMMENT_MARKER}"},
    ])
    github.edit_issue_comment = AsyncMock()
    result = await dev.run_dev_issue(
        claims=None, repo="owner/repo", issue_number=123,
        branch_template="fix/issue-{n}", dispatcher=_proof_dispatcher(),
        github=github, worktree=worktree, dashboard=None,
        state_db=StateDB(tmp_path / "s.db"), transport=None,
        contexts_dir=tmp_path / "contexts", pr_verifier=_verifier(GREEN),
        require_proof=True,
    )
    assert result.success, result
    github.edit_issue_comment.assert_awaited_once()
    assert github.edit_issue_comment.await_args.args[1] == 9
    assert _proof_comments(github) == []


@pytest.mark.asyncio
async def test_a_check_the_relay_could_not_run_is_not_proven(
    tmp_path: Path, monkeypatch
) -> None:
    """The real evidence function, with a review copy that does not exist:
    the relay's own fault is "not proven", never a pass."""
    worktree, github = _fakes(tmp_path)
    github.list_issue_comments = AsyncMock(return_value=[])
    result = await dev.run_dev_issue(
        claims=None, repo="owner/repo", issue_number=123,
        branch_template="fix/issue-{n}", dispatcher=_proof_dispatcher(),
        github=github, worktree=worktree, dashboard=None,
        state_db=StateDB(tmp_path / "s.db"), transport=None,
        contexts_dir=tmp_path / "contexts", pr_verifier=_verifier(GREEN),
        require_proof=True,
    )
    assert not result.success
    assert result.summary == f"PR #{PR} held as a draft: {NOT_PROVEN}"
    assert "could not check the proof" in result.error
    assert _ready_calls(github) == []


@pytest.mark.asyncio
async def test_config_opt_out_says_so_on_the_pr_and_hands_over(
    tmp_path: Path, monkeypatch
) -> None:
    result, github, fake = await _gate_run(
        tmp_path, monkeypatch, None, require_proof=False,
    )
    assert result.success, result
    fake.assert_not_awaited()
    assert "draft" not in github.create_pr.await_args.kwargs
    [body] = _proof_comments(github)
    assert "proof not required by config" in body


@pytest.mark.asyncio
async def test_a_caller_that_does_not_say_runs_no_proof_step(
    tmp_path: Path, monkeypatch
) -> None:
    result, github, fake = await _gate_run(
        tmp_path, monkeypatch, None, require_proof=None,
    )
    assert result.success, result
    fake.assert_not_awaited()
    assert _proof_comments(github) == []


def test_proof_is_required_by_default() -> None:
    assert AutomationConfig().require_proof is True
    assert AutomationConfig(require_proof=False).require_proof is False


def test_every_cli_dev_run_passes_the_repo_setting() -> None:
    """The pipeline's own default runs no proof step, so each place the
    CLI starts or resumes a dev run must pass the config's value."""
    import ctrlrelay.cli as cli

    tree = ast.parse(Path(cli.__file__).read_text())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) in ("run_dev_issue", "resume_dev_from_pending")
    ]
    assert len(calls) >= 3
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert "require_proof" in kw, ast.unparse(call)
        assert ast.unparse(kw["require_proof"]).endswith("automation.require_proof")


@pytest.mark.asyncio
async def test_a_sandbox_that_does_not_start_is_could_not_run(tmp_path: Path) -> None:
    """bwrap failing to set up exits 1, like a failing test; it is not one."""
    fake = tmp_path / "bwrap"
    fake.write_text(
        "#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    run = await run_proof_command(
        ["true"], tmp_path, Confinement(mode="bwrap", bwrap=str(fake)),
    )
    assert run.status == "could not run", run
