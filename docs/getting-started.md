---
title: Getting Started
layout: default
nav_order: 2
description: "Install ctrlrelay, configure your first repo, and run the dev pipeline end-to-end."
permalink: /getting-started/
---

# Getting started

In about 15 minutes you will install ctrlrelay, point it at your repos, and
watch it turn a real GitHub issue into a pull request.

## Before you start

You need:

- **macOS or Linux.** Windows is not supported.
- **Python 3.12 or newer.**
- **git 2.20 or newer** — ctrlrelay gives every issue its own `git worktree`.
- **The [GitHub CLI](https://cli.github.com/) (`gh`)**, logged in with
  `gh auth login`. ctrlrelay makes every GitHub call through it.
- **A coding agent CLI**, logged in:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) — the default, or
  - [OpenCode](https://opencode.ai) (`opencode`) — can point at a local model.
    Pick it per repo with `repos[].agent_type`; see
    [Configuration]({{ '/configuration/' | relative_url }}).

Nice to have:

- **A chat for questions** — a Telegram bot, or a bot account on your
  Mattermost server. Without one, ctrlrelay still runs, but a blocked agent
  cannot get your answer. See [Chat bridge]({{ '/bridge/' | relative_url }}).
- **A reviewer CLI** for the automatic pull request review. Turn it off with
  `code_review.method: "off"` if you don't want it; see
  [Configuration]({{ '/configuration/' | relative_url }}).
- [`uv`](https://github.com/astral-sh/uv) for faster installs.

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

`--yes` accepts every default and never prompts. The chat token is read from
the variable your transport names — `$CTRLRELAY_TELEGRAM_TOKEN` for Telegram,
`$CTRLRELAY_MATTERMOST_TOKEN` for Mattermost. Export it before running setup if
you want it baked into the rendered launchd or systemd unit.

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

The dev pipeline takes a GitHub issue, starts your coding agent in an isolated
git worktree, and opens a PR. Pick a small issue assigned to you (or create one to
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
but cannot resume. Switch to the Telegram or Mattermost transport to actually
answer — see [Chat bridge]({{ '/bridge/' | relative_url }}) and [Feedback loop]({{ '/feedback-loop/' | relative_url }}).

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

## Let teammates hand it work

By default only your own account (the one `gh` is logged in as) can start a
run: by assigning an issue to itself, or by adding a trigger label. To let a
teammate do the same, list their GitHub login:

```yaml
intake:
  allowed_accounts: ["teammate-login"]     # every repo

repos:
  - name: "your-org/your-repo"
    automation:
      allowed_accounts: ["teammate-login"] # this repo only; replaces the list above
```

The teammate then assigns the issue **to your account**, or adds one of your
`include_labels`. If the repo uses `require_labels`, the issue must carry
that label too. Anyone not on the list is ignored, and the status page shows
their issue as "assigned by someone else". Restart the poller after editing
the file. Details: [Configuration: intake]({{ '/configuration/' | relative_url }}#intake).

## Where to next

- [Configuration]({{ '/configuration/' | relative_url }}) — every key in `orchestrator.yaml`.
- [Chat bridge]({{ '/bridge/' | relative_url }}) — how to set up the human-in-the-loop channel.
- [Feedback loop]({{ '/feedback-loop/' | relative_url }}) — what `BLOCKED_NEEDS_INPUT` actually does end-to-end.
- [CLI reference]({{ '/cli/' | relative_url }}) — every subcommand and flag.
- [Operations]({{ '/operations/' | relative_url }}) — running ctrlrelay as a service.
- [Personalization sync]({{ '/personalization/' | relative_url }}) — sync your Claude config, per-project memory, and spec outputs across machines.
