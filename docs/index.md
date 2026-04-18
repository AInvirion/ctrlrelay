---
title: Home
layout: default
nav_order: 1
description: "dev-sync — local-first orchestrator that turns assigned GitHub issues into reviewed PRs by driving Claude Code in the background."
permalink: /
---

# dev-sync
{: .fs-9 }

Local-first orchestrator that drives headless Claude Code (`claude -p`) across
your GitHub repos. Watches for assigned issues, runs the dev pipeline in an
isolated git worktree, opens a PR, and asks you on Telegram when it gets stuck.
{: .fs-6 .fw-300 }

[Get started]({{ '/getting-started/' | relative_url }}){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[Configure]({{ '/configuration/' | relative_url }}){: .btn .fs-5 .mb-4 .mb-md-0 .mr-2 }
[Set up the bridge]({{ '/bridge/' | relative_url }}){: .btn .fs-5 .mb-4 .mb-md-0 }

---

## Use it

- **[Getting started]({{ '/getting-started/' | relative_url }})** — install,
  write your first config, run the dev pipeline against an issue.
- **[Configuration]({{ '/configuration/' | relative_url }})** — every key in
  `orchestrator.yaml`, with defaults and what they actually do.
- **[Telegram bridge]({{ '/bridge/' | relative_url }})** — BotFather walkthrough,
  starting the bridge, troubleshooting Telegram delivery.
- **[Feedback loop]({{ '/feedback-loop/' | relative_url }})** — how
  `BLOCKED_NEEDS_INPUT` checkpoints reach you and how your reply resumes the
  paused Claude session.
- **[CLI reference]({{ '/cli/' | relative_url }})** — every subcommand and
  flag.
- **[Operations]({{ '/operations/' | relative_url }})** — running the bridge
  and poller under launchd (macOS) or systemd (Linux), tailing logs, reading
  the state DB.

## Build on it

- **[Architecture]({{ '/architecture/' | relative_url }})** — layered
  overview, dispatcher ↔ Claude contract, state DB shape, worktree lifecycle.
- **[Development]({{ '/development/' | relative_url }})** — local dev setup,
  tests, ruff, contributing.

## What is this?

dev-sync sits between GitHub and your laptop's `claude` install. The poller
checks each configured repo for issues newly assigned to you. When it sees one,
it acquires a per-repo lock, creates a worktree on a fresh branch, spawns
`claude -p` with the issue title and body in the prompt, and lets the agent
work TDD-style toward a PR. When the agent gets stuck, it writes a
checkpoint, the orchestrator routes the question to your Telegram chat, and
your reply resumes the same Claude session.

Everything runs locally. State lives in a SQLite file. Bridge state lives in
a Unix socket. The Telegram bot is the only outbound dependency, and it's
optional — `dev-sync` will run without it for local testing.

---

## Reference

Original design docs and implementation plans now live under
[Design & history]({{ '/reference/' | relative_url }}). Useful for context;
not the right entry point if you just want to use the tool.
