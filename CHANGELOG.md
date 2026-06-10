# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-06-10

### Added

- **Secops sweeps now remember operator decisions across days.** The
  `automation_decisions` table had a schema but no producer or consumer
  — every 6am sweep re-asked about the same Dependabot PRs the operator
  had already answered, drowning Telegram. Wired both halves:
  1. When the BLOCKED loop in `run_secops_all` (or the out-of-band
     `resume_secops_from_pending` sweeper) receives an answer, regex
     extracts every PR# from the question and records one row per PR#
     keyed on `(repo, "dependabot_pr", "#N")` with the operator's
     verbatim answer. PR# regex handles both `PR #60` and bare `#60`
     forms, dedupes, and tolerates questions with no PR# (e.g.
     CodeQL-only) by writing nothing.
  2. Before building each sweep's prompt, `run_secops_all` pulls the
     last 30 days of decisions for that repo (namespaced to
     `operation="dependabot_pr"` so e.g. CodeQL-suppression decisions
     don't leak into the Dependabot prompt) and threads them via
     `ctx.extra['prior_decisions']` into `_build_prompt`, which renders
     a `## Prior operator decisions (last 30 days)` block listing each
     decision verbatim alongside the original question snippet so the
     agent can detect a force-pushed PR that swapped the version bump
     under the same PR number. The block carries instructions to act
     on prior answers unless circumstances have materially changed
     (different version bump, CI state flipped). Without this the
     persistence layer was dead weight.

### Fixed

- **Secops handles "no CI configured" without inventing options.**
  Repos with no `.github/workflows/*` previously confused the agent
  into freelance suggestions ("Want me to batch into a review PR?")
  because the "auto-merge with passing CI" gate couldn't fire. Prompt
  now explicitly directs: treat ALL Dependabot PRs as ASK regardless
  of tier, and signal BLOCKED ONCE with a single consolidated question
  per repo (not one per PR).
- **Secops escalates pre-existing CI failures instead of stalling PRs.**
  When CI is failing on a check unrelated to the PR's package (e.g.
  `pip-audit` flagging a transitive CVE in `idna` while the PR bumps
  `boto3`), patch PRs were sitting stuck for weeks because the agent
  reported the failure and moved on. Prompt now requires the agent to
  signal BLOCKED with a one-line question naming the underlying issue
  and a root-cause hint from `gh run view --log-failed`.
- **Secops scope discipline.** The prompt now enforces "exactly three
  legitimate exits" per open PR (merge / leave / BLOCKED) and
  explicitly forbids batching PRs into review PRs, opening tracking
  issues, or performing manual reviews. Cuts off the freelance
  options observed in production sweeps.

## [0.5.0] - 2026-05-11

Minor release. Three changes on the secops path; together they take the
secops sweep from "silently drops operator decisions" to "respects per-repo
policy and reaches the operator on every blocked decision".

### Fixed

- **Secops BLOCKED questions now reach the operator via Telegram.**
  Three discrete bugs were stacking to drop every blocked secops session
  silently (#131):
  1. `run_secops_all` never called `transport.ask()` after a blocked
     pipeline result. The DB row was marked `status='blocked'` and a
     `pending_resumes` entry was inserted, but the question went nowhere.
     Now mirrors the dev/task pipelines: post the question, await an
     answer, resume — up to `DEFAULT_MAX_BLOCKED_ROUNDS=5` rounds. On
     transport failure preserves `blocked=True` so the existing
     persistence-on-blocked branch fires.
  2. `ctrlrelay run secops` (manual CLI) was passing `transport=None`,
     so even with the dispatch loop in place, the `is not None` gate
     short-circuited and questions still went to pending_resumes
     instead of Telegram. Now builds a `SocketTransport` when the
     bridge socket is up, mirroring the scheduled-cron path.
  3. `SocketTransport._receive_loop` resolved the per-request future on
     the FIRST message matching `request_id`. The bridge sends two
     messages for an ASK: an intermediate `ACK(status="pending")` then
     a terminal `ANSWER`. Receiving the ACK fulfilled the future early
     and `ask()` raised `TransportError("Unexpected response: BridgeOp.ACK")`.
     Now skips `ACK(status="pending")` and waits for `ANSWER`/`ERROR`.

  Real-world impact: an 86-repo secops sweep with 21 sessions hitting
  BLOCKED produced zero Telegram messages pre-fix. Post-fix, every
  blocked session reaches the operator with a structured question.

### Added

- **Auto-merge operator-authored `.github/dependabot.yml`-only PRs.**
  The secops agent's policy treated all operator-authored PRs as needing
  explicit approval, even ones that ONLY add an ecosystem entry to
  `.github/dependabot.yml`. These are the prerequisite PRs the operator
  files when bulk-enabling Dependabot across repos with branch protection,
  and they sat indefinitely (#132). The carve-out is narrow and
  multi-gated:
  1. **Author check**: `author.login == $OPERATOR` (derived from
     `gh api user --jq .login`) — collaborators, GitHub apps, or
     external contributors are NOT eligible even for dependabot.yml-only.
  2. **Diff check**: `gh pr diff` must be PURELY ADDITIVE — any line
     beginning with `-` (other than `---` file headers) signals a
     deletion or modification of existing config -> BLOCKED.
  3. **CI check**: `gh pr checks` must all pass — a PR adding invalid
     YAML can pass author+diff and still break Dependabot for the repo
     if merged without this gate.

  All three must pass before merge. Any one failing -> BLOCKED.

- **Per-repo `automation:` policy now drives the secops agent.**
  `orchestrator.yaml`'s per-repo `automation` block (defining
  `dependabot_patch`, `dependabot_minor`, `dependabot_major` as
  auto/ask/never) was decorative — the secops prompt had hardcoded
  prose that ignored it (#133). Now `_build_prompt` accepts an
  `AutomationConfig` and renders per-tier directives like
  "patch updates: AUTO-MERGE", "minor updates: ASK", "major updates: NEVER"
  per repo. `run_secops_all` puts `repo_config.automation` in
  `ctx.extra`; `resume_secops_from_pending` accepts an `automation`
  kwarg; the pending-resume sweeper in `cli.py` looks up the per-repo
  policy and threads it on resume.

  Operators can now set a sensitive repo to `dependabot_minor: never`
  and the agent will actually respect it.

## [0.4.1] - 2026-05-08

Patch release. Two follow-ups against the v0.4.0 setup flow surfaced
during the dogfood validation:

### Fixed

- **`setup` no longer lists the personalization repo under
  `repos:`.** When the operator's personalization repo lives under
  one of the configured owners (e.g. `alice/dotclaude` while `alice`
  is also an enumerated owner), it was being added to the dev
  pipeline's monitored set — the poller would have polled it for
  issues and worktree-cloned it. The repo is the cross-machine sync
  target, not a project; setup now drops it from the enumerated list
  before generating the YAML.

### Added

- **Auto-wire detected skills on `setup`.** When the personalization
  repo already contains `global/skills/<name>/` directories (e.g.
  from a prior `personalization push` on another machine), setup now
  pre-clones the repo, scans for skill subdirectories, and adds one
  `paths:` entry per skill to the generated `personalization.paths`
  block. Operators don't have to hand-edit the config to wire each
  skill back on a new machine. Pass `--no-wire-skills` to opt out.
  Hidden directories and stray top-level files are ignored — only
  real skill packages count.

## [0.4.0] - 2026-05-08

Minor release. One new feature plus one schema simplification:

- **`ctrlrelay setup`** — first-run onboarding command. Detects every
  GitHub org you belong to, enumerates non-fork non-archived repos in
  each, writes a fresh `~/.config/ctrlrelay/orchestrator.yaml`, clones
  every repo to `~/Projects/<owner.lower()>/<repo>`, optionally
  configures the personalization sync block, and optionally renders
  launchd/systemd unit files. Replaces the multi-step manual playbook
  operators previously had to follow on a new machine. Interactive by
  default; `--yes` and `--owner` flags make it scriptable.
- **`paths.owner_aliases` deprecated; lowercase-org-folder convention.**
  The path resolver now always derives `local_path` as
  `${repo_root}/${owner.lower()}/${repo}` (closes #128). The previous
  `owner_aliases` indirection caused `clone-all`/`pull-all`/`status`
  to disagree with the dev pipeline on where a given repo lived.
  Parsing of `owner_aliases` is retained so 0.3.x configs still load;
  a `DeprecationWarning` fires when the block is non-empty.

### Added

- **`ctrlrelay setup`** (closes the onboarding gap reported during the
  v0.3.0 reinstall flow). Composes existing primitives (gh discovery,
  config generation, `git clone`, `personalization init`, `install
  launchd|systemd`) into a single command. Reads
  `$CTRLRELAY_TELEGRAM_TOKEN` so the rendered plist isn't a
  placeholder. Refuses to overwrite an existing `orchestrator.yaml`
  without `--force`.
- **`repos clone-all` / `pull-all` / `status` accept DEST as
  optional**. When omitted, each command operates on the
  config-resolved `local_path` of every repo, so the same path
  resolution serves the bulk commands and the dev pipeline. Pass DEST
  to override (lands at `DEST/<owner.lower()>/<repo>`).

### Changed

- **Path resolver: `owner.lower()` is the folder, always.** Closes #128.
  Affects every command that touches `repo.local_path`. Operators on
  v0.3.0 with mixed-case folders (e.g. `~/Projects/AInvirion/...`)
  must either rename the folder, set per-repo `local_path` overrides,
  or run `ctrlrelay setup --force` to land everything at the new
  lowercase paths.

### Migration from 0.3.0

- Drop `paths.owner_aliases` from `orchestrator.yaml` (or ignore the
  deprecation warning until you next regenerate the config).
- Rename existing on-disk folders to lowercase (e.g.
  `mv ~/Projects/AInvirion ~/Projects/ainvirion`) — or, easier, run
  `ctrlrelay setup --force` to clone everything fresh under the new
  convention.

## [0.3.0] - 2026-05-08

Minor release. Three additive features: cross-machine **personalization
sync** of operator state through a private GitHub repo, **portability
fixes** that let one `orchestrator.yaml` work unmodified across machines,
and **label-driven issue matching** for the dev pipeline. One install
fix (`PYTHONUNBUFFERED`) and a docs page covering the new
personalization flow.

### Added

- **Personalization sync** (#123, #124). New `personalization:` config
  block + `ctrlrelay personalization init/status/push/pull`
  subcommands sync the operator's Claude Code state — global config,
  per-project memory, spec/superpower outputs — across machines
  through a separate (typically private) GitHub repo. Per-machine
  branches (`personalization/<node_id>`) rebase onto `main` and FF
  the integration branch, so two machines pushing concurrently never
  overwrite each other; `--force-with-lease` is reserved for the
  per-machine branch, never used on `main`. Source/target paths
  support `${HOME}`, `${PROJECT}` (slug `<owner>--<repo>`),
  `${PROJECT_ENCODED}` (matches Claude's path encoding),
  `${PROJECT_LOCAL}`, and `${PROJECT_PARENT}` placeholders. An
  allowlist limits commits to declared entries — random files in the
  checkout aren't staged. **Adopt-flow** is on by default: `init`
  moves pre-existing real targets (e.g. `~/.claude/CLAUDE.md` that
  predates the sync setup) into the synced repo and lays a symlink in
  their place; `--no-adopt` opts out. Both-real-content collisions
  surface as `skipped-conflict-both-exist` for manual reconciliation.
  See the new [Personalization sync](docs/personalization.md) page.
- **Auto-pull on cron** (#124). New optional
  `schedules.personalization_cron` runs `personalization pull` on the
  poller daemon, with two safety rails: skip-on-dirty (never rebases
  under uncommitted operator edits) and `adopt=False` on the re-wire
  (a background sync never silently moves files; adoption stays
  init-only). Auto-push is intentionally not scheduled — daemon-side
  commits surprise people. Dispatched via `asyncio.to_thread` so a
  slow remote can't stall the poller's event loop (Telegram dispatch,
  pending-resume sweeper, secops cron).
- **`paths.repo_root` + `paths.owner_aliases`** (#121). When set,
  `repos[].local_path` is derived as
  `${repo_root}/${owner_aliases.get(owner, owner)}/${repo}`. Per-repo
  `local_path` still wins as an override. Without `repo_root`, the
  legacy "local_path required per repo" behaviour is preserved.
  Collapses 69 explicit `local_path` values to 20 in the maintainer's
  config.
- **`node_id` defaults to hostname** (#121). Falls back to
  `socket.gethostname()` when missing, null, or blank. Heartbeats and
  session logs still get a meaningful per-node label without forcing
  every operator to edit the file.
- **`ctrlrelay install launchd|systemd`** (#121). Renders
  bridge/poller service unit files from in-package templates,
  substituting `USER`, `HOME`, `CTRLRELAY_BIN`, `WORKDIR`,
  `LABEL_PREFIX`, `POLLER_INTERVAL`, and (when set)
  `CTRLRELAY_TELEGRAM_TOKEN`. Writes to the conventional locations
  (`~/Library/LaunchAgents` on macOS, `~/.config/systemd/user` on
  Linux) and refuses to clobber existing files unless `--force`.
  Replaces the docs.operations.md copy-paste flow where every
  operator hand-edited `/Users/$ME/...` strings — a tax on
  portability and a common source of broken plists.
- **Label-driven issue matching** (#115, closes #80). Two new
  per-repo lists govern which issues the poller hands to the dev
  pipeline:
  - `repos[].automation.exclude_labels` (default `["manual",
    "operator", "instruction"]`) — issues carrying any of these
    labels are skipped, marked seen, logged as
    `poll.issue.excluded_by_label`, and never trigger code changes.
    For operator tasks and pure instruction issues.
  - `repos[].automation.include_labels` (default `[]`) — when
    non-empty, issues carrying any of these labels opt **in** to the
    dev pipeline regardless of who is (or isn't) assigned. For repos
    that drive the pipeline by triage label rather than assignment.
  Matching is case-insensitive. Trust model documented in
  [configuration.md](docs/configuration.md#repos-automation): anyone
  with triage permission on a repo can apply a label, which matches
  ctrlrelay's existing trust model.

### Fixed

- **`PYTHONUNBUFFERED=1` in launchd plists and systemd units** (#122).
  Without it, daemon stdout/stderr buffered up to 4–8 KB before
  flushing to the log file — so `tail -f` on a poller log looked
  frozen for minutes during quiet periods, and crash diagnostics were
  clipped at the last buffer boundary instead of the actual failure
  point. Templates now set the env var and `ctrlrelay install
  launchd|systemd` re-emits both unit files on next run.

### Docs

- **[Personalization sync](docs/personalization.md)** (#125). New
  page (nav order 8) covering setup, the dotclaude repo layout, the
  init/status/push/pull lifecycle, `--no-adopt`, auto-pull cron,
  multi-machine bootstrap, and the gotchas (worktrees, edit-through-
  symlink semantics, allowlist enforcement, conflict handling, strict
  origin URL match). Pairs with new `schedules` and `personalization`
  sections in [configuration.md](docs/configuration.md) and a new
  `ctrlrelay personalization` block in [cli.md](docs/cli.md).

## [0.2.1] - 2026-04-28

Patch release. Fixes a long-standing UX bug where `ctrlrelay` could
only be invoked from a directory containing `config/orchestrator.yaml`.

### Fixed

- **`--config` now auto-discovers `orchestrator.yaml`.** Every CLI
  command previously hardcoded a relative `config/orchestrator.yaml`
  default, so running `ctrlrelay status` (or any other subcommand)
  from `/tmp`, `$HOME`, or anywhere outside the project root failed
  with `Config file not found: config/orchestrator.yaml`. The CLI
  now resolves the config in this order:

  1. The path passed to `--config` / `-c`, if any.
  2. `$CTRLRELAY_CONFIG` (a new environment variable).
  3. `./config/orchestrator.yaml`, walking up from the current
     working directory to the filesystem root — matches how `git`
     and `uv` find their config.
  4. `$XDG_CONFIG_HOME/ctrlrelay/orchestrator.yaml` (defaults to
     `~/.config/ctrlrelay/orchestrator.yaml`).

  When nothing matches, the error now lists every location searched
  so it's clear where to drop the file or which env var to set.
  Daemon spawn paths (`ctrlrelay poller start` re-exec under
  `--foreground`) pass the *resolved* absolute path to the child so
  the daemon doesn't break when launchd starts it from `/`.

## [0.2.0] - 2026-04-27

Minor release. Adds bulk repo operations driven by
`config/orchestrator.yaml`, plus a batch of dev-pipeline correctness
fixes (worktree ownership, PR-CI lock contention, watcher
persistence) and a docs cleanup.

### Added

- **`ctrlrelay repos clone-all/pull-all/status`** (closes #117).
  Stand up an isolated workspace from the orchestrator manifest in
  one command:

  ```
  ctrlrelay repos clone-all ~/code/myproject [--filter ORG] [--dry-run]
  ctrlrelay repos pull-all  ~/code/myproject [--filter ORG] [--dry-run]
  ctrlrelay repos status    ~/code/myproject [--filter ORG]
  ```

  Each repo lands at `DEST/<org>/<repo>` derived from the `name:`
  field; remote is `git@github.com:{name}.git`. The configured
  `local_path` is ignored when `DEST` is passed, so existing
  `~/Projects/...` checkouts stay untouched. Replaces the legacy
  `bkp/sync` shell scripts that broke when the manifest was
  archived during the rewrite.
- **`RepoConfig.name` validator.** Repo names are now validated
  against `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$` at config load.
  Rejects `..`, extra slashes, and shell metacharacters before they
  reach a clone target — defense in depth for the new bulk
  commands.

### Fixed

- **Watchers persist across poller restarts** (closes #57). Adds
  a `pr_watches` state-db table so in-flight merge watchers
  survive launchd kickstart, crashes, and reboots. Before this,
  any PR sitting in review across a poller restart silently lost
  its post-merge automation (issue auto-close + Telegram
  notification) for the rest of its 7-day window. The poller now
  rehydrates surviving rows on startup and spawns one watcher
  task per row.
- **Repo lock released during PR CI verification** (closes #29).
  `run_dev_issue` used to hold the per-repo lock through the
  `PRVerifier.wait_for_checks` polling window — a pure `gh` poll
  that can run for up to 30 minutes — which made every peer
  session targeting the same repo report "Repository locked by
  another session" while no git work was in flight. The lock is
  now released before `verify`, reacquired only if a `request_fix`
  follow-up needs to spawn an agent against the worktree, and
  released again on cleanup. `CancelledError` during the unlocked
  window propagates without leaking a lock row.
- **Branch ownership signal survives delete+recreate** (closes #51).
  `create_worktree_with_new_branch` now returns
  `(path, created_fresh)` so the caller knows whether THIS session
  created the branch (fresh from default, or via the stale-merged
  delete+recreate path). Before #51, `run_dev_issue` snapshotted
  `branch_preexisted` BEFORE the call; the snapshot went stale the
  moment the helper detected a fully-merged local branch and
  deleted+recreated it. A FAILED cleanup would then skip
  `delete_branch` and leak partial commits into the next retry.
- **Refuse reuse when branch still backs an open PR** (closes #52).
  `create_worktree_with_new_branch` now probes GitHub (via
  `GitHubCLI.list_prs(head=...)`) before reusing an existing local
  branch. If an open PR still backs it (prior DONE session whose
  PR is unmerged, or any external source), raises `WorktreeError`
  with the PR number and a concrete operator action instead of
  silently hijacking the reviewer's already-reviewed branch or
  tripping "A pull request already exists" at `gh pr create`.
- **`pull-all` checks subprocess return codes.** `git status`
  failure no longer treats an empty stdout as "clean" and proceeds
  to pull a corrupt repo. `git fetch` failure on a dirty tree is
  now reported as `failed` instead of silently being counted as
  `dirty — fetched only`.
- **`status` no longer crashes on edge cases.** `git rev-list`
  parsing wrapped in a helper that returns 0 on any non-zero
  return code or non-numeric output, instead of raising on
  `int(ahead)`.

### Changed

- **Docs use `com.example.*` placeholder for launchd labels**
  (closes #23). The launchd plist examples and `launchctl`
  commands no longer hard-code `com.ainvirion.ctrlrelay-*` as the
  job label. Anyone copying the docs verbatim picked up that label
  too, which is fine until two forks of the project share a
  machine. Swapped to `com.example.ctrlrelay-*` with a one-line
  note directing readers to use a reverse-DNS prefix they own.

### Operator notes

- Upgrade via `uv tool install ctrlrelay@latest --force` and
  restart poller + bridge.
  No schema or config changes — the new `pr_watches` state-db
  table is created idempotently on first start.
- New workflow: `ctrlrelay repos clone-all ~/code/myproject` to
  stand up a fresh workspace, `repos pull-all` to refresh it.
  Existing `~/Projects/...` checkouts are not touched.

## [0.1.12] - 2026-04-22

Patch release. Closes #90 — three related polish items on the
`ctrlrelay ci wait` helper / `PRVerifier.wait_for_checks` polling
loop, plus a fail-closed safety fix caught by codex on the PR.

### Fixed

- **Short timeouts now honored.** `wait_for_checks` used to block
  the full `poll_interval` before noticing a shorter `--timeout`
  budget was over. Repro: `ctrlrelay ci wait --timeout 1
  --interval 15` returned in ~15s instead of ~1s. Now the per-
  iteration sleep is capped at the remaining wall-clock deadline.
- **Transient `gh` errors no longer leak as tracebacks.**
  `asyncio.TimeoutError` from a hung `gh` subprocess used to
  surface as an unhandled Python stack trace. The polling loop now
  catches it (and `GitHubError`) inside the loop, logs
  `pr_verifier.transient_gh_error`, and retries the next tick.
- **Persistent `gh` failures now fail closed** (codex P1). If
  every poll errors up to the deadline and no successful read ever
  happened, `wait_for_checks` raises the last transient error
  instead of returning `[]`. Without this, the empty list was
  misread by callers as "no CI configured" and silently
  greenlighted PRs while GitHub was down.
- **Wall-clock deadline.** Switched from accumulated-sleep elapsed
  tracking to a monotonic `loop.time()` deadline, which is correct
  even with `poll_interval=0` and unaffected by wall-clock jumps.

### Operator notes

- Upgrade via `uv tool install ctrlrelay@latest --force` and
  restart poller + bridge. No schema or config changes.
- `ctrlrelay ci wait --pr <N> --timeout <s>` invocations with
  short timeouts will now return promptly. Existing dev-pipeline
  PR verification calls behave identically except in the
  GitHub-fully-down scenario, where they will now surface a clean
  failure instead of silently passing.

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

[Unreleased]: https://github.com/AInvirion/ctrlrelay/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/AInvirion/ctrlrelay/compare/v0.5.0...v0.6.0
[0.1.3]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.3
[0.1.1]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.1
[0.1.0]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.0
