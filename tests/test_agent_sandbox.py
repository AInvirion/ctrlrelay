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


def test_the_prefix_layers_read_only_writable_read_only_then_hidden(tmp_path) -> None:
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
        "--bind", str(wt), str(wt),
        "--ro-bind", str(settings), str(settings),
        "--tmpfs", str(hidden), "--ro-bind", "/dev/null", str(secret_file),
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
try create_local_settings "echo planted > $HOME/.claude/settings.local.json"
try swap_linked_entry "rm $HOME/.claude/CLAUDE.md; echo planted > $HOME/.claude/CLAUDE.md"
try new_top_level_file "touch $HOME/.claude/hooks.json"
try create_git_hook "mkdir -p $GITCOMMON/hooks && touch $GITCOMMON/hooks/post-checkout"
try hardlink_settings "ln $HOME/.claude/settings.json ./hl"
try hardlink_git_config "ln $GITCOMMON/config ./hl2"
try write_project_memory "echo planted >> $HOME/.claude/projects/-other/memory/MEMORY.md"
M="$HOME/.claude/projects/-bare/memory"
try create_project_memory "mkdir -p $M && touch $M/MEMORY.md"
try repoint_dot_git "echo 'gitdir: /elsewhere' > .git"
try worktree_config "echo planted >> $GITDIR/config.worktree"
try read_hidden_in_writable "grep -q token $HOME/.claude/secret-token"
try other_run_commondir "echo /elsewhere > $GITCOMMON/worktrees/other/commondir"
try rename_other_run "mv $GITCOMMON/worktrees/other $GITCOMMON/worktrees/other.x"
try write_main_config "echo planted >> $HOME/.claude.json"
try repoint_commondir "echo /elsewhere > $GITDIR/commondir"
try rename_worktrees "mv $GITCOMMON/worktrees $GITCOMMON/worktrees.x"
P="$HOME/.claude/projects"
try rename_project_for_memory "mv $P/-other $P/-other.x && mkdir -p $P/-other/memory"
try read_shell_snapshot "cat $HOME/.claude/shell-snapshots/snapshot-bash.sh"
try write_shell_snapshot "echo planted >> $HOME/.claude/shell-snapshots/snapshot-bash.sh"
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
    if Path.cwd().resolve().is_relative_to("/tmp"):
        pytest.skip("the fake home must be outside /tmp, which the sandbox replaces")
    base = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=Path.cwd()))
    h = base / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (h / ".config" / "gh").mkdir(parents=True)
    (h / ".config" / "gh" / "hosts.yml").write_text("oauth_token: secret")
    (h / ".bashrc").write_text("# mine\n")
    (h / ".local" / "bin").mkdir(parents=True)
    (h / ".claude" / "projects" / "-other" / "memory").mkdir(parents=True)
    (h / ".claude" / "projects" / "-other" / "memory" / "MEMORY.md").write_text("mine\n")
    (h / ".claude" / "projects" / "-bare").mkdir()
    (h / ".claude" / "settings.json").write_text("{}")
    (h / ".claude.json").write_text('{"mcpServers": {}}')
    (h / ".claude" / "shell-snapshots").mkdir()
    (h / ".claude" / "shell-snapshots" / "snapshot-bash.sh").write_text("export MINE=1\n")
    (base / "shared").mkdir()
    (base / "shared" / "CLAUDE.md").write_text("shared\n")
    (h / ".claude" / "CLAUDE.md").symlink_to(base / "shared" / "CLAUDE.md")
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
    # Another run's worktree in the same clone.
    _git("worktree", "add", "-q", "-b", "other", str(home / "other"), "main", cwd=bare)

    # Not there before the run: the agent must not be able to create it.
    shutil.rmtree(bare / "hooks")
    (h / ".claude" / "secret-token").write_text("token")
    gitdir = Path(_git("rev-parse", "--path-format=absolute", "--git-dir", cwd=wt))
    agent = home / "agent.sh"
    agent.write_text(AGENT)
    agent.chmod(0o755)
    from ctrlrelay.core.agent_env import DEFAULT_SANDBOX_HIDE_PATHS

    d = ClaudeDispatcher(
        usage_store=None, claude_binary=str(agent), sandbox="bwrap",
        extra_env={"GITCOMMON": str(bare), "GITDIR": str(gitdir)},
        sandbox_cache_dir=str(home / "agent-cache"),
        sandbox_hide_paths=(*DEFAULT_SANDBOX_HIDE_PATHS, "~/.claude/secret-token"),
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
        "create_local_settings": "no",
        "swap_linked_entry": "no",
        "new_top_level_file": "no",
        "create_git_hook": "no",
        "hardlink_settings": "no",
        "hardlink_git_config": "no",
        "write_project_memory": "no",
        "create_project_memory": "no",
        "repoint_dot_git": "no",
        "worktree_config": "no",
        "read_hidden_in_writable": "no",
        "other_run_commondir": "no",
        "rename_other_run": "no",
        # Its own copy; the real one is checked below.
        "write_main_config": "yes",
        "repoint_commondir": "no",
        "rename_worktrees": "no",
        "rename_project_for_memory": "no",
        "read_shell_snapshot": "no",
        # Into the sandbox's own empty copy; the real one is checked below.
        "write_shell_snapshot": "yes",
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
    assert not (h / ".claude" / "settings.local.json").exists()
    assert (h / ".claude.json").read_text() == '{"mcpServers": {}}'
    snap = h / ".claude" / "shell-snapshots" / "snapshot-bash.sh"
    assert snap.read_text() == "export MINE=1\n"
    assert (h / ".claude" / "projects" / "-other" / "memory" / "MEMORY.md").exists()
    assert (gitdir / "commondir").read_text().strip() != "/elsewhere"
    assert (h / ".claude" / "CLAUDE.md").is_symlink()
    assert (home / "shared" / "CLAUDE.md").read_text() == "shared\n"
    assert (home / "agent-cache" / "uv" / "x").exists()
    assert _git("log", "-1", "--format=%s", "run", cwd=bare) == "change"
    assert (wt / ".git").read_text().startswith("gitdir: ")
    assert "planted" not in (bare / "config").read_text()


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



@pytest.mark.asyncio
async def test_a_state_file_outside_the_worktree_is_refused(home: Path) -> None:
    from ctrlrelay.core.agent_env import SandboxUnavailableError
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    wt = home / "wt"
    wt.mkdir()
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(home / "c"),
    ) if BWRAP else None
    if d is None:
        pytest.skip("bwrap not installed")
    with pytest.raises(SandboxUnavailableError, match="outside the worktree"):
        await d.spawn_session(
            session_id="s", prompt="p", working_dir=wt,
            state_file=home / "home" / "state.json", github_access=False,
        )


def test_a_dangling_link_at_a_read_only_path_stops_the_run(tmp_path) -> None:
    from ctrlrelay.core.agent_env import SandboxUnavailableError, bwrap_prefix

    open_dir = tmp_path / "open"
    open_dir.mkdir()
    (open_dir / "settings.local.json").symlink_to(tmp_path / "gone" / "x.json")
    with pytest.raises(SandboxUnavailableError, match="link to something missing"):
        bwrap_prefix(
            "/usr/bin/bwrap", [], writable=[open_dir],
            readonly=[open_dir / "settings.local.json"],
        )


def test_a_missing_read_only_path_nothing_writable_reaches_is_left_alone(
    tmp_path,
) -> None:
    from ctrlrelay.core.agent_env import bwrap_prefix

    bwrap_prefix("/usr/bin/bwrap", [], writable=[], readonly=[tmp_path / "hooks"])
    assert not (tmp_path / "hooks").exists()


def test_a_missing_read_only_path_under_a_symlinked_writable_dir_is_created(
    tmp_path,
) -> None:
    from ctrlrelay.core.agent_env import bwrap_prefix

    real = tmp_path / "real-claude"
    real.mkdir()
    link = tmp_path / "claude"
    link.symlink_to(real)
    argv = bwrap_prefix(
        "/usr/bin/bwrap", [], writable=[link],
        readonly=[link / "settings.local.json"],
    )
    made = real / "settings.local.json"
    assert made.read_text() == "{}\n"
    assert argv[-4:] == ["--ro-bind", str(made), str(made), "--"]


def test_the_agent_caches_are_its_own(tmp_path) -> None:
    from ctrlrelay.core.agent_env import agent_cache_env

    env = agent_cache_env(tmp_path)
    assert all(Path(v).is_relative_to(tmp_path) for v in env.values())
    assert {"XDG_CACHE_HOME", "npm_config_cache", "GOMODCACHE", "CARGO_HOME"} <= set(env)


def test_a_read_only_path_that_cannot_be_made_stops_the_run(tmp_path) -> None:
    from ctrlrelay.core.agent_env import SandboxUnavailableError, bwrap_prefix

    open_dir = tmp_path / "open"
    open_dir.mkdir()
    (open_dir / "plugins").write_text("a file where a directory goes")
    # A file named like a directory blocks creating hooks inside it.
    with pytest.raises(SandboxUnavailableError, match="cannot create"):
        bwrap_prefix(
            "/usr/bin/bwrap", [], writable=[open_dir],
            readonly=[open_dir / "plugins" / "hooks"],
        )


def test_a_hidden_path_wins_over_a_read_only_one_naming_it(tmp_path) -> None:
    from ctrlrelay.core.agent_env import bwrap_prefix

    secret = tmp_path / "ssh"
    secret.mkdir()
    argv = bwrap_prefix("/usr/bin/bwrap", [str(secret)], readonly=[secret])
    assert argv[-6:] == [
        "--ro-bind", str(secret), str(secret), "--tmpfs", str(secret), "--",
    ]


def test_a_missing_git_config_is_made_a_file_not_a_directory(
    tmp_path, monkeypatch,
) -> None:
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wt = tmp_path / "wt"
    _git("init", "-q", str(wt), cwd=tmp_path)
    (wt / ".git" / "config").unlink()
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(tmp_path / "c"),
    )
    private = tmp_path / "private"
    private.mkdir()
    d._sandbox_prefix({}, wt, wt / ".ctrlrelay" / "state.json", private)
    assert (wt / ".git" / "config").is_file()


@pytest.mark.parametrize("make", ["nothing", "dangling", "inside-another"])
def test_a_worktree_whose_git_directory_cannot_be_found_is_refused(
    tmp_path, make,
) -> None:
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    from ctrlrelay.core.agent_env import SandboxUnavailableError
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    wt = tmp_path / "wt"
    wt.mkdir()
    if make == "dangling":
        (wt / ".git").symlink_to(tmp_path / "gone")
    if make == "inside-another":
        # A home kept in git: the enclosing repository is not this run's.
        _git("init", "-q", str(tmp_path), cwd=tmp_path)
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(tmp_path / "c"),
    )
    with pytest.raises(SandboxUnavailableError, match="git directories"):
        d._sandbox_prefix({}, wt, wt / ".ctrlrelay" / "state.json", tmp_path)


def test_a_cache_dir_that_contains_home_is_refused(tmp_path, monkeypatch) -> None:
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    from ctrlrelay.core.agent_env import SandboxUnavailableError
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "cache-link").symlink_to(home)
    wt = tmp_path / "wt"
    _git("init", "-q", str(wt), cwd=tmp_path)
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(tmp_path / "cache-link"),
    )
    with pytest.raises(SandboxUnavailableError, match="contains the home directory"):
        d._sandbox_prefix({}, wt, wt / ".ctrlrelay" / "state.json", tmp_path)


def test_a_replaceable_read_only_link_is_named_and_an_entry_level_one_is_not(
    tmp_path, caplog,
) -> None:
    import logging

    from ctrlrelay.core.agent_env import bwrap_prefix

    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "real.json").write_text("{}")
    (state / "settings.json").symlink_to(tmp_path / "real.json")
    (state / "projects").mkdir()
    with caplog.at_level(logging.WARNING):
        bwrap_prefix("/usr/bin/bwrap", [], writable=[f"{state}/*"],
                     readonly=[state / "settings.json"])
        assert not [r for r in caplog.records if "replaceable" in r.getMessage()]
        bwrap_prefix("/usr/bin/bwrap", [], writable=[state],
                     readonly=[state / "settings.json"])
    [rec] = [r for r in caplog.records if "replaceable" in r.getMessage()]
    assert rec.path == str(state / "settings.json")


def test_claude_config_dir_set_is_refused(tmp_path, monkeypatch) -> None:
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    from ctrlrelay.core.agent_env import SandboxUnavailableError
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    wt = tmp_path / "wt"
    _git("init", "-q", str(wt), cwd=tmp_path)
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(tmp_path / "c"),
    )
    with pytest.raises(SandboxUnavailableError, match="CLAUDE_CONFIG_DIR"):
        d._sandbox_prefix({"CLAUDE_CONFIG_DIR": "/x"}, wt,
                          wt / ".ctrlrelay" / "state.json", tmp_path)
