"""Tests for personalization sync.

Three layers:

1. ``paths`` — path encoding (matches Claude's actual encoding) +
   template resolution.
2. ``manager.wire_symlinks`` — idempotent + replace-stale +
   refuse-real-file + skip-missing-source.
3. ``manager.push/pull`` — integration against a tmp bare-repo "remote"
   and two tmp working checkouts simulating machines A and B. Covers
   the happy path and the rebase-conflict abort path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from ctrlrelay.core.config import (
    Config,
    ConfigError,
    load_config,
)
from ctrlrelay.personalization import (
    PersonalizationManager,
    TemplateContext,
    encode_project_path,
    resolve_template,
)
from ctrlrelay.personalization.manager import PersonalizationError, _run_git
from ctrlrelay.personalization.paths import project_slug

# ---------- Layer 1: path encoding + template resolution ---------------------


class TestEncodeProjectPath:
    def test_simple_absolute_path(self) -> None:
        assert encode_project_path("/Users/foo/Projects/bar") == "-Users-foo-Projects-bar"

    def test_dot_in_path_becomes_dash(self) -> None:
        # ``.ctrlrelay`` becomes ``-ctrlrelay``; combined with the
        # preceding ``/`` you get ``--ctrlrelay``.
        encoded = encode_project_path("/Users/foo/.ctrlrelay/repos/x")
        assert encoded == "-Users-foo--ctrlrelay-repos-x"

    def test_underscore_becomes_dash(self) -> None:
        # Verified against ~/.claude/projects/ on a real install:
        # ``oscarvalenzuelab/my_lalanotes`` is encoded as
        # ``oscarvalenzuelab-my-lalanotes``.
        encoded = encode_project_path("/Users/foo/Projects/my_lalanotes")
        assert encoded == "-Users-foo-Projects-my-lalanotes"

    def test_existing_hyphen_preserved(self) -> None:
        encoded = encode_project_path("/Users/foo/Projects/dev-sync")
        assert encoded == "-Users-foo-Projects-dev-sync"

    def test_dot_in_repo_name(self) -> None:
        # ``SemClone/semcl.one`` per real install → ``semcl-one``.
        encoded = encode_project_path("/Users/foo/SEMCL.ONE/semcl.one")
        assert encoded == "-Users-foo-SEMCL-ONE-semcl-one"

    def test_relative_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            encode_project_path("relative/path")

    def test_tilde_expansion(self) -> None:
        encoded = encode_project_path("~")
        # The encoding should start with the encoded form of $HOME.
        # We don't know the literal home in CI so just assert it
        # starts with ``-`` and contains no unescaped path separators.
        assert encoded.startswith("-")
        assert "/" not in encoded


class TestResolveTemplate:
    def test_home_only_no_project_context(self) -> None:
        result = resolve_template("${HOME}/.claude/CLAUDE.md", TemplateContext())
        assert result == Path.home() / ".claude" / "CLAUDE.md"

    def test_tilde_works_too(self) -> None:
        result = resolve_template("~/.claude/CLAUDE.md", TemplateContext())
        assert result == Path.home() / ".claude" / "CLAUDE.md"

    def test_project_placeholders(self, tmp_path: Path) -> None:
        local = tmp_path / "owner" / "repo"
        ctx = TemplateContext(project="owner-repo", project_local=local)
        assert resolve_template("${PROJECT}", ctx) == Path("owner-repo")
        assert resolve_template("${PROJECT_LOCAL}", ctx) == local
        assert resolve_template("${PROJECT_PARENT}", ctx) == tmp_path / "owner"
        assert resolve_template("${PROJECT_ENCODED}", ctx) == Path(
            encode_project_path(local)
        )

    def test_missing_project_context_raises(self) -> None:
        with pytest.raises(ValueError, match="project context"):
            resolve_template("${PROJECT}/foo", TemplateContext())

    def test_combined_placeholders(self, tmp_path: Path) -> None:
        local = tmp_path / "AInvirion" / "ctrlrelay"
        ctx = TemplateContext(project="AInvirion-ctrlrelay", project_local=local)
        result = resolve_template("${PROJECT_PARENT}/specs/${PROJECT}/", ctx)
        assert result == tmp_path / "AInvirion" / "specs" / "AInvirion-ctrlrelay"

    def test_project_encoded_doesnt_eat_project(self, tmp_path: Path) -> None:
        # Substitution order must replace ``${PROJECT_ENCODED}`` before
        # ``${PROJECT}`` so the latter doesn't partially-eat the former.
        local = tmp_path / "x"
        ctx = TemplateContext(project="x", project_local=local)
        # Both placeholders in one template:
        out = resolve_template("a/${PROJECT_ENCODED}/b/${PROJECT}/c", ctx)
        assert "${PROJECT" not in str(out)


class TestProjectSlug:
    def test_simple(self) -> None:
        assert project_slug("AInvirion/ctrlrelay") == "AInvirion--ctrlrelay"

    def test_no_collision_between_hyphenated_pairs(self) -> None:
        # Codex pass 6 finding: single-hyphen flattening collides for
        # ``a-b/c`` and ``a/b-c``. Double-hyphen separator avoids it.
        assert project_slug("a-b/c") != project_slug("a/b-c")
        assert project_slug("a-b/c") == "a-b--c"
        assert project_slug("a/b-c") == "a--b-c"


# ---------- Layer 2: config loading ------------------------------------------


def _base_config_dict(personalization: dict | None = None) -> dict:
    """Minimal valid orchestrator.yaml dict, optionally with a personalization block."""
    cfg: dict = {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": "~/.ctrlrelay/state.db",
            "worktrees": "~/.ctrlrelay/worktrees",
            "bare_repos": "~/.ctrlrelay/repos",
            "contexts": "~/.ctrlrelay/contexts",
            "skills": "~/.ctrlrelay/skills",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": "~/.ctrlrelay/inbox.txt",
                "outbox": "~/.ctrlrelay/outbox.txt",
            },
        },
        "dashboard": {"enabled": False},
        "repos": [],
    }
    if personalization is not None:
        cfg["personalization"] = personalization
    return cfg


class TestPersonalizationConfig:
    def test_personalization_optional(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict()))
        config = load_config(path)
        assert config.personalization is None
        assert config.personalization_branch() is None

    def test_personalization_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/dotclaude",
            "paths": [
                {"source": "global/CLAUDE.md", "target": "~/.claude/CLAUDE.md"},
            ],
        })))
        config = load_config(path)
        assert config.personalization is not None
        assert config.personalization.repo == "owner/dotclaude"
        assert config.personalization_branch() == "personalization/test-node"

    def test_invalid_repo_name(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "not_a_valid/owner/repo",  # extra slash
            "paths": [],
        })))
        with pytest.raises(ConfigError):
            load_config(path)

    def test_unknown_placeholder_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "paths": [
                {"source": "x/${BOGUS}/y", "target": "~/x"},
            ],
        })))
        with pytest.raises(ConfigError, match="unknown placeholders"):
            load_config(path)

    def test_project_placeholder_without_project_scoped_rejected(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "paths": [
                {"source": "x/${PROJECT}/", "target": "~/x/"},
                # missing project_scoped: true
            ],
        })))
        with pytest.raises(ConfigError, match="project_scoped"):
            load_config(path)

    @pytest.mark.parametrize("bad_source", [
        "../secret",        # parent escape
        "foo/../etc",       # buried parent escape
        "/etc/passwd",      # absolute
    ])
    def test_source_path_escape_rejected(
        self, tmp_path: Path, bad_source: str
    ) -> None:
        """``source`` must stay inside the personalization checkout —
        ``..`` segments and absolute paths would let a config wire
        symlinks pointing outside the repo and crash later in ``git
        add`` (Codex pass 8 finding).
        """
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "paths": [
                {"source": bad_source, "target": "~/safe"},
            ],
        })))
        with pytest.raises(ConfigError):
            load_config(path)

    def test_home_in_source_rejected(self, tmp_path: Path) -> None:
        """${HOME} is a valid target placeholder but nonsensical in
        source — source is rooted at the personalization checkout,
        not the user's home dir. Earlier the validator accepted it
        and wire would silently treat it as a literal subdir whose
        name was ``${HOME}`` (Codex pass 11).
        """
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "paths": [
                {"source": "${HOME}/something", "target": "~/elsewhere"},
            ],
        })))
        with pytest.raises(ConfigError, match="HOME"):
            load_config(path)

    def test_target_dotdot_rejected(self, tmp_path: Path) -> None:
        """``..`` in target is also rejected for auditability."""
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "paths": [
                {"source": "global/CLAUDE.md", "target": "~/.claude/../etc"},
            ],
        })))
        with pytest.raises(ConfigError):
            load_config(path)

    def test_dir_vs_file_mismatch_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "paths": [
                {"source": "x/", "target": "~/x"},  # source is dir, target file
            ],
        })))
        with pytest.raises(ConfigError, match="directory vs file"):
            load_config(path)

    def test_effective_node_id_fallback_validated(self, tmp_path: Path) -> None:
        """When personalization.node_id is omitted, the top-level
        node_id is used. If the top-level is not git-branch-safe (e.g.
        a hostname with spaces), config load must fail upfront rather
        than letting init/push blow up later.
        """
        path = tmp_path / "cfg.yaml"
        cfg = _base_config_dict({
            "repo": "owner/repo",
            "paths": [],
        })
        cfg["node_id"] = "Oscar's MacBook"   # spaces and apostrophe
        path.write_text(yaml.dump(cfg))
        with pytest.raises(ConfigError, match="not safe"):
            load_config(path)

    def test_effective_node_id_explicit_overrides_unsafe_fallback(
        self, tmp_path: Path
    ) -> None:
        """An explicit personalization.node_id makes the validation
        pass even when the top-level is unsafe.
        """
        path = tmp_path / "cfg.yaml"
        cfg = _base_config_dict({
            "repo": "owner/repo",
            "node_id": "macbook",
            "paths": [],
        })
        cfg["node_id"] = "Oscar's MacBook"
        path.write_text(yaml.dump(cfg))
        config = load_config(path)
        assert config.personalization_branch() == "personalization/macbook"

    def test_main_branch_validates_safe(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "main_branch": "--exec=evil",
            "paths": [],
        })))
        with pytest.raises(ConfigError):
            load_config(path)

    @pytest.mark.parametrize("bad", [
        "main:backup",   # colon — refspec separator
        "foo..bar",      # consecutive dots
        ".leading-dot",  # leading dot
        "-leading-dash", # leading dash (also looks like a git option)
        "has space",     # whitespace
        "tilde~here",    # tilde — git ref-illegal
        "caret^here",    # caret — git ref-illegal
        "with/slash",    # slash inside a single component
        "",              # empty
        "trailing.",     # trailing dot — git rejects (Codex pass 8)
        "foo.lock",      # .lock suffix — reserved by git
    ])
    def test_main_branch_rejects_git_unsafe_chars(
        self, tmp_path: Path, bad: str
    ) -> None:
        # Codex pass 7 finding: earlier denylist let things like
        # ``main:backup`` and ``foo..bar`` through; tighten to an
        # explicit allow-list matching the documented safe set.
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "main_branch": bad,
            "paths": [],
        })))
        with pytest.raises(ConfigError):
            load_config(path)

    @pytest.mark.parametrize("bad", [
        "host:name", "node..2", ".host", "-host", "name with space",
    ])
    def test_personalization_node_id_rejects_git_unsafe_chars(
        self, tmp_path: Path, bad: str
    ) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "node_id": bad,
            "paths": [],
        })))
        with pytest.raises(ConfigError):
            load_config(path)


# ---------- Layer 3: symlink wiring ------------------------------------------


def _build_config(
    *,
    checkout_path: Path,
    paths: list[dict],
    repos: list[dict] | None = None,
) -> Config:
    """Build a Config object directly (bypassing YAML) for fast tests."""
    return Config.model_validate({
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": "/tmp/state.db",
            "worktrees": "/tmp/worktrees",
            "bare_repos": "/tmp/bare",
            "contexts": "/tmp/contexts",
            "skills": "/tmp/skills",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
        },
        "dashboard": {"enabled": False},
        "personalization": {
            "repo": "owner/dotclaude",
            "checkout_path": str(checkout_path),
            "paths": paths,
        },
        "repos": repos or [],
    })


@pytest.fixture
def empty_checkout(tmp_path: Path) -> Path:
    """A directory pretending to be the personalization checkout. Not a
    real git repo — symlink wiring tests don't need git, so we skip the
    cost.
    """
    checkout = tmp_path / "personalization"
    checkout.mkdir()
    return checkout


class TestSymlinkWiring:
    def test_creates_missing_symlink(self, tmp_path: Path, empty_checkout: Path) -> None:
        # Source must exist in the checkout for wire to act on it.
        (empty_checkout / "global").mkdir()
        (empty_checkout / "global" / "CLAUDE.md").write_text("hi")

        target_dir = tmp_path / "home" / ".claude"
        target = target_dir / "CLAUDE.md"

        config = _build_config(
            checkout_path=empty_checkout,
            paths=[{"source": "global/CLAUDE.md", "target": str(target)}],
        )
        results = PersonalizationManager(config).wire_symlinks()
        assert len(results) == 1
        assert results[0].action == "created"
        assert target.is_symlink()
        assert target.readlink() == empty_checkout / "global" / "CLAUDE.md"

    def test_idempotent(self, tmp_path: Path, empty_checkout: Path) -> None:
        (empty_checkout / "global").mkdir()
        (empty_checkout / "global" / "CLAUDE.md").write_text("hi")
        target = tmp_path / "home" / ".claude" / "CLAUDE.md"
        config = _build_config(
            checkout_path=empty_checkout,
            paths=[{"source": "global/CLAUDE.md", "target": str(target)}],
        )
        mgr = PersonalizationManager(config)
        mgr.wire_symlinks()
        # Second run is a no-op.
        results = mgr.wire_symlinks()
        assert results[0].action == "already-correct"

    def test_replaces_wrong_symlink(self, tmp_path: Path, empty_checkout: Path) -> None:
        (empty_checkout / "global").mkdir()
        (empty_checkout / "global" / "CLAUDE.md").write_text("hi")
        target = tmp_path / "home" / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        wrong = tmp_path / "wrong"
        wrong.write_text("wrong")
        target.symlink_to(wrong)

        config = _build_config(
            checkout_path=empty_checkout,
            paths=[{"source": "global/CLAUDE.md", "target": str(target)}],
        )
        results = PersonalizationManager(config).wire_symlinks()
        assert results[0].action == "replaced-stale-symlink"
        assert target.readlink() == empty_checkout / "global" / "CLAUDE.md"

    def test_refuses_real_file_at_target(
        self, tmp_path: Path, empty_checkout: Path
    ) -> None:
        (empty_checkout / "global").mkdir()
        (empty_checkout / "global" / "CLAUDE.md").write_text("from-checkout")
        target = tmp_path / "home" / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("real file, do not clobber")

        config = _build_config(
            checkout_path=empty_checkout,
            paths=[{"source": "global/CLAUDE.md", "target": str(target)}],
        )
        results = PersonalizationManager(config).wire_symlinks()
        assert results[0].action == "skipped-real-file-at-target"
        assert target.is_symlink() is False
        assert target.read_text() == "real file, do not clobber"

    def test_skips_missing_source(
        self, tmp_path: Path, empty_checkout: Path
    ) -> None:
        target = tmp_path / "home" / ".claude" / "CLAUDE.md"
        config = _build_config(
            checkout_path=empty_checkout,
            paths=[{"source": "global/CLAUDE.md", "target": str(target)}],
        )
        results = PersonalizationManager(config).wire_symlinks()
        assert results[0].action == "skipped-source-missing"
        assert target.exists() is False

    def test_project_scoped_only_wires_existing_repos(
        self, tmp_path: Path, empty_checkout: Path
    ) -> None:
        # Two configured repos — one cloned locally, one not.
        cloned = tmp_path / "cloned"
        cloned.mkdir()
        not_cloned = tmp_path / "not_cloned"  # deliberately not created

        # Source dir exists in checkout for the cloned repo only.
        # ``project_slug("owner/cloned")`` is ``"owner--cloned"`` —
        # double-hyphen separator (collision-free).
        (empty_checkout / "claude-memory" / "owner--cloned").mkdir(parents=True)

        config = _build_config(
            checkout_path=empty_checkout,
            paths=[
                {
                    "source": "claude-memory/${PROJECT}/",
                    # Explicit trailing slash on the str — pathlib drops it.
                    "target": str(tmp_path / "claude-projects" / "${PROJECT_ENCODED}") + "/",
                    "project_scoped": True,
                },
            ],
            repos=[
                {"name": "owner/cloned", "local_path": str(cloned)},
                {"name": "owner/not-cloned", "local_path": str(not_cloned)},
            ],
        )
        results = PersonalizationManager(config).wire_symlinks()
        # Only the cloned repo gets a plan; not_cloned is filtered by
        # the ``local_path.exists()`` gate in ``_plan_project_scoped``.
        assert len(results) == 1
        assert results[0].action == "created"
        assert results[0].plan.repo_name == "owner/cloned"


# ---------- Layer 4: integration push/pull -----------------------------------


def _git(*args: str, cwd: Path) -> str:
    return _run_git(args, cwd=cwd, check=True).stdout


def _git_init_bare(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "--bare", "--initial-branch=main", cwd=path)
    return path


def _git_init_with_initial_commit(path: Path, *, remote_url: str) -> None:
    """Clone an empty bare repo, create main with one commit, push.

    Some git versions refuse to push a brand-new branch named ``main``
    to a totally-empty bare repo unless we set the upstream explicitly.
    Doing this in a helper isolates the dance.
    """
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "clone", remote_url, str(path)],
        check=True,
        env=env,
        capture_output=True,
    )
    # Pin author so commits are deterministic-enough for assertions.
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("checkout", "-b", "main", cwd=path)
    (path / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-m", "seed", cwd=path)
    _git("push", "-u", "origin", "main", cwd=path)


@pytest.fixture
def remote_bare(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Local bare repo standing in for the GitHub remote."""
    base = tmp_path_factory.mktemp("remote")
    bare = _git_init_bare(base / "dotclaude.git")
    # Seed the bare repo via a sidecar working clone — easier than
    # constructing the initial commit manually.
    seed_dir = base / "seed"
    _git_init_with_initial_commit(seed_dir, remote_url=str(bare))
    return bare


def _config_for(checkout: Path, remote_bare: Path, *, node_id: str) -> Config:
    """Build a Config whose personalization repo URL points at the
    local bare repo (so tests don't hit the network).
    """
    cfg = Config.model_validate({
        "version": "1",
        "node_id": node_id,
        "timezone": "UTC",
        "paths": {
            "state_db": "/tmp/state.db",
            "worktrees": "/tmp/worktrees",
            "bare_repos": "/tmp/bare",
            "contexts": "/tmp/contexts",
            "skills": "/tmp/skills",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
        },
        "dashboard": {"enabled": False},
        "personalization": {
            "repo": "test/dotclaude",   # passes the regex; URL is overridden below
            "checkout_path": str(checkout),
            "paths": [
                {
                    "source": "global/CLAUDE.md",
                    "target": str(
                        checkout.parent / "fake-home/.claude/CLAUDE.md"
                    ),
                },
            ],
        },
        "repos": [],
    })
    return cfg


def _patch_remote_url(mgr: PersonalizationManager, url: str) -> None:
    """Override the GitHub URL with our local bare-repo path so
    ``init`` clones from the test fixture.
    """
    mgr.repo_url = url


class TestPushPullIntegration:
    def test_init_clones_and_creates_per_machine_branch(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        checkout = tmp_path / "personalization"
        config = _config_for(checkout, remote_bare, node_id="machine-a")
        mgr = PersonalizationManager(config)
        _patch_remote_url(mgr, str(remote_bare))

        summary = mgr.init()
        assert "personalization/machine-a" in summary
        assert (checkout / ".git").exists()
        # On the per-machine branch.
        head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout).strip()
        assert head == "personalization/machine-a"

    def test_push_no_changes_is_noop_success(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        checkout = tmp_path / "personalization"
        config = _config_for(checkout, remote_bare, node_id="machine-a")
        mgr = PersonalizationManager(config)
        _patch_remote_url(mgr, str(remote_bare))
        mgr.init()

        # Nothing to commit — push should still succeed (just pushes
        # the empty branch update, FF main is a no-op).
        result = mgr.push(message="empty")
        assert result.success, result.summary

    def test_push_then_pull_propagates_change(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        # Machine A: init, write content, push.
        a_checkout = tmp_path / "machine-a" / "personalization"
        a_config = _config_for(a_checkout, remote_bare, node_id="machine-a")
        a_mgr = PersonalizationManager(a_config)
        _patch_remote_url(a_mgr, str(remote_bare))
        a_mgr.init()

        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "CLAUDE.md").write_text("from machine-a\n")
        # Re-wire so the symlink exists and the source is staged on push.
        a_mgr.wire_symlinks()
        push_a = a_mgr.push(message="from-a")
        assert push_a.success, push_a.summary

        # Machine B: init, expect to pick up A's content.
        b_checkout = tmp_path / "machine-b" / "personalization"
        b_config = _config_for(b_checkout, remote_bare, node_id="machine-b")
        b_mgr = PersonalizationManager(b_config)
        _patch_remote_url(b_mgr, str(remote_bare))
        b_mgr.init()
        # Pull rebases B's branch onto origin/main, which now carries A's commit.
        pull_b = b_mgr.pull()
        assert pull_b.success, pull_b.summary
        assert (b_checkout / "global" / "CLAUDE.md").read_text() == "from machine-a\n"

    def test_concurrent_writes_rebase_conflict_aborts_cleanly(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        # Both machines edit the same file before either has pulled.
        a_checkout = tmp_path / "machine-a" / "personalization"
        b_checkout = tmp_path / "machine-b" / "personalization"
        a_config = _config_for(a_checkout, remote_bare, node_id="machine-a")
        b_config = _config_for(b_checkout, remote_bare, node_id="machine-b")
        a_mgr = PersonalizationManager(a_config)
        b_mgr = PersonalizationManager(b_config)
        _patch_remote_url(a_mgr, str(remote_bare))
        _patch_remote_url(b_mgr, str(remote_bare))
        a_mgr.init()
        b_mgr.init()

        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "CLAUDE.md").write_text("from-a\n")
        (b_checkout / "global").mkdir()
        (b_checkout / "global" / "CLAUDE.md").write_text("from-b\n")

        # A pushes first → wins origin/main.
        result_a = a_mgr.push()
        assert result_a.success

        # B pushes — rebase onto origin/main now sees a conflict on
        # CLAUDE.md. Expect a clean abort with conflict_files reported.
        result_b = b_mgr.push()
        assert result_b.success is False
        assert "conflict" in result_b.summary.lower()
        assert "global/CLAUDE.md" in result_b.conflict_files
        # Working tree restored — not in the middle of a rebase.
        # The file is still there with B's content; rebase aborted.
        assert (b_checkout / "global" / "CLAUDE.md").read_text() == "from-b\n"
        # No ``rebase-merge`` directory (which would mean rebase still in progress).
        assert not (b_checkout / ".git" / "rebase-merge").exists()
        assert not (b_checkout / ".git" / "rebase-apply").exists()


class TestPushDeletionAndContention:
    """Regressions for the three Codex-review findings."""

    def test_push_stages_tracked_file_deletion(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        """A configured single-file source that was previously committed
        and is then deleted in the working tree must have its deletion
        staged and pushed. Earlier the missing-source filter skipped
        deletions, leaving a tombstone-only-on-this-machine and a
        stale file forever on the remote.
        """
        a_checkout = tmp_path / "machine-a" / "personalization"
        a_config = _config_for(a_checkout, remote_bare, node_id="machine-a")
        a_mgr = PersonalizationManager(a_config)
        _patch_remote_url(a_mgr, str(remote_bare))
        a_mgr.init()

        # Add and push a file.
        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "CLAUDE.md").write_text("v1\n")
        a_mgr.wire_symlinks()
        first = a_mgr.push(message="add v1")
        assert first.success, first.summary

        # Delete it and push the deletion.
        (a_checkout / "global" / "CLAUDE.md").unlink()
        deletion = a_mgr.push(message="delete it")
        assert deletion.success, deletion.summary

        # Verify on a second machine that the file is gone.
        b_checkout = tmp_path / "machine-b" / "personalization"
        b_config = _config_for(b_checkout, remote_bare, node_id="machine-b")
        b_mgr = PersonalizationManager(b_config)
        _patch_remote_url(b_mgr, str(remote_bare))
        b_mgr.init()
        b_mgr.pull()
        assert not (b_checkout / "global" / "CLAUDE.md").exists()

    def test_push_succeeds_after_concurrent_main_advance(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        """If origin/main advances between our fetch and our FF-push
        (a non-conflicting concurrent push from another machine), the
        retry loop must rebase onto the new tip and try again rather
        than reporting a hollow success.
        """
        # Use a DIRECTORY path so multiple files match the configured
        # source. The default ``_config_for`` only configures a single
        # file; that wouldn't stage A's and B's distinct sibling files.
        def _dir_config(checkout: Path, node_id: str) -> Config:
            return Config.model_validate({
                "version": "1",
                "node_id": node_id,
                "timezone": "UTC",
                "paths": {
                    "state_db": "/tmp/state.db",
                    "worktrees": "/tmp/worktrees",
                    "bare_repos": "/tmp/bare",
                    "contexts": "/tmp/contexts",
                    "skills": "/tmp/skills",
                },
                "transport": {
                    "type": "file_mock",
                    "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
                },
                "dashboard": {"enabled": False},
                "personalization": {
                    "repo": "test/dotclaude",
                    "checkout_path": str(checkout),
                    "paths": [
                        {
                            "source": "global/",
                            "target": str(checkout.parent / "fake-home/global") + "/",
                        },
                    ],
                },
                "repos": [],
            })

        a_checkout = tmp_path / "machine-a" / "personalization"
        b_checkout = tmp_path / "machine-b" / "personalization"
        a_mgr = PersonalizationManager(_dir_config(a_checkout, "machine-a"))
        b_mgr = PersonalizationManager(_dir_config(b_checkout, "machine-b"))
        _patch_remote_url(a_mgr, str(remote_bare))
        _patch_remote_url(b_mgr, str(remote_bare))
        a_mgr.init()
        b_mgr.init()

        # A and B touch DIFFERENT files under the configured dir (no
        # rebase conflict). Both pushes commit; B races A on FF.
        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "from-a.md").write_text("a\n")
        (b_checkout / "global").mkdir()
        (b_checkout / "global" / "from-b.md").write_text("b\n")

        result_a = a_mgr.push(message="from-a")
        assert result_a.success
        result_b = b_mgr.push(message="from-b")
        assert result_b.success, result_b.summary

        # Verify origin/main contains both commits — i.e. B's push
        # actually landed on main, not just on the per-machine branch.
        c_checkout = tmp_path / "machine-c" / "personalization"
        c_mgr = PersonalizationManager(_dir_config(c_checkout, "machine-c"))
        _patch_remote_url(c_mgr, str(remote_bare))
        c_mgr.init()
        c_mgr.pull()
        assert (c_checkout / "global" / "from-a.md").exists()
        assert (c_checkout / "global" / "from-b.md").exists()

    def test_retry_loop_recovers_from_simulated_ff_rejection(
        self, tmp_path: Path, remote_bare: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force the FF-push of main to fail once, then succeed. The
        retry loop must:
          1. detect the FF rejection,
          2. fetch the advanced main,
          3. rebase the local working branch (rewriting commits),
          4. push the per-machine branch with --force-with-lease
             (otherwise non-FF on the rewritten branch),
          5. retry the FF push of main.
        """
        a_checkout = tmp_path / "machine-a" / "personalization"
        a_config = _config_for(a_checkout, remote_bare, node_id="machine-a")
        a_mgr = PersonalizationManager(a_config)
        _patch_remote_url(a_mgr, str(remote_bare))
        a_mgr.init()
        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "CLAUDE.md").write_text("rev1\n")

        # Patch ``_git_capturing`` to simulate one FF rejection on the
        # first ``working_branch:main`` push, then let the retry
        # succeed normally. We also need to actually advance
        # origin/main between the first FF and the retry, otherwise
        # the rebase in iteration 2 is a no-op and the retried push
        # would be identical to the first attempt (and hit the same
        # patched failure). Spinning up a "phantom" commit on main is
        # cheap: push a marker commit to the bare via a sidecar clone.
        from ctrlrelay.personalization import manager as mgr_module

        ff_attempts = {"n": 0}
        original = mgr_module.PersonalizationManager._git_capturing

        # Sidecar clone we'll use to inject a concurrent commit.
        sidecar = tmp_path / "sidecar"
        _git("clone", str(remote_bare), str(sidecar), cwd=tmp_path)
        _git("config", "user.email", "x@x", cwd=sidecar)
        _git("config", "user.name", "Sidecar", cwd=sidecar)

        def patched_git_capturing(self, *args: str, check: bool = True):
            # Detect the FF push: ``push origin <branch>:<main>``.
            # The colon-form refspec only appears in the FF push
            # invocation; all other push paths use a plain branch name.
            is_ff = (
                args
                and args[0] == "push"
                and any(
                    isinstance(a, str) and a.endswith(":" + self.main_branch)
                    for a in args
                )
            )
            if is_ff and ff_attempts["n"] == 0:
                ff_attempts["n"] += 1
                # Simulate origin/main having advanced under us:
                # commit + push from the sidecar so the next fetch
                # actually sees a new main tip.
                (sidecar / "phantom.md").write_text("phantom\n")
                _git("add", "phantom.md", cwd=sidecar)
                _git("commit", "-m", "phantom advance", cwd=sidecar)
                _git("push", "origin", "main", cwd=sidecar)
                # Return a synthetic non-zero exit so the retry path
                # engages — exactly as if the real push had failed.
                import subprocess
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr=" ! [rejected]        develop -> main (non-fast-forward)\n",
                )
            return original(self, *args, check=check)

        monkeypatch.setattr(
            mgr_module.PersonalizationManager,
            "_git_capturing",
            patched_git_capturing,
        )

        result = a_mgr.push(message="rev1")
        assert result.success, result.summary
        assert "after 2 attempts" in result.summary
        # And the phantom commit didn't get clobbered.
        c_checkout = tmp_path / "verify"
        _git("clone", str(remote_bare), str(c_checkout), cwd=tmp_path)
        assert (c_checkout / "phantom.md").exists()
        assert (c_checkout / "global" / "CLAUDE.md").read_text() == "rev1\n"

    def test_init_bootstraps_empty_repo(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A brand-new GitHub-style empty bare repo (no commits) must
        be initable: clone leaves unborn HEAD, then init seeds main
        with a README commit, pushes, and proceeds normally.
        """
        base = tmp_path_factory.mktemp("empty")
        bare = _git_init_bare(base / "empty.git")  # bare, no commits

        checkout = tmp_path / "personalization"
        cfg = Config.model_validate({
            "version": "1",
            "node_id": "machine-empty",
            "timezone": "UTC",
            "paths": {
                "state_db": "/tmp/state.db",
                "worktrees": "/tmp/worktrees",
                "bare_repos": "/tmp/bare",
                "contexts": "/tmp/contexts",
                "skills": "/tmp/skills",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
            },
            "dashboard": {"enabled": False},
            "personalization": {
                "repo": "test/empty",
                "checkout_path": str(checkout),
                "paths": [],
            },
            "repos": [],
        })
        mgr = PersonalizationManager(cfg)
        mgr.repo_url = str(bare)
        summary = mgr.init()
        assert "personalization/machine-empty" in summary
        # README seeded and committed on main.
        assert (checkout / "README.md").exists()
        # On the per-machine branch, with main reachable from origin.
        head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout).strip()
        assert head == "personalization/machine-empty"
        # origin has main pushed.
        remote_main = _git(
            "ls-remote", str(bare), "main", cwd=checkout
        ).strip()
        assert remote_main, "origin/main should exist after bootstrap"

    def test_init_refuses_when_main_branch_misconfigured(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """If the remote has commits but no branch matching
        ``main_branch`` (typical when the repo is on ``master`` and
        the user wrote ``main`` in config), init must NOT silently
        create a parallel ``main`` — it must surface the
        misconfiguration so the operator can fix the config.
        """
        base = tmp_path_factory.mktemp("master-only")
        bare = _git_init_bare(base / "master-only.git")
        seed = base / "seed"
        _git_init_with_initial_commit(seed, remote_url=str(bare))
        # Push master, then flip the bare repo's HEAD so deleting
        # ``main`` on it is allowed (a bare repo refuses to delete
        # the branch its HEAD points at).
        _git("branch", "-m", "main", "master", cwd=seed)
        _git("push", "-u", "origin", "master", cwd=seed)
        _git(
            "symbolic-ref", "HEAD", "refs/heads/master",
            cwd=bare,
        )
        _git("push", "origin", "--delete", "main", cwd=seed)

        checkout = tmp_path / "personalization"
        cfg = Config.model_validate({
            "version": "1",
            "node_id": "machine-x",
            "timezone": "UTC",
            "paths": {
                "state_db": "/tmp/state.db",
                "worktrees": "/tmp/worktrees",
                "bare_repos": "/tmp/bare",
                "contexts": "/tmp/contexts",
                "skills": "/tmp/skills",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
            },
            "dashboard": {"enabled": False},
            "personalization": {
                "repo": "test/master-only",
                "checkout_path": str(checkout),
                # Wrong: remote uses "master".
                "main_branch": "main",
                "paths": [],
            },
            "repos": [],
        })
        mgr = PersonalizationManager(cfg)
        mgr.repo_url = str(bare)
        with pytest.raises(PersonalizationError, match="not empty"):
            mgr.init()

    def test_push_sets_local_identity_when_global_missing(
        self, tmp_path: Path, remote_bare: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh machine without global user.email / user.name must
        still be able to commit on push; the manager configures a
        fallback identity on the local checkout.
        """
        # Force git to ignore any system/global config so we mimic a
        # truly identity-less machine.
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("HOME", str(tmp_path / "fresh-home"))
        (tmp_path / "fresh-home").mkdir()

        checkout = tmp_path / "personalization"
        config = _config_for(checkout, remote_bare, node_id="machine-fresh")
        mgr = PersonalizationManager(config)
        _patch_remote_url(mgr, str(remote_bare))
        mgr.init()

        # Make a real change so push has something to commit.
        (checkout / "global").mkdir()
        (checkout / "global" / "CLAUDE.md").write_text("hi from fresh\n")
        result = mgr.push(message="from-fresh-machine")
        assert result.success, result.summary
        # Local identity got configured on the checkout.
        email = _git("config", "--get", "user.email", cwd=checkout).strip()
        assert email == "ctrlrelay@local"

    def test_push_recovers_when_origin_branch_ahead_of_main(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        """A previous invocation may have left
        ``origin/personalization/<node>`` updated while
        ``origin/main`` did NOT fast-forward (interrupted, retry
        budget exhausted, etc.). The next push must rebase locally
        and force-with-lease the per-machine branch — earlier the
        first attempt was a plain push that hit non-FF rejection on
        the per-machine branch and exited before retry-2 engaged
        force-with-lease. Codex review pass 9 caught this.
        """
        # Bring the world into the broken state: A pushes successfully,
        # then a sidecar advances origin/main, leaving A's local
        # branch out of date with origin/main but A's branch on origin
        # still at its old SHA (i.e. origin/personalization/A is
        # behind both origin/main AND what A's local branch will
        # become after rebase).
        a_checkout = tmp_path / "machine-a" / "personalization"
        a_config = _config_for(a_checkout, remote_bare, node_id="machine-a")
        a_mgr = PersonalizationManager(a_config)
        _patch_remote_url(a_mgr, str(remote_bare))
        a_mgr.init()
        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "CLAUDE.md").write_text("rev1\n")
        first = a_mgr.push(message="rev1")
        assert first.success, first.summary

        # Sidecar push to advance origin/main without touching A's
        # per-machine branch. After this:
        #   origin/main = rev1 + sidecar
        #   origin/personalization/machine-a = rev1
        sidecar = tmp_path / "sidecar"
        _git("clone", str(remote_bare), str(sidecar), cwd=tmp_path)
        _git("config", "user.email", "x@x", cwd=sidecar)
        _git("config", "user.name", "Sidecar", cwd=sidecar)
        (sidecar / "advance.md").write_text("advance\n")
        _git("add", "advance.md", cwd=sidecar)
        _git("commit", "-m", "advance main", cwd=sidecar)
        _git("push", "origin", "main", cwd=sidecar)

        # Now A wants to make another change. With the bug, the
        # rebase at the start of push() rewrites A's commit, plain
        # push to per-machine branch is non-FF and fails on attempt
        # 1, and the function exits before retry-2 can engage
        # force-with-lease.
        (a_checkout / "global" / "CLAUDE.md").write_text("rev2\n")
        result = a_mgr.push(message="rev2")
        assert result.success, result.summary
        # All three commits should now be on origin/main: rev1, the
        # sidecar advance, and rev2.
        verify = tmp_path / "verify"
        _git("clone", str(remote_bare), str(verify), cwd=tmp_path)
        assert (verify / "advance.md").exists()
        assert (verify / "global" / "CLAUDE.md").read_text() == "rev2\n"

    def test_push_does_not_commit_pre_staged_outside_allowlist(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        """If something is pre-staged in the index OUTSIDE the
        configured allowlist (operator manually ran ``git add``, an
        interrupted previous run, etc.), push must NOT commit it.
        Codex pass 12 finding: an unscoped ``git commit`` would
        publish unrelated/private files alongside legitimate
        configured changes. The commit must be scoped to the
        configured pathspecs.
        """
        a_checkout = tmp_path / "machine-a" / "personalization"
        a_config = _config_for(a_checkout, remote_bare, node_id="machine-a")
        a_mgr = PersonalizationManager(a_config)
        _patch_remote_url(a_mgr, str(remote_bare))
        a_mgr.init()

        # Allowlisted change (will be committed):
        (a_checkout / "global").mkdir()
        (a_checkout / "global" / "CLAUDE.md").write_text("public\n")

        # NON-allowlisted file pre-staged (must NOT be committed):
        (a_checkout / "secrets.txt").write_text("leak-me-not\n")
        _git("add", "secrets.txt", cwd=a_checkout)

        result = a_mgr.push(message="legit-change")
        assert result.success, result.summary

        # Verify on a fresh clone that secrets.txt did NOT travel.
        verify = tmp_path / "verify"
        _git("clone", str(remote_bare), str(verify), cwd=tmp_path)
        assert (verify / "global" / "CLAUDE.md").exists()
        assert (verify / "global" / "CLAUDE.md").read_text() == "public\n"
        assert not (verify / "secrets.txt").exists()

    def test_init_works_with_non_default_main_branch(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """When ``personalization.main_branch`` is not the repo's
        default (which after ``git clone`` is the only locally-
        checked-out branch), init must branch from ``origin/<main>``,
        not the bare name.
        """
        # Build a remote whose default is "main" but which also has a
        # "develop" branch carrying initial content.
        base = tmp_path_factory.mktemp("alt-main")
        bare = _git_init_bare(base / "alt.git")
        seed = base / "seed"
        _git_init_with_initial_commit(seed, remote_url=str(bare))
        # Create a 'develop' branch with its own commit and push it.
        _git("checkout", "-b", "develop", cwd=seed)
        (seed / "develop-marker.md").write_text("on develop\n")
        _git("add", "develop-marker.md", cwd=seed)
        _git("commit", "-m", "develop init", cwd=seed)
        _git("push", "-u", "origin", "develop", cwd=seed)

        checkout = tmp_path / "personalization"
        cfg = Config.model_validate({
            "version": "1",
            "node_id": "machine-x",
            "timezone": "UTC",
            "paths": {
                "state_db": "/tmp/state.db",
                "worktrees": "/tmp/worktrees",
                "bare_repos": "/tmp/bare",
                "contexts": "/tmp/contexts",
                "skills": "/tmp/skills",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
            },
            "dashboard": {"enabled": False},
            "personalization": {
                "repo": "test/dotclaude",
                "checkout_path": str(checkout),
                "main_branch": "develop",
                "paths": [],
            },
            "repos": [],
        })
        mgr = PersonalizationManager(cfg)
        mgr.repo_url = str(bare)
        mgr.init()
        # On the per-machine branch...
        head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout).strip()
        assert head == "personalization/machine-x"
        # ...and reachable from develop's content (the marker file).
        assert (checkout / "develop-marker.md").exists()


class TestManagerErrors:
    def test_init_rejected_when_path_exists_and_not_ours(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        checkout = tmp_path / "personalization"
        checkout.mkdir()
        (checkout / "stranger").write_text("not a clone")

        config = _config_for(checkout, remote_bare, node_id="machine-a")
        mgr = PersonalizationManager(config)
        _patch_remote_url(mgr, str(remote_bare))
        with pytest.raises(PersonalizationError, match="back it up"):
            mgr.init()

    def test_init_rejects_prefix_matched_existing_clone(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Earlier ``_is_existing_checkout_ours`` used ``in`` against
        the origin URL — so if config repo was ``acme/dot`` and the
        existing clone's origin was ``acme/dotfiles``, init would
        wrongly converge against the foreign clone. Codex pass 6
        caught this.
        """
        base = tmp_path_factory.mktemp("dotfiles")
        bare = _git_init_bare(base / "dotfiles.git")
        seed = base / "seed"
        _git_init_with_initial_commit(seed, remote_url=str(bare))

        # Pre-existing clone of acme/dotfiles at the configured path.
        checkout = tmp_path / "personalization"
        _git("clone", str(bare), str(checkout), cwd=tmp_path)
        # Rewrite origin to the prefix-collision URL so the check has
        # something to match against (test uses local bare; we
        # spoof the remote name).
        _git(
            "remote", "set-url", "origin",
            "https://github.com/acme/dotfiles.git",
            cwd=checkout,
        )

        cfg = Config.model_validate({
            "version": "1",
            "node_id": "machine-x",
            "timezone": "UTC",
            "paths": {
                "state_db": "/tmp/state.db",
                "worktrees": "/tmp/worktrees",
                "bare_repos": "/tmp/bare",
                "contexts": "/tmp/contexts",
                "skills": "/tmp/skills",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
            },
            "dashboard": {"enabled": False},
            "personalization": {
                # Config repo is prefix of the existing clone's repo.
                "repo": "acme/dot",
                "checkout_path": str(checkout),
                "paths": [],
            },
            "repos": [],
        })
        mgr = PersonalizationManager(cfg)
        with pytest.raises(PersonalizationError, match="not a clone"):
            mgr.init()

    def test_init_clones_into_empty_directory(
        self, tmp_path: Path, remote_bare: Path
    ) -> None:
        """If checkout_path exists as an empty directory (e.g. created
        by provisioning), init should clone into it rather than
        refuse. Codex pass 6 caught this.
        """
        checkout = tmp_path / "personalization"
        checkout.mkdir()  # empty
        config = _config_for(checkout, remote_bare, node_id="machine-empty-dir")
        mgr = PersonalizationManager(config)
        _patch_remote_url(mgr, str(remote_bare))
        # Should NOT raise: empty dir is fine for clone target.
        mgr.init()
        assert (checkout / ".git").exists()

    def test_init_recovers_existing_empty_clone(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """If checkout_path is already a clone of the configured repo
        but the remote is still empty (e.g. the user manually cloned
        and then ran init, or a previous init was interrupted right
        after ``git clone``), the converge-existing path must
        bootstrap rather than blow up trying to rev-parse an unborn
        HEAD. Codex pass 10 finding.
        """
        base = tmp_path_factory.mktemp("empty-clone")
        bare = _git_init_bare(base / "empty.git")
        # Manually clone (no commits) — simulates the interrupted state.
        checkout = tmp_path / "personalization"
        _git("clone", str(bare), str(checkout), cwd=tmp_path)

        cfg = Config.model_validate({
            "version": "1",
            "node_id": "machine-recover",
            "timezone": "UTC",
            "paths": {
                "state_db": "/tmp/state.db",
                "worktrees": "/tmp/worktrees",
                "bare_repos": "/tmp/bare",
                "contexts": "/tmp/contexts",
                "skills": "/tmp/skills",
            },
            "transport": {
                "type": "file_mock",
                "file_mock": {"inbox": "/tmp/in", "outbox": "/tmp/out"},
            },
            "dashboard": {"enabled": False},
            "personalization": {
                "repo": "test/empty",
                "checkout_path": str(checkout),
                "paths": [],
            },
            "repos": [],
        })
        # Spoof the origin URL to satisfy ``_is_existing_checkout_ours``.
        _git(
            "remote", "set-url", "origin",
            "https://github.com/test/empty.git",
            cwd=checkout,
        )
        # ...but real fetch/clone target is the local bare.
        # We re-set origin AGAIN to the local bare so push/clone in
        # bootstrap can actually reach it. The is-ours check uses the
        # parsed owner/repo from the URL, so we use a dual approach:
        # set the remote URL to the local path and also set a
        # ``url.<base>.insteadOf`` rewrite so the OWNER/REPO check
        # still passes. Simpler: set the URL to a github-shaped URL
        # via insteadOf and let the actual operations follow it to
        # the local bare. But _is_existing_checkout_ours reads
        # remote.origin.url directly — it doesn't follow insteadOf.
        # So set origin to the local bare path AND ensure the
        # checkout-ours check considers it ours by virtue of the
        # config repo being a sentinel that matches the local URL
        # tail. Simplest: override the URL parsing for the test. The
        # cleanest approach is to set origin to an URL whose tail
        # matches "test/empty" (the configured repo) and use a git
        # ``url.X.insteadOf`` rewrite so operations against that URL
        # transparently go to the bare.
        _git(
            "remote", "set-url", "origin", str(bare), cwd=checkout
        )
        mgr = PersonalizationManager(cfg)
        # Override the URL extraction since our local bare path
        # doesn't have an ``owner/repo`` shape — patch the regex
        # check to recognize this as ours for test purposes.
        mgr._is_existing_checkout_ours = lambda: True  # type: ignore[method-assign]
        summary = mgr.init()
        assert "personalization/machine-recover" in summary
        # Bootstrap ran: README on main, our per-machine branch
        # branched off main.
        assert (checkout / "README.md").exists()
        head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout).strip()
        assert head == "personalization/machine-recover"

    def test_status_works_before_init(self, tmp_path: Path, remote_bare: Path) -> None:
        # status should not raise when the checkout doesn't exist yet —
        # it should print a friendly message that a wired CLI can show.
        checkout = tmp_path / "personalization"  # not created
        config = _config_for(checkout, remote_bare, node_id="machine-a")
        mgr = PersonalizationManager(config)
        msg = mgr.status()
        assert "does not exist" in msg

    def test_push_before_init_errors(self, tmp_path: Path, remote_bare: Path) -> None:
        checkout = tmp_path / "personalization"
        config = _config_for(checkout, remote_bare, node_id="machine-a")
        mgr = PersonalizationManager(config)
        with pytest.raises(PersonalizationError, match="no checkout"):
            mgr.push()


class TestCLI:
    """Smoke tests for the Typer wrappers around the manager. Verifies
    that ``personalization push`` / ``pull`` (Codex pass 10 finding)
    catch ``PersonalizationError`` and exit cleanly instead of
    propagating a traceback."""

    def test_push_before_init_returns_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from ctrlrelay.cli import app

        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "checkout_path": str(tmp_path / "checkout"),  # absent → error
            "paths": [],
        })))
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["personalization", "push", "--config", str(cfg_path)],
        )
        assert result.exit_code == 1
        # No traceback in output; the manager's actionable message wins.
        assert "Traceback" not in result.output
        assert "no checkout" in result.output.lower()

    def test_pull_before_init_returns_exit_1(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from ctrlrelay.cli import app

        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(yaml.dump(_base_config_dict({
            "repo": "owner/repo",
            "checkout_path": str(tmp_path / "checkout"),
            "paths": [],
        })))
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["personalization", "pull", "--config", str(cfg_path)],
        )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
