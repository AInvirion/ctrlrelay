# ctrlrelay

[![Tests and lint](https://github.com/AInvirion/ctrlrelay/actions/workflows/test.yml/badge.svg)](https://github.com/AInvirion/ctrlrelay/actions/workflows/test.yml)
[![Build](https://github.com/AInvirion/ctrlrelay/actions/workflows/build.yml/badge.svg)](https://github.com/AInvirion/ctrlrelay/actions/workflows/build.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/ctrlrelay/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Local-first orchestrator that drives headless Claude Code (`claude -p`) across
your GitHub repos. Watches for assigned issues, runs a dev pipeline in an
isolated git worktree, opens a PR, and asks you on Telegram when it gets stuck.

---

## Why

Claude Code is great interactively. Running it across half a dozen repos
without staring at a terminal is a different problem: who schedules the
runs, who watches for new work, who hands you the "I'm blocked, what do
you want?" question, who tracks the PR until it merges.

`ctrlrelay` is a small daemon that does all of that on your laptop. It's
local-first on purpose: no server, no queue, no multi-tenant anything. Your
Claude subscription, your GitHub credentials, your repos, your machine.

## Features

- **Issue poller** — detects issues assigned to you across every configured
  repo, spawns a Claude Code dev session in a dedicated git worktree, and
  opens a PR.
- **Telegram bridge** — when a session hits a blocking question, the bridge
  relays it to you as a DM and resumes the session once you reply.
- **PR watcher** — tracks the PR to merge and closes the loop with a
  notification when your PR ships.
- **In-process scheduler** (APScheduler) — runs periodic jobs inside the
  poller daemon. The built-in `secops` job reviews Dependabot alerts and PRs
  across every repo daily at 6am; cron expressions are standard 5-field with
  proper Vixie semantics (Sun=0, DOM-OR-DOW, etc.).
- **Checkpoint protocol** — Claude writes a structured state file at the
  end of every session so the orchestrator knows whether it succeeded,
  failed, or is blocked on input.
- **Cross-platform supervision** — launchd (macOS) and systemd (Linux)
  plist/unit examples in `docs/operations.md`. One codebase, identical
  behavior.

## How it works

```
             ┌───────────────┐
             │  GitHub API   │
             └───────┬───────┘
                     │ (poll: issues assigned to me)
             ┌───────▼───────┐         ┌──────────────┐
             │ poller daemon │◄────────┤   APScheduler│
             │  (launchd /   │         │ (secops cron)│
             │   systemd)    │         └──────────────┘
             └───┬───────┬───┘
      new issue  │       │  blocked session
                 │       │
      ┌──────────▼──┐  ┌─▼───────────────────┐
      │ dev pipeline│  │ Telegram bridge     │
      │ in worktree │  │ (socket ↔ bot API)  │
      │   Claude -p │  └──────────┬──────────┘
      └──────┬──────┘             │
             │ PR opened          │ DM you
             ▼                    ▼
      ┌────────────┐         ┌─────────┐
      │ PR watcher │         │   You   │
      └────────────┘         └─────────┘
```

Under the hood it's Python + `asyncio` + `sqlite` for state, shelling out to
`claude -p` for the model work and `gh` for GitHub. No web server, no queue,
no database dependency — just a launchd/systemd-supervised daemon.

## Install

Requires Python 3.12+, the [`claude` CLI][claude-cli], the [`gh` CLI][gh-cli], and `git` 2.20+.

**From PyPI** (once published):

```bash
pip install ctrlrelay
# or: uv pip install ctrlrelay
```

**From source** (current path while in alpha):

```bash
git clone https://github.com/AInvirion/ctrlrelay.git
cd ctrlrelay
uv pip install -e .   # or: pip install -e .
```

## Quick start

```bash
# Copy and edit the example config:
cp config/orchestrator.yaml.example config/orchestrator.yaml

# Validate it:
ctrlrelay config validate

# Run the dev pipeline against an issue you're assigned:
ctrlrelay run dev --issue 42 --repo your-org/your-repo

# Or start the poller to auto-process newly assigned issues + run the
# scheduled secops sweep daily at 6am:
ctrlrelay poller start   # daemonizes; returns the terminal
ctrlrelay poller status  # verify it's running
```

Run as a supervised daemon (launchd on macOS / systemd on Linux) — see
[operations docs][ops-docs].

## Documentation

- [Getting started][docs-start]
- [Configuration][docs-config]
- [Telegram bridge][docs-bridge]
- [Feedback loop][docs-feedback]
- [CLI reference][docs-cli]
- [Operations (launchd / systemd / scheduled jobs)][ops-docs]
- [Architecture][docs-arch]
- [Development][docs-dev]

## Contributing

Bug reports, PRs, and design discussion all welcome. Please read
[`SECURITY.md`](SECURITY.md) before filing anything that looks like a
vulnerability — use a private GitHub advisory instead.

## License

MIT — see [`LICENSE`](LICENSE).

[claude-cli]: https://docs.anthropic.com/claude/docs/claude-cli
[gh-cli]: https://cli.github.com/
[docs-start]: https://ainvirion.github.io/ctrlrelay/getting-started/
[docs-config]: https://ainvirion.github.io/ctrlrelay/configuration/
[docs-bridge]: https://ainvirion.github.io/ctrlrelay/bridge/
[docs-feedback]: https://ainvirion.github.io/ctrlrelay/feedback-loop/
[docs-cli]: https://ainvirion.github.io/ctrlrelay/cli/
[ops-docs]: https://ainvirion.github.io/ctrlrelay/operations/
[docs-arch]: https://ainvirion.github.io/ctrlrelay/architecture/
[docs-dev]: https://ainvirion.github.io/ctrlrelay/development/
