# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Poller: filter out issues where the most recent assignment to the operator
  was performed by someone else (e.g. a teammate or a bot). Foreign
  assignments are logged as `poll.issue.foreign_assignment` and marked seen
  so they aren't re-checked. Repos can opt back into the old "any assignment
  counts" behaviour with `automation.accept_foreign_assignments: true`.

### Fixed

- **Dispatcher: `--resume` now uses Claude's session UUID, not our composite
  id** (#83). Newer `claude` CLI versions (v2.0.x+) validate that `--resume
  <id>` is either a real UUID or a known session title, so passing our
  `dev-<owner>-<repo>-<issue>-<hex>` composite id hard-failed on every
  resume — blocked-question answers and post-DONE fix rounds both surfaced
  as misleading "❌ Failed" notifications. `ClaudeDispatcher` now parses
  `session_id` out of Claude's JSON stdout, exposes it as
  `SessionResult.agent_session_id`, and persists it in a new
  `sessions.agent_session_id` column so subsequent resumes feed the real
  UUID. If state_db has no UUID (session predates the fix, or stdout
  wasn't JSON), pipelines fall back to a fresh spawn rather than failing.

## [0.1.5] - 2026-04-20

The "ready to be open-sourced" release. Apache-2.0 license, AInvirion
governance files, CLA Assistant wired against the org's reusable
workflow, PyPI publish workflow via Trusted Publishing (OIDC, no
secret rotation). Plus the foundation for plugging in alternative
coding-agent backends (Codex, OpenCode, Hermes, Kiro …) in follow-up
PRs without touching pipelines or callers. **First release published
to PyPI.**

### Added

- **Multi-agent config surface** (#73). Top-level config section
  renamed `claude:` → `agent:` with a new `type:` field selecting
  the backend adapter. Today only `"claude"` is implemented; the
  new `AgentAdapter` protocol in `src/ctrlrelay/core/dispatcher.py`
  defines the seam, and `make_agent_dispatcher()` is the factory.
  Unknown `type` values raise `NotImplementedError` at daemon
  startup with a clear hint instead of silently falling back.
- **PyPI publish workflow** (#64). New `.github/workflows/publish.yml`
  fires on `release: published` and ships sdist + wheel to PyPI via
  OIDC Trusted Publishing — no API tokens stored as GitHub secrets.
  Gated by a `pypi` GitHub Environment so each release pauses for
  manual approval.
- **AInvirion OSS governance files** (#65). `CODE_OF_CONDUCT.md`,
  `CONTRIBUTING.md` (Python-adapted), `.github/PULL_REQUEST_TEMPLATE.md`,
  `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.md`,
  `.github/dependabot.yml` (github-actions + pip ecosystems weekly),
  and `.github/workflows/cla.yml` (delegates to AInvirion's org-wide
  reusable CLA Assistant workflow).
- **`SECURITY.md`** with private-advisory reporting flow + SLA
  targets (#63).
- **Per-repo Telegram notifications for scheduled secops** (#74).
  Scheduled-sweep starts emit a 🔄 message; each blocked / failed
  result fans out a per-repo message with the actual question
  (or error) and session id; final aggregate summary kept for
  at-a-glance scan. Fixes the previous "⏸️ Scheduled secops: 2
  run(s) blocked on user input" with no repo names or question
  text.
- **CLI surfaces blocking question** (#75). `ctrlrelay run secops`
  now prints `Question:` (yellow) when blocked and `Error:` (red)
  when failed-but-not-blocked, mirroring the scheduled-path
  Telegram fan-out.
- **`[project.urls]`** in `pyproject.toml` — Homepage, Documentation,
  Repository, Issues, Changelog (PyPI sidebar links). Broader
  classifiers (Python 3.13 + 3.14, macOS + Linux, Systems
  Administration, Git) (#63).

### Changed

- **License: MIT → Apache-2.0** (#65). Matches the AInvirion OSS
  template. Includes `Copyright (c) 2026 AInvirion LLC. All Rights
  Reserved.` Wheel metadata: `License-Expression: Apache-2.0`,
  `License-File: LICENSE`.
- **README repositioned for multi-agent** (#65). Tagline changed
  from "Local-first orchestrator for Claude Code…" to
  "…for headless coding agents…". New "Roadmap" section calls out
  Codex / OpenCode / Hermes / Kiro as planned backends.
  Prerequisites adds the `codex` CLI as an optional dependency for
  the secops review step (was undocumented).
- **`claude:` config key deprecated** (#73). The legacy YAML key
  is still accepted as an alias (with a `DeprecationWarning` at
  load), and `config.claude` works as a Python property mirroring
  `config.agent`. Both removed in a future release; rename your
  `orchestrator.yaml` at your convenience.
- **CI workflow uses an explicit venv** (#68). `setup-uv` v5 → v7
  required this — v7 dropped the implicit auto-venv and Ubuntu's
  PEP-668 system Python refuses `--system`. Added a `uv venv`
  step before `uv pip install`.

### Removed

- **Tracked operator state**: `config/orchestrator.yaml` and
  `repos.manifest` (#63). Both contained personal Telegram
  `chat_id`, private-org repo lists, and local paths. Untracked
  via `git rm --cached`; only the `.example` ships publicly. Both
  added to `.gitignore`.
- **`uv.lock`** (#66). AInvirion Python-SDK convention: libraries
  published to PyPI don't pin a resolver lock; consumers resolve
  against declared ranges in `pyproject.toml`.

### Security audit

`git log --all` was scanned for Telegram bot tokens, AWS access
keys, GitHub PATs, and Slack tokens — **zero matches, ever**. The
live bot token only lives in `~/Library/LaunchAgents/*.plist`,
outside the repo. No history rewrite needed.

### Dependency updates

Six GitHub Actions bumps from Dependabot, all major versions:
`actions/checkout` 4 → 6 (#71), `actions/upload-artifact` 4 → 7
(#69, paired with), `actions/download-artifact` 4 → 8 (#67),
`actions/configure-pages` 5 → 6 (#72), `actions/deploy-pages`
4 → 5 (#70), `astral-sh/setup-uv` 5 → 7 (#68).

## [0.1.4] - 2026-04-20

Big day: daemon UX, a security fix, and the scheduler finally lands. The
poller is now a real service — it forks and returns the terminal, owns a
PID file that `status`/`stop` can actually find, and hosts an in-process
cron that runs `secops` daily at 6am (the design called for this in
Phase 3; Phase 3 shipped manual-only). End-to-end verified live:
scheduler fired at the exact cron minute, 6 repos swept in 2m32s.

### Added

- **In-process scheduler hosted by the poller daemon** (APScheduler).
  Registered today: `secops` at `0 6 * * *` in the config timezone,
  matching the original design spec that was never shipped in Phase 3
  (PR #60). Configurable via a new top-level `schedules:` section in
  `orchestrator.yaml`:

  ```yaml
  schedules:
    secops_cron: "0 6 * * *"   # override to e.g. "0 6 * * 1" for weekly
  ```

  Invalid cron expressions fail at config-load time. Vixie cron DOW
  semantics (0=Sun, 7=Sun-alias) and DOM/DOW-OR semantics are normalized
  so standard 5-field expressions behave the way every reference
  describes, independent of APScheduler's quirks. Misfires coalesce
  with a 1-hour grace window, so a laptop asleep at the fire time still
  runs the job on wake without replaying missed fires. Cross-platform:
  the scheduler runs inside the poller's asyncio loop, so macOS
  (launchd) and Linux (systemd) behave identically — no per-OS timer
  unit required.

  Operator-facing note: the in-process scheduler needs up to 150s to
  drain a running secops sweep on stop. The supervisor's stop timeout
  must cover that — the example launchd plist and systemd unit in
  `docs/operations.md` now set `ExitTimeOut=180` / `TimeoutStopSec=180`
  respectively (PR #61). If you're upgrading from an older unit file,
  add those fields before restarting.

### Changed

- **Issue-claim comment on picked-up issues now signs as
  `CTRLRelay`** instead of `🤖 Agent` (PR #59), so the tool identifies
  itself by name on the issues it claims.

- **`ctrlrelay bridge start` / `ctrlrelay poller start` now daemonize by
  default** and return to the shell immediately, writing a PID file so
  `status`/`stop` can find the process. This matches the normal "start a
  service" UX expectation and fixes the previous behavior where the
  terminal would block until Ctrl+C.
- Added `--foreground` / `-F` flag to both commands. Under a process
  supervisor (launchd, systemd) pass this so the supervisor — not the
  CLI's own fork — owns the long-lived PID. Existing supervisor unit
  files must be updated to include this flag; examples in
  `docs/operations.md` are revised.
- `ctrlrelay bridge status` and `poller status` rely on the PID file as
  before. If a supervisor runs the old (pre-`--foreground`) command, no
  PID file is written and `status` now prints a migration hint instead
  of a misleading "not running".
- The deprecated `--daemon` / `-d` flag has been removed; the daemonize
  behavior is now the default. Scripts using `--daemon` will need to
  drop the flag.

### Security

- **Telegram bot token is no longer passed via subprocess argv.** Before
  this release, daemon-mode `bridge start` invoked the child with
  `--bot-token <TOKEN>` on the command line, exposing the secret to any
  local user or tool that reads `ps` / `/proc/*/cmdline`. The daemon
  parent now tells the child which env var to read (`--bot-token-env`)
  and relies on the inherited process environment instead. Since
  daemonize mode is now the default, this would have leaked the token on
  every vanilla `ctrlrelay bridge start` — rotate your bot token if you
  ran a pre-release `main` build of this branch in a shared environment.

### Fixed

- **Daemon `start` no longer reports success for a crashed child.** The
  parent now waits briefly for the child after `subprocess.Popen` and
  reports failure if it exited (e.g. `gh` missing, env var unset,
  crash-on-import). Previously the parent printed `Poller started
  (PID N)` even when the child died within milliseconds.
- **Foreground `poller start` now handles `SIGTERM` cleanly.** Under
  launchd/systemd, service stop sends SIGTERM; the prior code only
  caught `KeyboardInterrupt`, so the outer `finally` that unlinks
  `poller.pid` and closes the state DB never ran. A recycled PID could
  then be mistaken for a live poller by the next `start`/`status`. The
  foreground path now installs SIGTERM/SIGINT handlers, cancels the poll
  loop task, and lets cleanup run.

## [0.1.3] - 2026-04-18

Reliability pass on the poller + retry flow, plus CI gating. No runtime
behavior change on the happy path — every fix is in the failure modes
that previously caused operator-visible wedges.

### Added

- **CI: `pytest` + `ruff check` workflow** (#26, via #49). Runs on every
  push to `main` and every pull request under a managed `uv` venv on
  Python 3.12. Fails on any test failure or lint violation. Also
  cleared 19 pre-existing lint errors and converted two
  backslash-continued `with patch.object` blocks to parenthesized
  `with` (PEP 617) so the suite passes cleanly under ruff.

### Fixed

- **Poller no longer crashes on individual `gh` failures** (#46, via
  #48). A slow or failing `gh issue list` used to propagate
  `TimeoutError` / `GitHubError` out of the polling loop, crashing the
  daemon and forcing a launchd restart. Two layers now:
  - Per-repo skip inside `IssuePoller.poll()` / `seed_current()` with
    a consecutive-failure counter; after 3 in a row the log escalates
    from INFO to WARNING (`persistent=True`) so a permanent misconfig
    stops hiding behind routine transient skips.
  - Outer safety net in `run_poll_loop` wraps both the poll and the
    per-handler dispatch so a single bad iteration or a handler crash
    can't take down the daemon. Each surviving event emits a structured
    `poll.iteration.failed` / `poll.handler.failed` / `poll.repo.skipped`
    record with correlation fields.
  - `asyncio.CancelledError` is always re-raised so a clean shutdown
    still propagates.
  - `poll()` never loses in-memory `seen_issues` mutations to a
    `_save_state` failure: the save is best-effort; the returned
    new_issues list always reaches the caller.
  - Malformed issue payloads (missing `number`, wrong type, non-dict
    entry) are skipped per-item so one bad record doesn't block the
    valid issues before or after it.

- **Verify-exhausted retries no longer wedge on a leftover branch**
  (#28, via #50). After a dev run exhausted `max_fix_attempts`, the
  branch was preserved on purpose (for operator inspection) but the
  next retry immediately failed with `fatal: 'fix/issue-N' already
  exists` from `git worktree add -b`. Now `create_worktree_with_new_branch`
  detects a pre-existing branch and handles four cases:
  - **On origin + local behind**: fast-forward to remote head via a
    dedicated scratch ref (`refs/ctrlrelay/sync/<branch>`) — never
    overwrites `refs/heads/<branch>` directly, so an unpushed local
    commit can't be silently lost. `git fetch` here has a 30s cap and
    all steps are best-effort.
  - **On origin + local ahead**: preserve local (likely recoverable
    unpushed work).
  - **On origin + diverged**: raise a clear `WorktreeError` — silent
    reuse would cause a non-ff push rejection later.
  - **Local-only**: use `git cherry <default> <branch>` to detect if
    every commit on the branch is already content-equivalent to
    something in the default branch (catches regular / squash / rebase
    merges). If yes, the branch is stale; delete + create fresh. If
    no, the branch has unique unpushed work; reuse.
  - Refuses reuse when the branch is still checked out by another live
    worktree (BLOCKED session that's waiting on operator reply).
  - Handles crash-between-rmtree-and-prune: on `worktree add` failing
    with "already checked out" against a stale admin entry whose
    worktree directory is gone, targets the specific admin dir (via
    its `gitdir` pointer, not path basename — works across git's
    sanitization and de-duplication) and deletes just that one without
    running the repo-wide `git worktree prune`. Scope-gated to entries
    under our managed `worktrees_dir` so a disconnected network mount
    never gets its admin state destroyed.

### Changed

- All new log events use the structured JSON helper from 0.1.1 with
  consistent `session_id` / `repo` / `issue_number` / `reason` fields.

### Known follow-ups

The following were identified during codex review and are filed as
separate tracked issues so the ownership and scope are clear:

- #51 — `branch_preexisted` ownership snapshot in `run_dev_issue`
  goes stale after `create_worktree_with_new_branch` recreates a
  stale-merged branch. Needs an API change to return `(path,
  created_fresh)` so the cleanup path knows whether to delete on
  failure. Narrow corner case (prior merged PR + retry that fails
  before push).
- #52 — Reuse path should refuse a branch that still backs an OPEN
  PR. Requires a `gh pr list --head <branch>` probe; worth a
  coordinated PR with #51.

## [0.1.1] - 2026-04-18

First release under the new `ctrlrelay` name. Ships the full
`BLOCKED → operator → resume` loop end-to-end, plus the observability +
verification features that were cooking since 0.1.0.

### Changed

- **Project renamed: `dev-sync` → `ctrlrelay`** (#42). Repo moved from
  `AInvirion/dev-sync` to `AInvirion/ctrlrelay`.
  - Python package: `dev_sync` → `ctrlrelay`; PyPI dist: `dev-sync` →
    `ctrlrelay`; CLI binary: `dev-sync` → `ctrlrelay`.
  - Runtime paths: `~/.dev-sync/` → `~/.ctrlrelay/` (state DB, worktrees,
    bare repos, sockets, logs).
  - Transport socket: `dev-sync.sock` → `ctrlrelay.sock`.
  - Environment variables: `DEV_SYNC_*` → `CTRLRELAY_*`
    (`CTRLRELAY_TELEGRAM_TOKEN`, `CTRLRELAY_DASHBOARD_TOKEN`,
    `CTRLRELAY_STATE_FILE`, `CTRLRELAY_SESSION_ID`).
  - launchd labels: `com.ainvirion.dev-sync-{poller,bridge}` →
    `com.ainvirion.ctrlrelay-{poller,bridge}`.

### Added

- **End-to-end `BLOCKED → operator → resume` loop over Telegram** (#38).
  When Claude signals `BLOCKED_NEEDS_INPUT`, the orchestrator posts the
  question via the configured transport, the bridge long-polls Telegram
  for the operator's reply, and the session is resumed with the answer.
  Bounded by `max_blocked_rounds` (default 5). Transport failures collapse
  to FAILED cleanly instead of stranding a blocked session.
  - `TelegramHandler.start_polling(handler)` / `stop_polling()` added.
  - Bridge server tracks `request_id → (telegram_msg_id, writer)` and
    delivers an `ANSWER` frame over the originating socket.
  - Match priority: reply_to_message_id → FIFO fallback.
  - Client-disconnect path drops that client's pending questions.
- **Structured observability events** (#40): `dev.question.posted`,
  `dev.answer.received`, `dev.session.resumed` land as JSON lines in
  `~/.ctrlrelay/logs/*.log`, with `session_id` / `repo` / `issue_number`
  correlation across every boundary. Bridge also emits `bridge: ASK`,
  `bridge: ANSWER`, `bridge: SEND` event lines.
- **Release-artifact trigger** (#37): the build workflow now fires on
  tag push (`v*`) and on published releases, and uploads the built
  wheel + sdist to the matching GitHub release automatically.
- **Operator / configuration / architecture documentation rebuild**
  (#39): `docs/` rewritten around how to use, configure, and operate
  ctrlrelay, closing #36.

### Fixed

- **Bridge: `ConnectionResetError` traceback on client disconnect race**
  (#45). The bridge used to emit an unhandled traceback in
  `bridge.error.log` when the client closed its socket while the bridge
  was still flushing the ACK. Now wraps `writer.write + drain` with an
  `is_closing()` pre-check and swallows `ConnectionResetError` /
  `BrokenPipeError` / `OSError` at DEBUG level with `op` + `request_id`
  for diagnosis.
- **README**: drop broken docs site link (#44, fixes #43).

### Migration (clean-slate)

Existing installs need a reset:
```bash
# stop + remove old daemons
launchctl bootout gui/$(id -u)/com.ainvirion.dev-sync-poller 2>/dev/null
launchctl bootout gui/$(id -u)/com.ainvirion.dev-sync-bridge 2>/dev/null
rm -f ~/Library/LaunchAgents/com.ainvirion.dev-sync-*.plist
# uninstall old package + remove all state
pip uninstall -y dev-sync
rm -rf ~/.dev-sync
# update shell env: rename DEV_SYNC_* → CTRLRELAY_*
# reinstall ctrlrelay, install new plists, bootstrap
```

## [0.1.0] - 2026-04-18

First tagged release of the `ctrlrelay` orchestrator. Bundles the multi-device
sync toolkit with the Phase 0–4 implementation of the local-first Claude Code
orchestrator (config, checkpoints, Telegram bridge, secops pipeline, and dev
pipeline).

### Added

#### Orchestrator package (`ctrlrelay`)

- **Phase 0 — package skeleton**: `pyproject.toml` with metadata and entry
  point, Typer-based CLI (`ctrlrelay`), Pydantic config models with validation,
  SQLite-backed state with per-repo locks, and `config validate` / `status`
  commands.
- **Phase 1 — checkpoints & skill audit**: checkpoint Pydantic models,
  `done` / `blocked` / `failed` helpers, `read_checkpoint` for the
  orchestrator, skill discovery from `SKILL.md`, audit checks with markdown
  report formatting, and `skills audit` / `skills list` CLI commands.
- **Phase 2 — Telegram bridge**: bridge protocol message types, transport
  protocol abstraction with `SocketTransport` and `FileMockTransport`
  implementations, bridge server with socket handling, Telegram handler,
  `bridge start/stop/status/test` CLI commands, and a daemon entry point.
- **Phase 3 — secops pipeline**: GitHub CLI wrapper, git worktree manager,
  Claude subprocess dispatcher, dashboard client with offline event queue,
  pipeline base protocol, `secops` pipeline implementation, and the
  `run secops` CLI command.
- **Phase 4 — dev pipeline**: GitHub issue and PR helpers, assigned-issue
  poller with persistent seen-state, worktree+branch creation and push,
  PR merge watcher, `dev` pipeline orchestration with post-merge handler,
  and `dev` / `poll` CLI commands.

#### Multi-device sync toolkit

- `./sync` CLI for cloning, pulling, and inspecting repos listed in
  `repos.manifest`, with filter and dry-run modes.
- Claude Code config sync (`export` / `import`) for settings, keybindings,
  agents, hooks, and skills, plus `team-export` / `team-import` for
  shareable team config (with home-path sanitization).
- Codex CLI sync (`codex-export` / `codex-import` / `codex-install`) and
  `codex-reviewer` MCP server for Claude ↔ Codex code review.
- First-time device bootstrap (`./sync setup`) and manifest rescanning
  (`./sync manifest`), including support for nested repo folders.
- Bundled Claude skills under `claude-config/skills/` (VID, gh-issue,
  gh-dashboard, gh-secops, gh-prdone, code-interest-sniff-test,
  codex-review-loop) and lab skills (ainvirion-design-conventions,
  ainvirion-morphs, minions, vid-spec).

### Documentation

- `docs/ctrlrelay-orchestrator-spec.md` — orchestrator design spec.
- `docs/superpowers/specs/2026-04-17-ctrlrelay-orchestrator-design.md` and
  per-phase implementation plans (Phase 0 through Phase 4).
- `docs/Claude_Code_Project_Guide.md` — project development guide.

[Unreleased]: https://github.com/AInvirion/ctrlrelay/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.3
[0.1.1]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.1
[0.1.0]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.0
