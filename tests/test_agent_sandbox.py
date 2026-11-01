"""The agent runs in a read-only copy of the machine (#236).

Checked by running a real dispatch under real bubblewrap: the "agent" is
a shell script that tries every read and write the issue names and
reports what happened. The fake home is NOT under /tmp, because the
sandbox replaces /tmp with an empty one and a home there would be out
of reach for that reason instead of the one under test.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

BWRAP = shutil.which("bwrap")


def _bwrap_works() -> str | None:
    if BWRAP is None:
        return "bwrap not installed"
    out = subprocess.run(
        [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--unshare-pid", "true"],
        capture_output=True, text=True, timeout=30,
    )
    return None if out.returncode == 0 else f"bwrap cannot run here: {out.stderr.strip()}"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_the_prefix_layers_read_only_hidden_writable_then_read_only(tmp_path) -> None:
    from ctrlrelay.core.agent_env import bwrap_prefix

    hidden = tmp_path / "ssh"
    hidden.mkdir()
    secret_file = tmp_path / "netrc"
    secret_file.write_text("machine x")
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = wt / "settings.json"
    settings.write_text("{}")
    argv = bwrap_prefix(
        "/usr/bin/bwrap", [str(hidden), str(secret_file), str(tmp_path / "missing")],
        writable=[wt, tmp_path / "missing-too"], readonly=[settings],
    )
    assert argv == [
        "/usr/bin/bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--unshare-pid", "--die-with-parent",
        "--tmpfs", str(hidden), "--ro-bind", "/dev/null", str(secret_file),
        "--bind", str(wt), str(wt),
        "--ro-bind", str(settings), str(settings),
        "--",
    ]


AGENT = r"""#!/bin/sh
r="$PWD/report"
try() { if sh -c "$2" >/dev/null 2>&1; then echo "$1=yes" >> "$r"; else echo "$1=no" >> "$r"; fi; }
try read_ssh_key "cat $HOME/.ssh/id_ed25519"
try read_gh_login "cat $HOME/.config/gh/hosts.yml"
try write_bashrc "echo planted >> $HOME/.bashrc"
try write_local_bin "touch $HOME/.local/bin/planted"
try write_settings "echo planted >> $HOME/.claude/settings.json"
try write_agent_state "touch $HOME/.claude/projects/run-log"
try write_hooks "touch $GITCOMMON/hooks/pre-push"
try write_git_config "git config --file $GITCOMMON/config core.fsmonitor /bin/evil"
try write_tmp "touch /tmp/scratch"
try write_cache "mkdir -p $XDG_CACHE_HOME/uv && touch $XDG_CACHE_HOME/uv/x"
G="git -c user.email=a@b -c user.name=a"
try commit "echo change > file.txt && git add file.txt && $G commit -qm change"
"""


@pytest.fixture
def home(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=Path.cwd()))
    h = base / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (h / ".config" / "gh").mkdir(parents=True)
    (h / ".config" / "gh" / "hosts.yml").write_text("oauth_token: secret")
    (h / ".bashrc").write_text("# mine\n")
    (h / ".local" / "bin").mkdir(parents=True)
    (h / ".claude" / "projects").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(h))
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_sandboxed_agent_reaches_only_its_worktree_tmp_state_and_cache(
    home: Path,
) -> None:
    why = _bwrap_works()
    if why:
        pytest.skip(why)
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    h = home / "home"
    bare = home / "repo.git"
    _git("init", "-q", "--bare", str(bare), cwd=home)
    seed = home / "seed"
    _git("init", "-q", "-b", "main", str(seed), cwd=home)
    _git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "init", cwd=seed)
    _git("push", "-q", str(bare), "main", cwd=seed)
    wt = home / "wt"
    _git("worktree", "add", "-q", "-b", "run", str(wt), "main", cwd=bare)

    agent = home / "agent.sh"
    agent.write_text(AGENT)
    agent.chmod(0o755)
    d = ClaudeDispatcher(
        usage_store=None, claude_binary=str(agent), sandbox="bwrap",
        extra_env={"GITCOMMON": str(bare)},
        sandbox_cache_dir=str(home / "agent-cache"),
    )
    await d.spawn_session(
        session_id="s-236", prompt="p", working_dir=wt,
        state_file=wt / ".ctrlrelay" / "state.json", github_access=False,
        timeout=60,
    )

    report = dict(
        line.split("=") for line in (wt / "report").read_text().splitlines()
    )
    assert report == {
        "read_ssh_key": "no",
        "read_gh_login": "no",
        "write_bashrc": "no",
        "write_local_bin": "no",
        "write_settings": "no",
        "write_agent_state": "yes",
        "write_hooks": "no",
        "write_git_config": "no",
        "write_tmp": "yes",
        "write_cache": "yes",
        "commit": "yes",
    }
    assert (h / ".bashrc").read_text() == "# mine\n"
    assert (h / ".claude" / "settings.json").read_text() == "{}"
    assert not (h / ".local" / "bin" / "planted").exists()
    assert not (bare / "hooks" / "pre-push").exists()
    assert (home / "agent-cache" / "uv" / "x").exists()
    assert _git("log", "-1", "--format=%s", "run", cwd=bare) == "change"


@pytest.mark.asyncio
async def test_control_without_the_sandbox_the_same_agent_reaches_everything(
    home: Path,
) -> None:
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    wt = home / "wt"
    wt.mkdir()
    _git("init", "-q", str(wt), cwd=home)
    agent = home / "agent.sh"
    agent.write_text(AGENT)
    agent.chmod(0o755)
    d = ClaudeDispatcher(
        usage_store=None, claude_binary=str(agent),
        extra_env={"GITCOMMON": str(wt / ".git"), "XDG_CACHE_HOME": str(home / "c")},
    )
    await d.spawn_session(
        session_id="s-236", prompt="p", working_dir=wt,
        state_file=wt / "state.json", github_access=False, timeout=60,
    )
    report = (wt / "report").read_text()
    assert "read_ssh_key=yes" in report
    assert "write_bashrc=yes" in report
    assert "write_settings=yes" in report
