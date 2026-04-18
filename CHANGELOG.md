# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/AInvirion/ctrlrelay/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.1
[0.1.0]: https://github.com/AInvirion/ctrlrelay/releases/tag/v0.1.0
