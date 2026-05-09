---
title: Getting Started
layout: default
nav_order: 2
description: "Install ctrlrelay, configure your first repo, and run the dev pipeline end-to-end."
permalink: /getting-started/
---

# Getting started

This page walks through installing ctrlrelay, writing a minimal `orchestrator.yaml`,
and running the dev pipeline against a real GitHub issue. Allow about 15 minutes.

## Requirements

- **Python 3.12+** — the package targets `requires-python = ">=3.12"`.
- **`claude` CLI** — Claude Code installed and authenticated. ctrlrelay shells out
  to `claude -p ...`. See [Claude Code installation](https://docs.anthropic.com/claude/docs/claude-code).
- **`gh` CLI** — GitHub CLI installed and authenticated (`gh auth login`). ctrlrelay
  uses `gh` for all GitHub API calls.
- **`git` 2.20+** — for `git worktree add`.
- A unix-like shell (macOS or Linux). Windows is not supported.

Optional:

- [`uv`](https://github.com/astral-sh/uv) for faster installs.
- A Telegram bot if you want the human-in-the-loop bridge — see [Telegram bridge]({{ '/bridge/' | relative_url }}).

## Install

The recommended install is via `pipx`:

```bash
pipx install ctrlrelay
ctrlrelay --version
```

Use `uv tool install ctrlrelay` if you prefer uv. For local development, see
[Development]({{ '/development/' | relative_url }}).

## First-run setup

Since v0.4.0, `ctrlrelay setup` does the boring parts of onboarding for you:
detect every GitHub org you belong to, list the non-fork non-archived repos
in each, write a fresh `orchestrator.yaml` to `~/.config/ctrlrelay/`, and
clone every repo to `~/Projects/<owner.lower()>/<repo>`. It can also
configure the personalization sync block and render daemon unit files for
launchd (macOS) or systemd (Linux).

Interactive (recommended for the first machine you set up):

```bash
ctrlrelay setup
```

Pick which owners to monitor when prompted. Setup will then enumerate repos,
write the config, clone everything, and ask whether you want personalization
sync and background services.

Non-interactive (good for second/third machines once you know the answers):

```bash
ctrlrelay setup --yes \
  --owner alice \
  --owner AInvirion \
  --transport telegram \
  --telegram-chat-id 12345 \
  --personalization-repo alice/dotclaude \
  --install-daemons
```

`--yes` accepts every default and never prompts. The Telegram bot token is
read from `$CTRLRELAY_TELEGRAM_TOKEN`; export it before running setup if you
want it baked into the rendered launchd plist.

After setup completes, validate:

```bash
ctrlrelay config validate
```

You should see something like:

```
✓ Config valid: ~/.config/ctrlrelay/orchestrator.yaml
  Node ID: your-hostname.local
  Timezone: UTC
  Transport: telegram
  Repos: 86
```

For the full schema (every key, every default), see
[Configuration]({{ '/configuration/' | relative_url }}). To edit the file
later, open `~/.config/ctrlrelay/orchestrator.yaml`; ctrlrelay re-reads it on
every command. Re-running `ctrlrelay setup` refuses to clobber an existing
config without `--force`.

`node_id` is optional in the generated config — when omitted it defaults to
your machine's hostname (`socket.gethostname()`). Set it explicitly only if
the hostname is meaningless (CI runners, containers).

## Your first dev-pipeline run

The dev pipeline takes a GitHub issue, spawns Claude Code in an isolated git
worktree, and opens a PR. Pick a small issue assigned to you (or create one to
test against), then:

```bash
ctrlrelay run dev --issue 42 --repo your-org/your-repo
```

What happens:

1. ctrlrelay acquires a per-repo lock so only one session runs against the repo
   at a time.
2. It clones the repo into `paths.bare_repos` (if not already there) and creates
   a worktree under `paths.worktrees` on a branch named from
   `repos[].dev_branch_template` (default `fix/issue-{n}`).
3. It spawns `claude -p ...` inside the worktree with the issue title/body and a
   structured prompt that instructs Claude to TDD-implement the change, push the
   branch, and open a PR.
4. Claude writes a checkpoint JSON file (`DONE`, `BLOCKED_NEEDS_INPUT`, or
   `FAILED`) when it finishes. ctrlrelay reads the checkpoint and reports the
   outcome.
5. On success, ctrlrelay removes the worktree (the branch stays — the open PR
   references it). On failure, it cleans up.

If Claude blocks asking a question, the answer mechanism only works when a
transport is connected. With `file_mock`, you'll see the question on the console
but cannot resume. Switch to the Telegram transport to actually answer — see
[Telegram bridge]({{ '/bridge/' | relative_url }}) and [Feedback loop]({{ '/feedback-loop/' | relative_url }}).

## Your first poller tick

The poller watches your configured repos for newly assigned issues and runs the
dev pipeline against each one. To run it interactively:

```bash
ctrlrelay poller start --interval 60
```

On the very first run the poller seeds its "seen" set with whatever is currently
assigned to you, so it does not replay the backlog. From that point onward, only
issues assigned _after_ the poller started will trigger a dev-pipeline run.

Hit Ctrl+C to stop. The poller's seen-issue state is persisted to
`{paths.state_db parent}/poller_state.json` so a restart picks up where it left off.

To run the poller as a long-lived background service, see [Operations]({{ '/operations/' | relative_url }}).

## Where to next

- [Configuration]({{ '/configuration/' | relative_url }}) — every key in `orchestrator.yaml`.
- [Telegram bridge]({{ '/bridge/' | relative_url }}) — how to set up the human-in-the-loop channel.
- [Feedback loop]({{ '/feedback-loop/' | relative_url }}) — what `BLOCKED_NEEDS_INPUT` actually does end-to-end.
- [CLI reference]({{ '/cli/' | relative_url }}) — every subcommand and flag.
- [Operations]({{ '/operations/' | relative_url }}) — running ctrlrelay as a service.
- [Personalization sync]({{ '/personalization/' | relative_url }}) — sync your Claude config, per-project memory, and spec outputs across machines.
