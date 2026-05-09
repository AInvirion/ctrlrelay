"""First-run setup: detect orgs, enumerate repos, write config, clone, optional daemon install.

Composes the building blocks operators previously had to wire together by
hand: gh-based owner discovery, per-owner repo enumeration with
archive/fork filters, ``orchestrator.yaml`` generation, per-repo git
clone using the same path resolver the dev pipeline uses, optional
personalization wiring, and optional launchd/systemd unit install.

The CLI wrapper in ``cli.py`` owns the interactive prompts. This module
takes a fully-resolved :class:`SetupOptions` and runs the work.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctrlrelay.core.config import Config, load_config

__all__ = [
    "SetupOptions",
    "SetupResult",
    "GhAuthError",
    "VALID_TRANSPORTS",
    "DEFAULT_CONFIG_OUT",
    "detect_owners",
    "list_repos",
    "build_orchestrator_yaml",
    "clone_repos",
    "run_setup",
]


# Mirrors the keys accepted by ``TransportConfig.type`` in the schema.
# Keep these in sync if a new transport adapter ships.
VALID_TRANSPORTS = ("file_mock", "telegram")

# Default destination for ``--config-out``. The daemon plists shipped
# in ``src/ctrlrelay/templates/launchd|systemd/`` rely on the config
# being at one of the standard auto-discovery locations; pointing the
# operator at a non-default path while also installing daemons would
# orphan them, so setup refuses that combo (see ``run_setup``).
DEFAULT_CONFIG_OUT = Path("~/.config/ctrlrelay/orchestrator.yaml").expanduser()


class GhAuthError(RuntimeError):
    """Raised when ``gh auth status`` fails — setup needs an authenticated gh CLI."""


@dataclass
class SetupOptions:
    """Resolved choices for one ``ctrlrelay setup`` run.

    The CLI fills these from a mix of flags and interactive prompts; the
    setup logic doesn't care which path each value came from.
    """

    owners: list[str] = field(default_factory=list)
    skip_archived: bool = True
    skip_forks: bool = True
    repo_root: Path = field(
        default_factory=lambda: Path("~/Projects").expanduser()
    )
    config_out: Path = field(
        default_factory=lambda: Path("~/.config/ctrlrelay/orchestrator.yaml").expanduser()
    )
    timezone: str = "UTC"
    transport: str = "file_mock"  # or "telegram"
    telegram_chat_id: int | None = None
    telegram_token: str | None = None  # only used when transport == "telegram"
    personalization_repo: str | None = None  # e.g. "alice/dotclaude"
    install_daemons: bool = False
    force: bool = False
    skip_clone: bool = False  # for tests / dry-run scenarios


@dataclass
class SetupResult:
    """End-state summary returned by :func:`run_setup`."""

    config_out: Path
    owners: list[str]
    n_repos: int
    cloned: int
    skipped: int
    failed: int
    personalization_summary: str | None = None
    daemon_units: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# gh helpers


def _run_gh(args: list[str]) -> str:
    """Run ``gh`` and return stdout. Captures stderr into the raised error."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def assert_gh_auth() -> None:
    """Refuse to proceed without an authenticated gh CLI.

    ``gh auth status`` writes its result to stderr (success or failure), so we
    just check the exit code rather than parsing the output.
    """
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhAuthError(
            "gh CLI is not authenticated. Run `gh auth login` and retry. "
            f"(gh auth status: {proc.stderr.strip() or proc.stdout.strip()})"
        )


def detect_owners() -> list[str]:
    """Return ``[user_login, *org_logins]`` for the authenticated gh user.

    The user's own login is included so personal-account repos are an
    obvious option in the setup flow alongside any orgs they belong to.
    """
    user = _run_gh(["api", "user", "--jq", ".login"]).strip()
    orgs_raw = _run_gh(
        ["api", "user/orgs", "--paginate", "--jq", ".[].login"]
    ).strip()
    orgs = [o for o in orgs_raw.splitlines() if o]
    return [user, *orgs]


def list_repos(
    owner: str, *, skip_archived: bool = True, skip_forks: bool = True
) -> list[dict]:
    """Return non-empty repos for ``owner``, applying the chosen filters.

    Empty repos (no default branch) are always excluded — they can't be
    cloned meaningfully, so adding them to ``repos:`` would just produce
    poller errors.
    """
    args = [
        "repo", "list", owner,
        "--limit", "1000",
        "--json", "nameWithOwner,isFork,isEmpty,defaultBranchRef",
    ]
    if skip_archived:
        args.append("--no-archived")
    if skip_forks:
        # Exclude forks at the API level too. Without --source, gh
        # returns forks; we'd still filter them in the loop below, but
        # asking gh to omit them up-front halves the response payload
        # for accounts with lots of fork noise.
        args.append("--source")
    raw = _run_gh(args)
    data = json.loads(raw or "[]")
    result: list[dict] = []
    for r in data:
        if skip_forks and r.get("isFork"):
            continue
        if r.get("isEmpty") or not r.get("defaultBranchRef"):
            continue
        result.append(r)
    return sorted(result, key=lambda r: r["nameWithOwner"].lower())


# ---------------------------------------------------------------------------
# config generation


def build_orchestrator_yaml(
    options: SetupOptions, repos_by_owner: dict[str, list[dict]]
) -> str:
    """Render the orchestrator.yaml as a string (no pyyaml — we want full
    control over comments and key order).
    """
    lines: list[str] = []
    a = lines.append

    a("# ctrlrelay orchestrator configuration")
    a("# Generated by `ctrlrelay setup`. Edit freely; re-running setup")
    a("# refuses to overwrite without --force.")
    a("")
    a('version: "1"')
    a(f'timezone: "{options.timezone}"')
    a("")
    a("paths:")
    a('  state_db: "~/.ctrlrelay/state.db"')
    a('  worktrees: "~/.ctrlrelay/worktrees"')
    a('  bare_repos: "~/.ctrlrelay/repos"')
    a('  contexts: "~/.ctrlrelay/contexts"')
    a('  skills: "~/.claude/skills"')
    a(f'  repo_root: "{_yaml_escape(str(options.repo_root))}"')
    a("")
    a("agent:")
    a('  type: "claude"')
    a('  binary: "claude"')
    a("  default_timeout_seconds: 1800")
    a('  output_format: "json"')
    a("")
    a("transport:")
    if options.transport == "telegram":
        a('  type: "telegram"')
        a("  telegram:")
        a('    bot_token_env: "CTRLRELAY_TELEGRAM_TOKEN"')
        a(f"    chat_id: {options.telegram_chat_id or 0}")
        a('    socket_path: "~/.ctrlrelay/ctrlrelay.sock"')
    else:
        a('  type: "file_mock"')
        a("  file_mock:")
        a('    inbox: "~/.ctrlrelay/inbox.txt"')
        a('    outbox: "~/.ctrlrelay/outbox.txt"')
    a("")
    a("dashboard:")
    a("  enabled: false")
    a("")
    a("schedules:")
    a('  secops_cron: "0 6 * * *"')
    a("")
    if options.personalization_repo:
        a("personalization:")
        a(f'  repo: "{options.personalization_repo}"')
        a("  paths:")
        a('    - source: "global/CLAUDE.md"')
        a('      target: "~/.claude/CLAUDE.md"')
        a("")
    a("# Repos discovered via `gh repo list`. Filters applied:")
    a(
        f"# skip_archived={options.skip_archived}, "
        f"skip_forks={options.skip_forks}. Empty repos always skipped."
    )
    total_repos = sum(len(rs) for rs in repos_by_owner.values())
    if total_repos == 0:
        # An empty mapping must serialize as ``repos: []``. A bare
        # ``repos:`` with only comment children parses as null and
        # the Pydantic schema rejects it.
        a("repos: []")
    else:
        a("repos:")
        for owner in options.owners:
            repos = repos_by_owner.get(owner, [])
            a(f"  # --- {owner} ({len(repos)} repo(s)) ---")
            for r in repos:
                a(f'  - name: "{r["nameWithOwner"]}"')
                a("    automation:")
                a("      dependabot_patch: auto")
                a("      dependabot_minor: ask")
                a("      dependabot_major: never")
    return "\n".join(lines) + "\n"


def _yaml_escape(value: str) -> str:
    """Minimal escape for double-quoted YAML scalars."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# clone


def clone_repos(config: Config) -> tuple[int, int, int]:
    """Clone every configured repo to its resolved ``local_path``.

    Returns ``(cloned, skipped, failed)``. Idempotent: a repo whose
    target already has a ``.git`` is treated as already-cloned and
    counted under ``skipped``.
    """
    cloned = skipped = failed = 0
    for r in config.repos:
        target = Path(str(r.local_path)).expanduser()
        if (target / ".git").is_dir():
            skipped += 1
            continue
        if target.exists() and any(target.iterdir()):
            failed += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--quiet", f"git@github.com:{r.name}.git", str(target)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            cloned += 1
        else:
            failed += 1
    return cloned, skipped, failed


# ---------------------------------------------------------------------------
# top-level driver


def run_setup(options: SetupOptions) -> SetupResult:
    """Execute the full setup flow against the given options.

    Order:
    1. Refuse if ``config_out`` exists and ``force`` is False — protects
       a hand-tuned operator config from being clobbered.
    2. Verify ``gh`` is authenticated.
    3. Per owner: enumerate repos via ``gh repo list``.
    4. Render and write ``orchestrator.yaml``.
    5. Validate by reloading through the Pydantic schema.
    6. Clone every repo to its resolved ``local_path`` (skipped when
       ``options.skip_clone`` is set).
    7. Optionally call ``personalization init`` if a repo was set.
    8. Optionally render and write daemon unit files.
    """
    if options.transport not in VALID_TRANSPORTS:
        # Reject mistypes loudly. Pre-fix, an unknown transport silently
        # fell through to the file_mock branch in build_orchestrator_yaml,
        # so an operator typing ``--transport telegrm`` would land a
        # file_mock config without realising. Codex review pass 2 caught
        # this — the lookup must match TransportConfig.type's enum.
        raise ValueError(
            f"unknown transport {options.transport!r}; "
            f"expected one of {', '.join(VALID_TRANSPORTS)}"
        )
    if options.install_daemons and options.config_out != DEFAULT_CONFIG_OUT:
        # The shipped launchd/systemd templates don't carry a
        # CTRLRELAY_CONFIG environment variable. A daemon started from
        # them auto-discovers via the default search path, which
        # wouldn't find a config dropped at e.g. ``/srv/ctrlrelay.yaml``.
        # Refuse the combo so the operator splits the steps explicitly.
        # Codex review pass 2 caught this orphan-daemon footgun.
        raise ValueError(
            f"--install-daemons requires the default --config-out "
            f"({DEFAULT_CONFIG_OUT}); got {options.config_out}. Either "
            "drop --config-out or run `ctrlrelay install launchd|systemd` "
            "manually after editing the rendered plists to set "
            "CTRLRELAY_CONFIG."
        )
    if options.config_out.exists() and not options.force:
        raise FileExistsError(
            f"{options.config_out} already exists; pass --force to overwrite"
        )
    assert_gh_auth()

    if not options.owners:
        raise ValueError("setup requires at least one owner")

    repos_by_owner: dict[str, list[dict]] = {}
    for owner in options.owners:
        repos_by_owner[owner] = list_repos(
            owner, skip_archived=options.skip_archived, skip_forks=options.skip_forks
        )

    yaml_text = build_orchestrator_yaml(options, repos_by_owner)
    options.config_out.parent.mkdir(parents=True, exist_ok=True)
    options.config_out.write_text(yaml_text)

    config = load_config(options.config_out)

    cloned = skipped = failed = 0
    if not options.skip_clone:
        cloned, skipped, failed = clone_repos(config)

    personalization_summary: str | None = None
    if options.personalization_repo:
        from ctrlrelay.personalization import PersonalizationManager
        from ctrlrelay.personalization.manager import PersonalizationError

        try:
            mgr = PersonalizationManager(config)
            personalization_summary = mgr.init(adopt=True)
        except PersonalizationError as e:
            personalization_summary = f"(failed: {e})"

    daemon_units: list[Path] = []
    if options.install_daemons:
        daemon_units = _install_daemons(options)

    return SetupResult(
        config_out=options.config_out,
        owners=list(options.owners),
        n_repos=sum(len(rs) for rs in repos_by_owner.values()),
        cloned=cloned,
        skipped=skipped,
        failed=failed,
        personalization_summary=personalization_summary,
        daemon_units=daemon_units,
    )


def _install_daemons(options: SetupOptions) -> list[Path]:
    """Render and write launchd or systemd unit files based on platform.

    Sets ``CTRLRELAY_TELEGRAM_TOKEN`` from ``options.telegram_token`` so
    the rendered unit doesn't carry the literal ``${...}`` placeholder.
    Returns the list of paths written so the CLI can print them.
    """
    import sys

    from ctrlrelay.install import render_launchd, render_systemd, write_units

    # Restore the token to the env so render_* picks it up. We don't
    # leak it back to the caller's env — restore the prior value at the
    # end so a parent shell that was already exporting a different
    # value isn't disturbed.
    prior_token = os.environ.get("CTRLRELAY_TELEGRAM_TOKEN")
    if options.transport == "telegram" and options.telegram_token:
        os.environ["CTRLRELAY_TELEGRAM_TOKEN"] = options.telegram_token

    # Use the operator's home as the daemon working directory. Stable,
    # always exists, doesn't tie service lifetime to a particular project
    # checkout. Operators who want a different workdir can re-run
    # `ctrlrelay install launchd --workdir <path> --force` later.
    workdir = Path.home()
    try:
        if sys.platform == "darwin":
            units = render_launchd(
                workdir=workdir, label_prefix="com.ctrlrelay"
            )
        else:
            units = render_systemd(workdir=workdir)
        written = write_units(units, overwrite=options.force)
    finally:
        if prior_token is None:
            os.environ.pop("CTRLRELAY_TELEGRAM_TOKEN", None)
        else:
            os.environ["CTRLRELAY_TELEGRAM_TOKEN"] = prior_token

    return written
