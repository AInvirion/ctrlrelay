---
title: Configuration
layout: default
nav_order: 3
description: "Full schema for orchestrator.yaml — every key, default, and how it is used."
permalink: /configuration/
---

# Configuration reference

dev-sync is configured by a single YAML file, `config/orchestrator.yaml`. The
default path can be overridden with `--config` / `-c` on every CLI command.

This page documents every recognised key. The authoritative source is the
pydantic schema in
[`src/dev_sync/core/config.py`](https://github.com/AInvirion/dev-sync/blob/main/src/dev_sync/core/config.py).

## Top-level keys

```yaml
version: "1"
node_id: "my-laptop"
timezone: "America/New_York"

paths:        { ... }
claude:       { ... }
transport:    { ... }
dashboard:    { ... }
repos:        [ ... ]
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `version` | string | no | `"1"` | Config schema version. Currently always `"1"`. |
| `node_id` | string | **yes** | — | Free-form identifier for this machine. Surfaces in dashboard heartbeats and session logs. |
| `timezone` | string | no | `"UTC"` | IANA timezone (e.g. `America/Santiago`). Used for scheduling. |
| `paths` | object | **yes** | — | See [paths](#paths). |
| `claude` | object | no | (defaults) | See [claude](#claude). |
| `transport` | object | **yes** | — | See [transport](#transport). |
| `dashboard` | object | no | (defaults) | See [dashboard](#dashboard). |
| `repos` | list | no | `[]` | See [repos](#repos). |

## paths

All paths support `~` expansion.

```yaml
paths:
  state_db:    "~/.dev-sync/state.db"
  worktrees:   "~/.dev-sync/worktrees"
  bare_repos:  "~/.dev-sync/repos"
  contexts:    "~/.dev-sync/contexts"
  skills:      "~/.claude/skills"
```

| Key | Type | Required | Description |
|---|---|---|---|
| `state_db` | path | **yes** | SQLite database for sessions, locks, telegram_pending, automation_decisions. |
| `worktrees` | path | **yes** | Where dev-sync creates per-session `git worktree` directories. |
| `bare_repos` | path | **yes** | Where dev-sync clones bare mirrors of each configured repo. |
| `contexts` | path | **yes** | Per-repo context directory (looked up as `<contexts>/<owner-repo>/CLAUDE.md`). If a `CLAUDE.md` exists, it is symlinked into the worktree at session start. |
| `skills` | path | **yes** | Claude Code skills directory used by `dev-sync skills audit` and `dev-sync skills list`. |

## claude

Controls how dev-sync invokes the `claude` CLI.

```yaml
claude:
  binary: "claude"
  default_timeout_seconds: 1800
  output_format: "json"
```

| Key | Type | Default | Description |
|---|---|---|---|
| `binary` | string | `"claude"` | Path to the `claude` executable. The bare name `"claude"` is auto-resolved at startup using `shutil.which("claude")`, then `~/.local/bin/claude`, `/usr/local/bin/claude`, `/opt/homebrew/bin/claude`. Set an absolute path to skip lookup (useful under launchd/systemd where PATH is minimal). |
| `default_timeout_seconds` | int | `1800` | Per-session timeout passed to `asyncio.wait_for`. Sessions that exceed this are killed and reported as failed. |
| `output_format` | string | `"json"` | Forwarded as `--output-format` to `claude -p`. |

dev-sync always invokes claude with `--dangerously-skip-permissions` so the
agent does not pause on tool-permission prompts in headless runs. This is
intentional — the orchestrator runs unattended.

## transport

The transport carries `BLOCKED_NEEDS_INPUT` questions out of dev-sync to a
human and routes the answer back. Pick one of two types.

```yaml
transport:
  type: "telegram"   # or "file_mock"
  telegram:
    bot_token_env: "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: 123456789
    socket_path: "~/.dev-sync/dev-sync.sock"
  file_mock:
    inbox:  "~/.dev-sync/inbox.txt"
    outbox: "~/.dev-sync/outbox.txt"
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | enum | no | `"file_mock"` | One of `"telegram"`, `"file_mock"`. |
| `telegram` | object | required when `type=telegram` | — | Telegram bridge settings — see below. |
| `file_mock` | object | required when `type=file_mock` | — | Local-file fake transport, used for tests/dev. |

### transport.telegram

| Key | Type | Default | Description |
|---|---|---|---|
| `bot_token_env` | string | `"DEV_SYNC_TELEGRAM_TOKEN"` | Name of the environment variable holding the bot token. dev-sync never reads the token directly — only the variable name. |
| `chat_id` | int | `0` | Telegram chat ID the bridge sends messages to and accepts replies from. |
| `socket_path` | path | `"~/.dev-sync/dev-sync.sock"` | Unix socket path the bridge listens on. Pipelines connect to this socket as clients. |

See [Telegram bridge]({{ '/bridge/' | relative_url }}) for the full setup walkthrough.

### transport.file_mock

| Key | Type | Required | Description |
|---|---|---|---|
| `inbox` | path | yes | File the orchestrator writes outgoing questions to. |
| `outbox` | path | yes | File the orchestrator reads answers from. |

`file_mock` is a non-interactive stand-in suitable for tests and local
experimentation. It has no resume-on-answer flow.

## dashboard

Optional remote dashboard for heartbeats and event push.

```yaml
dashboard:
  enabled: false
  url: "https://dev-sync-dashboard.example.com"
  auth_token_env: "DEV_SYNC_DASHBOARD_TOKEN"
  sync_config_on_heartbeat: false
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Set to `false` to skip dashboard wiring entirely. |
| `url` | string | `""` | Base URL of the dashboard service. Empty disables outbound calls. |
| `auth_token_env` | string | `"DEV_SYNC_DASHBOARD_TOKEN"` | Env-var name holding the dashboard auth token. |
| `sync_config_on_heartbeat` | bool | `false` | When true, the orchestrator pushes its current config alongside each heartbeat. |

The dashboard is optional. Leaving `url` empty (the default) is the supported
no-op configuration.

## repos

A list of repositories the orchestrator manages.

```yaml
repos:
  - name: "your-org/your-repo"
    local_path: "~/Projects/your-repo"
    dev_branch_template: "fix/issue-{n}"
    automation:
      dependabot_patch: auto
      dependabot_minor: ask
      dependabot_major: never
      codeql_dismiss: ask
      secret_alerts: never
      deploy_after_merge: auto
    code_review: { ... }      # optional
    deploy:      { ... }      # optional
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | **yes** | — | GitHub `owner/repo` slug. Used for `gh` calls and bare-repo / worktree naming. |
| `local_path` | path | **yes** | — | Where the repo is checked out on disk for human use. dev-sync itself uses bare mirrors under `paths.bare_repos`. |
| `dev_branch_template` | string | no | `"fix/issue-{n}"` | Branch-name template for dev-pipeline runs. `{n}` is replaced by the issue number. |
| `automation` | object | no | (defaults) | See [automation](#repos-automation). |
| `code_review` | object | no | (defaults) | Reserved for code-review policy. Currently unused by the bundled pipelines. |
| `deploy` | object | no | `null` | Reserved for deploy policy. Currently surfaced in `dev-sync config repos` but otherwise inert. |

### repos[].automation

Each key takes one of three policies: `auto` (act without asking), `ask` (pause
and ask the operator), or `never` (skip).

| Key | Default | Description |
|---|---|---|
| `dependabot_patch` | `auto` | Patch-version dependency bumps. |
| `dependabot_minor` | `ask` | Minor-version bumps. |
| `dependabot_major` | `never` | Major-version bumps. |
| `codeql_dismiss` | `ask` | CodeQL alert dismissal. |
| `secret_alerts` | `never` | Secret-scan alerts. |
| `deploy_after_merge` | `auto` | Whether to deploy after a merged PR. |

The current secops and dev pipelines read these settings to bias their prompts
to Claude — they're not enforced by hard-coded checks.

## Example: telegram-enabled config

```yaml
version: "1"
node_id: "studio-mac"
timezone: "America/Santiago"

paths:
  state_db:    "~/.dev-sync/state.db"
  worktrees:   "~/.dev-sync/worktrees"
  bare_repos:  "~/.dev-sync/repos"
  contexts:    "~/.dev-sync/contexts"
  skills:      "~/.claude/skills"

claude:
  binary: "/opt/homebrew/bin/claude"
  default_timeout_seconds: 3600
  output_format: "json"

transport:
  type: "telegram"
  telegram:
    bot_token_env: "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: 987654321
    socket_path: "~/.dev-sync/dev-sync.sock"

dashboard:
  enabled: false
  url: ""

repos:
  - name: "your-org/your-app"
    local_path: "~/Projects/your-app"
    automation:
      dependabot_patch: auto
      dependabot_minor: ask
      dependabot_major: never
```

## Validating

Always run `dev-sync config validate` after editing the file. It prints the
resolved transport, repo count, and parsed timezone — and surfaces any pydantic
validation errors with line context.
