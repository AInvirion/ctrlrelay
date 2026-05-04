---
title: Configuration
layout: default
nav_order: 3
description: "Full schema for orchestrator.yaml — every key, default, and how it is used."
permalink: /configuration/
---

# Configuration reference

ctrlrelay is configured by a single YAML file, `config/orchestrator.yaml`. The
default path can be overridden with `--config` / `-c` on every CLI command.

This page documents every recognised key. The authoritative source is the
pydantic schema in
[`src/ctrlrelay/core/config.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/config.py).

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
| `node_id` | string | no | `socket.gethostname()` | Free-form identifier for this machine. Surfaces in dashboard heartbeats and session logs. Defaults to the OS hostname when omitted, null, or blank — set explicitly only if the hostname is meaningless (CI runners, ephemeral containers). |
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
  state_db:    "~/.ctrlrelay/state.db"
  worktrees:   "~/.ctrlrelay/worktrees"
  bare_repos:  "~/.ctrlrelay/repos"
  contexts:    "~/.ctrlrelay/contexts"
  skills:      "~/.claude/skills"
  # Optional convention for repos[].local_path:
  repo_root:   "~/Projects"
  owner_aliases:
    AInvirion: AINVIRION       # GitHub owner -> on-disk folder name
    SemClone: SEMCL.ONE
```

| Key | Type | Required | Description |
|---|---|---|---|
| `state_db` | path | **yes** | SQLite database for sessions, locks, telegram_pending, automation_decisions. |
| `worktrees` | path | **yes** | Where ctrlrelay creates per-session `git worktree` directories. |
| `bare_repos` | path | **yes** | Where ctrlrelay clones bare mirrors of each configured repo. |
| `contexts` | path | **yes** | Per-repo context directory (looked up as `<contexts>/<owner-repo>/CLAUDE.md`). If a `CLAUDE.md` exists, it is symlinked into the worktree at session start. |
| `skills` | path | **yes** | Claude Code skills directory used by `ctrlrelay skills audit` and `ctrlrelay skills list`. |
| `repo_root` | path | no | Convention root for repo clones. When set, `repos[].local_path` may be omitted and is derived as `${repo_root}/${owner_aliases.get(owner, owner)}/${repo}`. Without `repo_root`, every repo entry must declare its own `local_path` (legacy behaviour). |
| `owner_aliases` | object | no | Map of GitHub owner -> on-disk folder name. Lets the convention work when local folders use a vanity name (`SemClone` repos under `~/Projects/SEMCL.ONE/`). Lookup falls through to the literal owner if not present. |

## claude

Controls how ctrlrelay invokes the `claude` CLI.

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

ctrlrelay always invokes claude with `--dangerously-skip-permissions` so the
agent does not pause on tool-permission prompts in headless runs. This is
intentional — the orchestrator runs unattended.

## transport

The transport carries `BLOCKED_NEEDS_INPUT` questions out of ctrlrelay to a
human and routes the answer back. Pick one of two types.

```yaml
transport:
  type: "telegram"   # or "file_mock"
  telegram:
    bot_token_env: "CTRLRELAY_TELEGRAM_TOKEN"
    chat_id: 123456789
    socket_path: "~/.ctrlrelay/ctrlrelay.sock"
  file_mock:
    inbox:  "~/.ctrlrelay/inbox.txt"
    outbox: "~/.ctrlrelay/outbox.txt"
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | enum | no | `"file_mock"` | One of `"telegram"`, `"file_mock"`. |
| `telegram` | object | required when `type=telegram` | — | Telegram bridge settings — see below. |
| `file_mock` | object | required when `type=file_mock` | — | Local-file fake transport, used for tests/dev. |

### transport.telegram

| Key | Type | Default | Description |
|---|---|---|---|
| `bot_token_env` | string | `"CTRLRELAY_TELEGRAM_TOKEN"` | Name of the environment variable holding the bot token. ctrlrelay never reads the token directly — only the variable name. |
| `chat_id` | int | `0` | Telegram chat ID the bridge sends messages to and accepts replies from. |
| `socket_path` | path | `"~/.ctrlrelay/ctrlrelay.sock"` | Unix socket path the bridge listens on. Pipelines connect to this socket as clients. |

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
  url: "https://ctrlrelay-dashboard.example.com"
  auth_token_env: "CTRLRELAY_DASHBOARD_TOKEN"
  sync_config_on_heartbeat: false
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Set to `false` to skip dashboard wiring entirely. |
| `url` | string | `""` | Base URL of the dashboard service. Empty disables outbound calls. |
| `auth_token_env` | string | `"CTRLRELAY_DASHBOARD_TOKEN"` | Env-var name holding the dashboard auth token. |
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
      accept_foreign_assignments: false
      exclude_labels: ["manual", "operator", "instruction"]
    code_review: { ... }      # optional
    deploy:      { ... }      # optional
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | **yes** | — | GitHub `owner/repo` slug. Used for `gh` calls and bare-repo / worktree naming. |
| `local_path` | path | conditional | derived | Where the repo is checked out on disk for human use. Optional when `paths.repo_root` is set (then derived as `${repo_root}/${owner_aliases.get(owner, owner)}/${repo}`); required otherwise. An explicit value always wins as override. ctrlrelay itself uses bare mirrors under `paths.bare_repos`. |
| `dev_branch_template` | string | no | `"fix/issue-{n}"` | Branch-name template for dev-pipeline runs. `{n}` is replaced by the issue number. |
| `automation` | object | no | (defaults) | See [automation](#repos-automation). |
| `code_review` | object | no | (defaults) | Reserved for code-review policy. Currently unused by the bundled pipelines. |
| `deploy` | object | no | `null` | Reserved for deploy policy. Currently surfaced in `ctrlrelay config repos` but otherwise inert. |

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
| `accept_foreign_assignments` | `false` | When `true`, the poller also picks up issues assigned to you by someone else. Default (`false`) runs the dev pipeline only on issues you self-assigned. |
| `exclude_labels` | `["manual", "operator", "instruction"]` | Issue labels that tell the poller "this isn't for the agent". See [exclude_labels](#reposautomationexclude_labels) below. |
| `include_labels` | `[]` | Issue labels that opt an issue **into** the dev pipeline regardless of who is (or isn't) assigned. See [include_labels](#reposautomationinclude_labels) below. |

The current secops and dev pipelines read these settings to bias their prompts
to Claude — they're not enforced by hard-coded checks.

### repos[].automation.exclude_labels

Some issues you assign to the operator user aren't code work — they're operator
tasks (validate a build on your laptop) or pure instructions (document a
workflow). The dev pipeline has no way to tell these apart from a feature
request on its own, so it dutifully writes code and opens a PR anyway.

`exclude_labels` gives the operator a short-circuit: any issue carrying one of
the configured labels is **marked seen** in `poller_state.json` so it doesn't
re-appear on the next poll, **not handed to the dev pipeline**, and **logged**
under the `poll.issue.excluded_by_label` event.

```yaml
repos:
  - name: "your-org/your-repo"
    local_path: "~/Projects/your-repo"
    automation:
      exclude_labels: ["manual", "operator", "instruction"]
```

- Default: `["manual", "operator", "instruction"]`. Set to `[]` to disable.
- Matching is **case-insensitive** (`Manual` matches `manual`).
- The check runs in the poller, before any dev-pipeline work is scheduled.
- Apply the label on GitHub; the next poll will pick it up automatically.

If you mislabel and want the agent to take the issue after all, remove the
label on GitHub **and** delete the issue number from
`poller_state.json` (or bump the issue so it becomes visible again via some
other mechanism — the poller treats "seen" as sticky per design, so operator
input is the source of truth).

### repos[].automation.include_labels

Out of the box, an issue enters the dev pipeline only when it's assigned to
the configured GitHub user (and, with the pre-#79 self-assignment filter, only
when *you* were the one who assigned it). That works for a personal to-do
list; it doesn't cover the "team-coordinated" workflow where a teammate without
rights on your account wants to say "this issue is safe for the agent to take
a shot at."

`include_labels` is the opt-in complement to `exclude_labels`. Any issue
carrying one of the configured labels is handed to the dev pipeline,
**regardless of assignment**. The label itself is the trust signal — you opt
in by configuring the label; anyone with triage permission on the repo can
then flag an issue for the bot.

```yaml
repos:
  - name: "your-org/your-repo"
    local_path: "~/Projects/your-repo"
    automation:
      include_labels: ["ctrlrelay:auto"]
```

- Default: `[]`. An empty list preserves the pre-#80 assignment-only trigger
  — no behavior change for operators who haven't opted in.
- Matching is **case-insensitive** (`CtrlRelay:Auto` matches `ctrlrelay:auto`).
- An issue is accepted when **either** (a) it's assigned to the configured
  user (subject to the self-assignment filter from #79 and
  `accept_foreign_assignments`) **or** (b) it carries any label in
  `include_labels`. A label match **skips** the self-assignment check — the
  operator's config choice is the trust boundary.
- Dedup: an issue that is **both** labeled and assigned is picked up exactly
  once per poll cycle — no duplicate entries in `seen_issues` and no double
  pipeline spawn.
- `exclude_labels` always wins over `include_labels` on the same issue: an
  explicit "not for the agent" opt-OUT beats the generic label opt-IN.
- When a repo configures `include_labels`, the poller runs **targeted**
  queries per cycle: the existing `gh issue list --assignee <user>` plus
  one `gh issue list --label <L>` call per configured label. Results merge
  by issue number. This keeps the label path scale-safe on busy repos
  where an unfiltered fetch would silently cap at gh's `--limit` and miss
  labeled issues on later pages. Repos without `include_labels` run only
  the cheap `--assignee` query, so enabling the feature on one repo does
  not add API calls on the others.
- The event log entry for a label-triggered acceptance is
  `poll.issue.included_by_label`, alongside the existing
  `poll.issue.excluded_by_label` for exclusions.
- **Interaction with `task_labels`**: `include_labels` opts an issue
  into the poller's consideration set. Once surfaced, the usual
  routing still applies — if the same issue also carries a
  `task_labels` label, it runs through the **task** pipeline (report-
  only, no PR), not the dev pipeline. If you want label-triggered
  issues to always run dev, make sure `include_labels` and
  `task_labels` are disjoint (e.g. label opt-ins with
  `ctrlrelay:auto` and task runs with `task:<topic>`).
- **Upgrade path**: enabling `include_labels` on a repo that was
  already running the poller does NOT retroactively re-evaluate
  issues already in `poller_state.json`. Any foreign-assigned issue
  that pre-dates the config change won't be picked up via a later
  label addition. Only brand-new issues (after the config change) or
  issues you re-open will go through the label trigger. If you need
  to re-evaluate pre-existing issues on a specific repo, stop the
  poller, remove that repo's entry from `poller_state.json` under
  `seen_issues`, and restart. (A fully automatic migration would
  risk re-running pipelines for issues the bot had already handled.)

Trust model: anyone with triage permission on a repo can apply a label. That
matches the trust model ctrlrelay already uses — the operator configures which
repos and which labels trigger the pipeline; a hostile collaborator with
triage access was already able to push branches and trigger CI, so allowing
them to opt an issue into the dev pipeline is a narrower extension, not a new
vector.

## Example: telegram-enabled config

```yaml
version: "1"
node_id: "studio-mac"
timezone: "America/Santiago"

paths:
  state_db:    "~/.ctrlrelay/state.db"
  worktrees:   "~/.ctrlrelay/worktrees"
  bare_repos:  "~/.ctrlrelay/repos"
  contexts:    "~/.ctrlrelay/contexts"
  skills:      "~/.claude/skills"

claude:
  binary: "/opt/homebrew/bin/claude"
  default_timeout_seconds: 3600
  output_format: "json"

transport:
  type: "telegram"
  telegram:
    bot_token_env: "CTRLRELAY_TELEGRAM_TOKEN"
    chat_id: 987654321
    socket_path: "~/.ctrlrelay/ctrlrelay.sock"

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

Always run `ctrlrelay config validate` after editing the file. It prints the
resolved transport, repo count, and parsed timezone — and surfaces any pydantic
validation errors with line context.
