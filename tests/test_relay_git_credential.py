"""The relay's own fetch, clone and ls-remote use gh's login (#398)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ctrlrelay.core.worktree import (
    WorktreeManager,
    _git_subcommand,
    _with_gh_credential,
)

KEY = "credential.https://github.com.helper"


@pytest.fixture
def fake_gh(tmp_path: Path) -> Path:
    """A gh that answers ``auth git-credential`` as a logged-in gh does."""
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "auth git-credential" ]; then\n'
        "  cat >/dev/null\n"
        "  echo username=x-access-token\n"
        "  echo password=from-gh\n"
        "fi\n"
    )
    gh.chmod(0o755)
    return gh


def _gh_at(path: Path | None):
    return patch(
        "ctrlrelay.core.github._find_gh",
        return_value=str(path) if path else "gh-not-installed-here",
    )


def test_appends_after_existing_entries(fake_gh: Path) -> None:
    base = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.git@github.com:.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
    }
    with _gh_at(fake_gh):
        env = _with_gh_credential(base)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "url.git@github.com:.insteadOf"
    assert env["GIT_CONFIG_VALUE_0"] == "https://github.com/"
    assert env["GIT_CONFIG_KEY_1"] == KEY
    assert env["GIT_CONFIG_VALUE_1"] == f"!{fake_gh} auth git-credential"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert base["GIT_CONFIG_COUNT"] == "1", "the input must not be mutated"


def test_a_gh_that_cannot_be_found_adds_no_helper_but_still_no_prompt() -> None:
    with _gh_at(None):
        env = _with_gh_credential({"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "GIT_TERMINAL_PROMPT": "0"}


@pytest.mark.parametrize("count", ["two", "-1"])
def test_a_malformed_count_is_left_for_git_to_refuse(
    fake_gh: Path, count: str,
) -> None:
    with _gh_at(fake_gh):
        env = _with_gh_credential({"GIT_CONFIG_COUNT": count})
    assert env["GIT_CONFIG_COUNT"] == count
    assert not any(k.startswith("GIT_CONFIG_KEY_") for k in env)


@pytest.mark.parametrize(
    ("args", "sub"),
    [
        (("fetch", "origin"), "fetch"),
        (("-c", "core.hooksPath=/dev/null", "clone", "u"), "clone"),
        (("-C", "/x", "ls-remote", "origin"), "ls-remote"),
        (("--no-pager", "worktree", "list"), "worktree"),
        (("--git-dir", "/repo", "fetch", "origin"), "fetch"),
        ((), None),
    ],
)
def test_subcommand(args: tuple[str, ...], sub: str | None) -> None:
    assert _git_subcommand(args) == sub


def _credential_fill(env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=o/r.git\n\n",
        capture_output=True, text=True, env=env, check=True, timeout=30,
    ).stdout


def _isolated(tmp_path: Path, global_config: str = "") -> dict[str, str]:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(global_config)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(cfg)
    env["HOME"] = str(tmp_path)
    return env


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_asks_gh_when_no_helper_is_configured(
    tmp_path: Path, fake_gh: Path,
) -> None:
    with _gh_at(fake_gh):
        env = _with_gh_credential(_isolated(tmp_path))
    out = _credential_fill(env)
    assert "password=from-gh" in out


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_operator_helper_is_asked_first_and_wins(
    tmp_path: Path, fake_gh: Path,
) -> None:
    own = tmp_path / "own-helper"
    own.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        "echo username=operator\necho password=from-operator\n"
    )
    own.chmod(0o755)
    base = _isolated(tmp_path, f'[credential]\n\thelper = "!{own}"\n')
    with _gh_at(fake_gh):
        env = _with_gh_credential(base)
    out = _credential_fill(env)
    assert "password=from-operator" in out
    assert "from-gh" not in out


def _manager(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(
        worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "bare",
    )


def _proc() -> AsyncMock:
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0
    return proc


@pytest.mark.parametrize(
    "args",
    [
        ("fetch", "origin"),
        ("ls-remote", "--heads", "origin", "main"),
        ("clone", "--bare", "https://github.com/o/r.git", "/x"),
    ],
)
@pytest.mark.asyncio
async def test_run_git_gives_network_calls_the_helper(
    tmp_path: Path, fake_gh: Path, args: tuple[str, ...],
) -> None:
    mgr = _manager(tmp_path)
    before = dict(os.environ)
    with _gh_at(fake_gh), patch(
        "asyncio.create_subprocess_exec", return_value=_proc(),
    ) as spawn:
        await mgr._run_git(*args)
    env = spawn.call_args.kwargs["env"]
    n = int(env["GIT_CONFIG_COUNT"]) - 1
    assert env[f"GIT_CONFIG_KEY_{n}"] == KEY
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # The process environment is untouched, so the agent's environment,
    # which is built from it (#219), never sees the helper.
    assert dict(os.environ) == before


@pytest.mark.asyncio
async def test_run_git_leaves_local_calls_and_explicit_envs_alone(
    tmp_path: Path, fake_gh: Path,
) -> None:
    mgr = _manager(tmp_path)
    given = {"PATH": "/usr/bin"}
    with _gh_at(fake_gh), patch(
        "asyncio.create_subprocess_exec", return_value=_proc(),
    ) as spawn:
        await mgr._run_git("worktree", "list", "--porcelain")
        await mgr._run_git("fetch", "origin", env=given)
    assert spawn.call_args_list[0].kwargs["env"] is None
    assert spawn.call_args_list[1].kwargs["env"] is given

