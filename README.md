# ctrlrelay

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests and lint](https://github.com/AInvirion/ctrlrelay/actions/workflows/test.yml/badge.svg)](https://github.com/AInvirion/ctrlrelay/actions/workflows/test.yml)
[![Build](https://github.com/AInvirion/ctrlrelay/actions/workflows/build.yml/badge.svg)](https://github.com/AInvirion/ctrlrelay/actions/workflows/build.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/ctrlrelay/)
[![GitHub Issues](https://img.shields.io/github/issues/AInvirion/ctrlrelay.svg)](https://github.com/AInvirion/ctrlrelay/issues)
[![GitHub Pull Requests](https://img.shields.io/github/issues-pr/AInvirion/ctrlrelay.svg)](https://github.com/AInvirion/ctrlrelay/pulls)

> Local-first orchestrator for headless coding agents across your GitHub
> repos. Watches for assigned issues, runs a dev pipeline in an isolated
> git worktree, opens a PR, and asks you on Telegram when it gets stuck.

## Table of Contents

- [About](#about)
- [Features](#features)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Quick start](#quick-start)
- [Documentation](#documentation)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## About

Headless coding agents are great interactively. Running them across half
a dozen repos without staring at a terminal is a different problem: who
schedules the runs, who watches for new work, who hands you the "I'm
blocked, what do you want?" question, who tracks the PR until it merges.

`ctrlrelay` is a small daemon that does all of that on your laptop. It
is local-first on purpose: no server, no queue, no multi-tenant anything.
Your agent credentials, your GitHub credentials, your repos, your
machine.

Today `ctrlrelay` ships with a Claude Code (`claude -p`) backend. The
orchestrator layer — worktrees, state DB, scheduler, Telegram bridge —
is agent-agnostic, and plug-in backends for other headless coding agents
are on the roadmap (see [Roadmap](#roadmap)).

## Features

- **Issue poller.** Detects issues across every configured repo
  (either assigned to you, or carrying a configurable opt-in label like
  `ctrlrelay:auto`), spawns a dev session in a dedicated git worktree,
  and opens a PR. Label triggers let a teammate without rights on your
  account flag an issue as safe for the bot to pick up —
  see [`include_labels`][docs-config].
- **Telegram bridge.** When a session hits a blocking question, the
  bridge relays it to you as a DM and resumes the session once you
  reply.
- **PR watcher.** Tracks the opened PR to merge and closes the loop
  with a Telegram notification.
- **In-process scheduler** (APScheduler). Runs periodic jobs inside
  the poller daemon. Ships with a `secops` job that reviews Dependabot
  alerts and PRs daily at 6am; cron expressions follow standard Vixie
  5-field semantics (Sun=0, DOM-OR-DOW).
- **Checkpoint protocol.** The agent writes a structured state file
  at the end of every session so the orchestrator knows whether it
  succeeded, failed, or is blocked on input. Agent backends implement
  this protocol to integrate.
- **Cross-platform supervision.** launchd (macOS) and systemd (Linux)
  examples in `docs/operations.md`. One codebase, identical behavior.

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
      │ agent CLI   │  └──────────┬──────────┘
      └──────┬──────┘             │
             │ PR opened          │ DM you
             ▼                    ▼
      ┌────────────┐         ┌─────────┐
      │ PR watcher │         │   You   │
      └────────────┘         └─────────┘
```

Under the hood it's Python + `asyncio` + `sqlite` for state. The agent
is invoked as a subprocess (today: `claude -p`), and GitHub is accessed
via the `gh` CLI. No web server, no queue, no database dependency — just
a launchd/systemd-supervised daemon.

## Getting started

### Prerequisites

- **Python 3.12+**
- **git 2.20+**
- The **[`gh` CLI][gh-cli]**, authenticated (`gh auth login`) — used for
  all GitHub API calls.
- A **headless coding agent backend.** Today that means the
  **[`claude` CLI][claude-cli]**, authenticated (`claude auth login`).
  Future backends will document their own setup.
- *(Optional, for the `secops` pipeline)* the **[`codex` CLI][codex-cli]**,
  authenticated. The secops pipeline invokes `codex review` as an
  independent reviewer for the agent's output; you can disable it by
  setting `code_review.method: "none"` in your config if you prefer to
  skip the review step.
- *(Optional)* a Telegram bot token if you want the bridge — see
  [Telegram bridge docs][docs-bridge].

### Installation

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

### Quick start

```bash
# Copy and edit the example config:
cp config/orchestrator.yaml.example config/orchestrator.yaml

# Validate it:
ctrlrelay config validate

# Run the dev pipeline against an issue you're assigned:
ctrlrelay run dev --issue 42 --repo your-org/your-repo

# Or start the poller to auto-process newly assigned issues + run the
# scheduled secops sweep daily at 6am:
ctrlrelay poller start      # daemonizes; returns the terminal
ctrlrelay poller status     # verify it's running
```

Run as a supervised daemon (launchd on macOS / systemd on Linux) — see
[Operations][ops-docs].

## Documentation

- [Getting started][docs-start]
- [Configuration][docs-config]
- [Telegram bridge][docs-bridge]
- [Feedback loop][docs-feedback]
- [CLI reference][docs-cli]
- [Operations (launchd / systemd / scheduled jobs)][ops-docs]
- [Architecture][docs-arch]
- [Development][docs-dev]

## Roadmap

- **Multi-agent backend support.** The agent dispatcher is the seam we
  intend to widen so `ctrlrelay` can drive alternative headless coding
  agents (e.g. OpenAI Codex CLI, OpenCode, Hermes) alongside Claude
  Code. Each backend will implement the same checkpoint protocol and
  be selectable per repo via config.
- **Additional scheduled jobs.** The in-process scheduler already has
  `secops`; follow-ups include a weekly activity summary and a stale-
  session reaper.
- **Dashboard mode.** An optional, opt-in heartbeat push to a hosted
  dashboard for operators running the daemon across many machines.

## Contributing

We welcome contributions from the community! Please read our
[Contributing Guidelines](CONTRIBUTING.md) before submitting a pull
request, and abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

First-time contributors will be prompted by the CLA Assistant bot to
sign the Contributor Assignment Agreement in-PR — it's a one-time,
one-comment step.

## Security

If you discover a security vulnerability, please follow our
[Security Policy](SECURITY.md). Please do not file public GitHub issues
for security reports — open a private advisory instead.

## License

This project is licensed under the Apache License 2.0 — see the
[LICENSE](LICENSE) file for details.

Copyright (c) 2026 AInvirion LLC. All Rights Reserved.

[claude-cli]: https://docs.anthropic.com/claude/docs/claude-cli
[gh-cli]: https://cli.github.com/
[codex-cli]: https://github.com/openai/codex
[docs-start]: https://ainvirion.github.io/ctrlrelay/getting-started/
[docs-config]: https://ainvirion.github.io/ctrlrelay/configuration/
[docs-bridge]: https://ainvirion.github.io/ctrlrelay/bridge/
[docs-feedback]: https://ainvirion.github.io/ctrlrelay/feedback-loop/
[docs-cli]: https://ainvirion.github.io/ctrlrelay/cli/
[ops-docs]: https://ainvirion.github.io/ctrlrelay/operations/
[docs-arch]: https://ainvirion.github.io/ctrlrelay/architecture/
[docs-dev]: https://ainvirion.github.io/ctrlrelay/development/
