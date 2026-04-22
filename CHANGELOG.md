# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.11] - 2026-04-22

Patch release. Fixes a stale-bare-repo bug that caused worktrees
to check out commits days or weeks behind origin — caught by the
v0.1.10 task pipeline e2e test, where the agent reported pytest
counts from a two-week-old commit.

### Fixed

- **`ensure_bare_repo` uses an explicit fetch refspec.** Previous
  `git fetch --all` was a silent no-op on bare clones with no
  `remote.origin.fetch` config (a state several real bare clones
  in the wild were in). The new explicit
  `refs/heads/*:refs/heads/*` refspec writes remote branch heads
  directly to local refs regardless of config state. Non-force on
  purpose: dev pipeline's branch-reuse path needs to preserve
  unpushed-local commits, which a force fetch would clobber
  (codex P1 caught on the first PR pass).

### Operator notes

- Upgrade via `uv tool install ctrlrelay@latest --force` and
  restart poller + bridge. No schema changes.
- One-time cleanup (already done on the maintainer's box): if you
  have existing bare clones that may be stale, run from
  `~/.ctrlrelay/repos`:
  ```bash
  for b in *.git; do
    git --git-dir="$b" fetch --prune origin 'refs/heads/*:refs/heads/*'
  done
  ```
  Subsequent ensure_bare_repo calls will keep them current.

## [0.1.10] - 2026-04-21

Adds a new "task" pipeline for GitHub issues whose outcome is
information, not code. The dev pipeline still handles issues that
expect a PR; a label routes the non-PR shape to the new pipeline.

### Added

- **Task pipeline** — `task`-labeled issues route to
  `src/ctrlrelay/pipelines/task.py` instead of the dev pipeline. The
  agent runs in a worktree of the repo's default branch, does the
  work (runs builds, investigates, reads files, queries tools), posts
  its findings as a GitHub issue comment, and signals DONE. No
  branch, no PR handoff.
- **`automation.task_labels`** — per-repo list of labels that route
  to the task pipeline. Defaults to `["task"]`. Case-insensitive
  match. `exclude_labels` still wins when both would match — an
  issue tagged both `manual` and `task` is skipped entirely (manual
  means "not for the agent").
- **Telegram notification variant for tasks** — `✅ Task done on #N
  ({repo}): {summary}` instead of `✅ PR ready: {url}` so empty PR
  URLs can't sneak through.
- **Resume-via-Telegram for blocked task sessions** — inherited from
  the same pending_resumes + sweeper path that powers dev/secops
  resumes. `resume_task_from_pending` rebuilds a fresh worktree from
  default branch (task worktrees are ephemeral, no branch state to
  preserve).

### Fixed

- **Lock leak on worktree-cleanup cancel.** Both `run_task_issue`
  and `resume_task_from_pending` now wrap `remove_worktree` with
  `asyncio.wait_for` and release the repo lock early on
  `CancelledError`. Without this guard a SIGTERM during cleanup
  would propagate out of the `finally` block before `release_lock`
  ran, wedging task/dev/secops on that repo until the row was
  manually cleared.

### Operator notes

- Upgrade via `uv tool install ctrlrelay@latest --force` and
  restart poller + bridge. No schema changes.
- To try it: label a GitHub issue with `task` and assign it to
  yourself. The agent will pick it up on the next poll, run in a
  default-branch worktree, and post findings as an issue comment.
  Expect a `✅ Task done` Telegram notification when it finishes.
- To add more routing labels (e.g., `investigate`, `build-check`):
  `automation.task_labels: ["task", "investigate", "build-check"]`
  per repo.
- Dev pipeline behavior is unchanged — issues without a task label
  still produce a PR.

## [0.1.9] - 2026-04-21

Extends the resume-via-Telegram flow shipped in v0.1.8 to cover the
dev pipeline. Plus three correctness fixes to the persistence layer
that also protect the secops path — caught by four rounds of codex
review on the PR.

### Added

- **Resume BLOCKED dev sessions via Telegram reply.** A dev session
  that exits BLOCKED (max_blocked_rounds exhausted, transport
  unavailable, or transport failing mid-loop) now writes a
  `pipeline="dev"` row into `pending_resumes`. The per-minute
  sweeper drains these via the new `resume_dev_from_pending` helper:
  looks up the session context from the sessions table, reuses the
  existing worktree (dev's BLOCKED-keep-both cleanup rule already
  leaves it alive), and calls `DevPipeline.resume()`. Same live
  in-process loop so the operator can have back-and-forth on the
  resume path too.
- **Sweeper dispatches by pipeline.** `row["pipeline"]` selects
  `resume_secops_from_pending` or `resume_dev_from_pending`.
  Unknown pipelines or missing repo config mark the row resumed to
  avoid hot-loops.

### Fixed

- **`mark_pending_resume_resumed` no-ops on refreshed unanswered
  rows.** When a resume re-blocks, the pipeline inserts a FRESH
  `pending_resumes` row (clearing answer/answered_at/resumed_at).
  The sweeper used to blindly stamp `resumed_at` on that fresh row,
  so the next operator reply set `answered_at` but left `resumed_at`
  populated, hiding the row from `list_pending_resumes_to_execute`
  forever. Now guarded on `answered_at IS NOT NULL`. Affected both
  secops and dev paths.
- **Empty-question BLOCKED exits now persist.** The persistence
  guard `if result.blocked and result.question` skipped rows when
  the agent BLOCKED without question text. New
  `_question_for_persist` helper synthesizes the same fallback
  string the in-process loop uses, so empty-question blocks still
  land in `pending_resumes`. Affected both secops and dev paths.
- **Transport failure inside BLOCKED loops preserves blocked=True.**
  `run_dev_issue` and `resume_dev_from_pending` used to convert a
  failed `transport.ask()` into `success=False, blocked=False`,
  skipping the outer persistence branch and wedging multi-turn
  resume sessions when the bridge flaked. Now keeps `blocked=True`
  with an error note so persistence fires and the session is
  resumable when the bridge is back.

### Schema

No schema changes. `pending_resumes` table unchanged from v0.1.8;
all fixes are in the helper methods and pipeline orchestration.

### Operator notes

- Upgrade via `uv tool install ctrlrelay@latest --force`, restart
  poller and bridge. The sweeper will start picking up
  pipeline="dev" rows automatically.
- Dev resume requires the repo's `dev_branch_template` to be in
  config (it's the standard field that was already there). If a
  previously-BLOCKED dev session is for a repo that was removed
  from config, the sweeper logs a skip and marks resumed — manual
  CLI resume or re-adding the repo is the recovery path.

## [0.1.8] - 2026-04-21

The "reply to BLOCKED in Telegram and it actually resumes" release.
Two operator-visibility fixes surfaced from running a 79-repo secops
sweep: a noisy log-spam issue and a silently-dropped-reply issue. The
latter turned into a proper resume flow.

### Added

- **Resume BLOCKED secops via Telegram reply.** When a scheduled
  secops sweep escalates BLOCKED and exits, the question is now
  persisted in a new `pending_resumes` table. Replying in Telegram
  matches against that table and queues the answer; a new per-minute
  `pending_resume_sweeper` scheduler job inside the poller drains
  answered rows — re-acquires the repo lock, re-creates the worktree,
  calls `SecopsPipeline.resume(ctx, answer)`, and Telegrams the
  result (success / re-blocked / failed). First reply-to-resume
  round-trip is ≤60s.
- **Disambiguation when multiple BLOCKED sessions exist.** Replying
  "merge it" when both `repoA` and `repoB` are blocked used to route
  to FIFO (wrong repo, possibly destructive). The bridge now refuses
  to guess: with >1 unanswered BLOCKED sessions it returns a Telegram
  list of pending session_ids so the operator can reply with one
  included. Single-BLOCKED case stays unambiguous.

### Fixed

- **Poller log spam on issues-disabled repos.** Repos with GitHub's
  Issues feature disabled (template repos, signature repos, GitHub
  Pages sites) returned a permanent `GitHubError(... has disabled
  issues)` that the poller classified as transient, retrying every
  120s and escalating to WARNING after 3 cycles. `poll()` and
  `seed_current()` now detect the specific error, mark the repo in
  an in-memory permanent-skip set, log once at INFO as
  `poll.repo.issues_disabled`, and skip the `gh` call on subsequent
  cycles. Resets on daemon restart.
- **Orphan Telegram replies silently dropped.** When a BLOCKED
  session had already torn down (scheduled secops), the bridge's
  in-memory `_pending_questions` entry died with the ASK socket and
  the operator's reply disappeared with just an `info` log line. The
  bridge now replies via Telegram so the failure is visible (and,
  with the resume flow above, actually lands as an answer).
- **Pending_resumes rows no longer dropped on sweeper lock
  contention.** When the per-minute sweeper raced the 6am scheduled
  secops on the same repo, it used to `mark_pending_resume_resumed`
  unconditionally and lose the queued answer. The sweeper now detects
  the specific `"Repository locked by another session"` error and
  leaves the row pending for the next tick.

### Schema migration

State DB gains a `pending_resumes` table (session_id PK, pipeline,
repo, question, created_at, answer, answered_at, resumed_at). Two
partial indexes: `idx_pending_resumes_unanswered` (for orphan-reply
lookup) and `idx_pending_resumes_answered_unresumed` (for sweeper
load). Created automatically on daemon start; no backfill needed.

### Operator notes

- Upgrade via `uv tool upgrade ctrlrelay` (or
  `uv tool install ctrlrelay@latest --force` if pinned), restart
  poller and bridge so the new sweeper schedules and the bridge
  sees the new schema.
- To exercise the resume-via-Telegram path: let a scheduled secops
  escalate BLOCKED, reply to the Telegram notification with your
  decision (or a fresh message that mentions the session_id if
  multiple repos are BLOCKED). Expect a `✅ Answer queued` ack within
  seconds and a result message within ~1 minute.
- Dev pipeline resume-via-Telegram is not yet wired; the sweeper
  skips non-secops rows.

## [0.1.7] - 2026-04-20

Patch release. Fixes one drift bug in how the package reports its own
version — the `__version__` string was pinned to a literal in
`__init__.py` and never got bumped alongside releases, so `ctrlrelay
version` on an installed 0.1.6 wheel still reported `0.1.4`. No API
or behavior changes.

### Fixed

- **`__version__` now derives from installed metadata** (`closes
  #94`). `ctrlrelay.__version__` resolves via
  `importlib.metadata.version("ctrlrelay")` at import time so it
  tracks the installed wheel's `pyproject.toml` automatically.
  Source-checkout pytest runs (where no dist-info exists because
  `pythonpath = ["src"]` is the only mechanism putting the package on
  `sys.path`) fall back to parsing the sibling `pyproject.toml` with
  `tomllib` — so the drift-catcher test
  (`test_version_matches_pyproject`) still sees a real version
  instead of a `0.0.0+unknown` placeholder.

### Operator notes

- No migration or restart guidance. Upgrade via `uv tool upgrade
  ctrlrelay` (or `pip install -U ctrlrelay`) and the next `ctrlrelay
  version` will report `0.1.7`.

## [0.1.6] - 2026-04-20

The "pipeline reliability + operator control" release. Closes three
failure modes that made the dev pipeline silently misreport its own
work, plus two new filters that let the operator say which issues the
agent should actually touch. Net effect: when a session ships a PR,
the success ping is real; when it blocks or fails, the question
reaches you; and the agent stops spontaneously opening PRs on issues
you meant for yourself.

### Added

- **Poller: self-assignment filter** (`closes #79`). Foreign
  assignments (teammate, bot, or auto-CODEOWNERS) are logged as
  `poll.issue.foreign_assignment`, marked seen so they don't
  re-trigger, and **not** handed to the dev pipeline. Old behaviour
  is opt-in per repo via `automation.accept_foreign_assignments:
  true`.
- **Poller: exclude-label filter** (`closes #91`). New
  `repos[].automation.exclude_labels` list of label names; any issue
  carrying a match is logged as `poll.issue.excluded_by_label` and
  skipped. Default value `["manual", "operator", "instruction"]`
  covers the common "this is a task for me, not the agent" case out
  of the box — set to `[]` to disable.
- **`ctrlrelay ci wait --pr <N>`** (`closes #85`). First-class CLI
  helper that polls a PR's GitHub Actions checks and exits 0 on
  all-pass, 1 on any fail, 2 on timeout. The dev-pipeline prompt now
  tells the agent to use this instead of improvising `until gh pr
  checks` bash loops — which it had been getting wrong, burning the
  full 30-minute session timeout and surfacing misleading "❌
  Failed" Telegram notifications for PRs that had actually shipped.

### Fixed

- **Dispatcher: `--resume` now uses Claude's session UUID, not our
  composite id** (`closes #83`). Newer `claude` CLI (v2.0.x+)
  validates that `--resume <id>` is a real UUID or a known session
  title; our `dev-<owner>-<repo>-<issue>-<hex>` composite id
  hard-failed every resume. `ClaudeDispatcher` now parses
  `session_id` from Claude's JSON stdout, exposes it as
  `SessionResult.agent_session_id`, and persists it in a new
  `sessions.agent_session_id` column. Sessions predating the fix
  fall back to a fresh spawn rather than hard-erroring.

### Changed

- **Docs (`SECURITY.md`)**: vulnerability reports now go to
  `security@ainvirion.com` instead of the personal Gmail that
  shipped in v0.1.5. Rotate any prior-release reference.
- **Docs site cleanup** (`closes #81`): retired the stale
  `docs/reference/` tree (original spec, Phase-by-phase plan docs,
  Claude-Code project guide) that drifted from what v0.1.5 actually
  ships and was actively misleading to new readers (and to LLMs
  writing PRs against this repo). Operator-facing docs (getting
  started, configuration, bridge, cli, operations, architecture,
  development) are unchanged.

### Schema migration

State DB sessions table gains a nullable `agent_session_id TEXT`
column. Runs automatically on daemon start; existing rows backfill
to NULL and participate in the fresh-spawn fallback above.

### Operator notes

- Default `exclude_labels` means the poller will now **skip** issues
  you tag with `manual`, `operator`, or `instruction` even though
  they're still assigned to you. If that's unexpected, either
  override the list or drop the label.
- The new `self-assignment filter` will skip issues that a teammate
  assigns to you. If a team workflow relies on teammate-assignment,
  flip `automation.accept_foreign_assignments: true` on that repo.

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
