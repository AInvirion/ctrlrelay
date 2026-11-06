"""The agent runs in a read-only copy of the machine (#236).

Checked by running a real dispatch under real bubblewrap: the "agent" is
a shell script that tries every read and write the issue names and
reports what happened. The fake home is NOT under /tmp, because the
sandbox replaces /tmp with an empty one and a home there would be out
of reach for that reason instead of the one under test.
"""

from __future__ import annotations

import re
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
try write_agent_state "touch $OWNPROJECT/run-log"
try write_other_project "touch $HOME/.claude/projects/-other/run-log"
try new_project_folder "mkdir $HOME/.claude/projects/-planted"
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
        extra_env={
            "GITCOMMON": str(bare), "GITDIR": str(gitdir),
            "OWNPROJECT": str(
                h / ".claude" / "projects" / re.sub(r"[/._]", "-", str(wt))
            ),
        },
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
        # Its own transcript folder only (#362): the projects folder is
        # read-only as a whole.
        "write_agent_state": "yes",
        "write_other_project": "no",
        "new_project_folder": "no",
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


def test_a_replaceable_read_only_link_stops_the_run_and_an_entry_level_one_does_not(
    tmp_path,
) -> None:
    from ctrlrelay.core.agent_env import SandboxUnavailableError, bwrap_prefix

    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "real.json").write_text("{}")
    (state / "settings.json").symlink_to(tmp_path / "real.json")
    (state / "projects").mkdir()
    (tmp_path / "mem").mkdir()
    (state / "projects" / "-p").symlink_to(tmp_path / "mem")
    # Entry by entry: the directory holding the link is not writable.
    bwrap_prefix("/usr/bin/bwrap", [], writable=[f"{state}/*"],
                 readonly=[state / "settings.json"])
    with pytest.raises(SandboxUnavailableError, match="could be replaced"):
        bwrap_prefix("/usr/bin/bwrap", [], writable=[state],
                     readonly=[state / "settings.json"])
    # A linked project directory, matched by a pattern, in a writable one.
    with pytest.raises(SandboxUnavailableError, match="could be replaced"):
        bwrap_prefix("/usr/bin/bwrap", [], writable=[f"{state}/*"],
                     readonly=[f"{state}/projects/*"])


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


def test_other_worktrees_are_locked_from_a_main_worktree_run_too(
    tmp_path, monkeypatch,
) -> None:
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wt = tmp_path / "wt"
    _git("init", "-q", str(wt), cwd=tmp_path)
    (wt / ".git" / "worktrees" / "other").mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(tmp_path / "c"),
    )
    argv = d._sandbox_prefix({}, wt, wt / ".ctrlrelay" / "state.json", private)
    locked = str((wt / ".git" / "worktrees").resolve())
    assert argv[argv.index(locked) - 1] == "--ro-bind"


def test_a_linked_main_config_is_copied_to_where_it_leads(
    tmp_path, monkeypatch,
) -> None:
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text("{}")
    (home / ".claude.json").symlink_to(real)
    wt = tmp_path / "wt"
    _git("init", "-q", str(wt), cwd=tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(tmp_path / "c"),
    )
    argv = d._sandbox_prefix({}, wt, wt / ".ctrlrelay" / "state.json", private)
    i = argv.index(str(private / "claude.json"))
    assert argv[i - 1:i + 2] == ["--bind", str(private / "claude.json"), str(real)]


def _worktree(home: Path) -> Path:
    bare = home / "repo.git"
    _git("init", "-q", "--bare", str(bare), cwd=home)
    seed = home / "seed"
    _git("init", "-q", "-b", "main", str(seed), cwd=home)
    _git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "init", cwd=seed)
    _git("push", "-q", str(bare), "main", cwd=seed)
    wt = home / "wt"
    _git("worktree", "add", "-q", "-b", "run", str(wt), "main", cwd=bare)
    return wt


# A projects folder per working directory the agent CLI ever ran in, named
# after the path: a node with a long history has thousands (#362).
_MANY = 4000


def _many_project_memories(home: Path) -> None:
    projects = home / "home" / ".claude" / "projects"
    for i in range(_MANY):
        name = f"-home-someone-.ctrlrelay-worktrees-owner-some-repo-dev-{i:05d}-abcdef12"
        (projects / name / "memory").mkdir(parents=True)


@pytest.mark.asyncio
async def test_thousands_of_project_folders_still_start_the_sandbox(
    home: Path,
) -> None:
    """The node that failed had 3695 `projects/*/memory` folders, each a
    read-only mount: past ARG_MAX bwrap could not be started at all. The
    options go through a file descriptor, so the run starts, the mounts
    still hold, and the command line stays short."""
    why = _bwrap_works()
    if why:
        pytest.skip(why)
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    _many_project_memories(home)
    wt = _worktree(home)
    some_memory = next((home / "home" / ".claude" / "projects").glob("*-00007-*"))
    agent = home / "agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        f"if touch '{some_memory}/memory/x' 2>/dev/null; then m=yes; else m=no; fi\n"
        'echo "ran=yes write_memory=$m" > report\n'
    )
    agent.chmod(0o755)
    seen: list[tuple[list[str], int]] = []
    d = ClaudeDispatcher(
        usage_store=None, claude_binary=str(agent), sandbox="bwrap",
        sandbox_cache_dir=str(home / "agent-cache"),
    )
    real = d._run_session

    async def spy(**kw):
        seen.append((kw["cmd"], kw["sandbox_prefix_len"]))
        return await real(**kw)

    d._run_session = spy  # type: ignore[method-assign]
    await d.spawn_session(
        session_id="s-362", prompt="p", working_dir=wt,
        state_file=wt / ".ctrlrelay" / "state.json", github_access=False,
        timeout=60,
    )
    assert (wt / "report").read_text().split() == ["ran=yes", "write_memory=no"]
    [(cmd, n)] = seen
    # The options are moved to the fd as the child starts; on the way in,
    # the prefix is marked for that.
    assert n > 0 and cmd[0].endswith("bwrap") and cmd[n - 1] == "--"


def test_control_a_mount_per_project_folder_cannot_start_bwrap(
    home: Path,
) -> None:
    """The rule before #362, a mount per `projects/*/memory`, on the same
    home: the kernel refuses to start bwrap (the node's failure). The
    default now mounts the projects folder once, and stays small."""
    if BWRAP is None:
        pytest.skip("bwrap not installed")
    import errno
    import os

    from ctrlrelay.core.agent_env import (
        DEFAULT_SANDBOX_HIDE_PATHS,
        DEFAULT_SANDBOX_READONLY_PATHS,
        DEFAULT_SANDBOX_WRITABLE_PATHS,
        bwrap_prefix,
    )

    _many_project_memories(home)

    def size(argv: list[str]) -> int:
        return sum(len(os.fsencode(a)) + 1 for a in argv)

    old = bwrap_prefix(
        BWRAP, DEFAULT_SANDBOX_HIDE_PATHS,
        writable=list(DEFAULT_SANDBOX_WRITABLE_PATHS),
        readonly=["~/.claude/projects/*/memory"],
    )
    assert size(old) > os.sysconf("SC_ARG_MAX") // 2
    with pytest.raises(OSError) as info:
        subprocess.run([*old, "true"], capture_output=True, timeout=30)
    assert info.value.errno == errno.E2BIG

    new = bwrap_prefix(
        BWRAP, DEFAULT_SANDBOX_HIDE_PATHS,
        writable=list(DEFAULT_SANDBOX_WRITABLE_PATHS),
        readonly=list(DEFAULT_SANDBOX_READONLY_PATHS),
    )
    assert size(new) < 8192


def test_bwrap_args_via_fd_round_trips_every_option() -> None:
    import os

    from ctrlrelay.core.agent_env import bwrap_args_via_fd

    prefix = ["/usr/bin/bwrap", "--ro-bind", "/a b", "/a b", "--tmpfs", "/t", "--"]
    argv, fd = bwrap_args_via_fd(prefix)
    try:
        assert argv == ["/usr/bin/bwrap", "--args", str(fd), "--"]
        data = os.read(fd, 1 << 16)
        assert data.split(b"\0")[:-1] == [os.fsencode(a) for a in prefix[1:-1]]
    finally:
        os.close(fd)
    assert bwrap_args_via_fd([]) == ([], None)
    with pytest.raises(ValueError):
        bwrap_args_via_fd(["/usr/bin/bwrap", "--tmpfs", "/t"])


def test_a_linked_transcript_folder_is_refused(home: Path) -> None:
    """A run before #362 could write the projects folder and leave a link
    where a later run's transcript folder goes. Opened writable, it would
    open its target: here the home directory."""
    import re as _re

    from ctrlrelay.core.agent_env import SandboxUnavailableError
    from ctrlrelay.core.dispatcher import ClaudeDispatcher

    wt = _worktree(home)
    h = home / "home"
    link = h / ".claude" / "projects" / _re.sub(r"[/._]", "-", str(wt))
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(h)
    if not BWRAP:
        pytest.skip("bwrap not installed")
    d = ClaudeDispatcher(
        usage_store=None, claude_binary="/bin/true", sandbox="bwrap",
        sandbox_cache_dir=str(home / "agent-cache"),
    )
    private = home / "private"
    private.mkdir()
    with pytest.raises(SandboxUnavailableError, match="is a link"):
        d._sandbox_prefix({}, wt, wt / ".ctrlrelay" / "state.json", private)
    assert (h / ".bashrc").read_text() == "# mine\n"
