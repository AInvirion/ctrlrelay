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
web:          { ... }
repos:        [ ... ]
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `version` | string | no | `"1"` | Config schema version. Currently always `"1"`. |
| `node_id` | string | no | `socket.gethostname()` | Free-form identifier for this machine. Surfaces in dashboard heartbeats, session logs, and the claim comment on every issue it takes, so machines sharing one account must each have a different one. `unknown` is reserved (it names claims from older versions). Defaults to the OS hostname when omitted, null, or blank — set explicitly only if the hostname is meaningless (CI runners, ephemeral containers). |
| `timezone` | string | no | `"UTC"` | IANA timezone (e.g. `America/Santiago`). Used for scheduling. |
| `paths` | object | **yes** | — | See [paths](#paths). |
| `claude` | object | no | (defaults) | See [claude](#claude). |
| `transport` | object | **yes** | — | See [transport](#transport). |
| `dashboard` | object | no | (defaults) | See [dashboard](#dashboard). |
| `web` | object | no | (defaults) | The read-only status page. See [web](#web). |
| `claims` | object | no | (defaults) | Claim timings for several machines on one account. See [claims](#claims). |
| `repos` | list | no | `[]` | See [repos](#repos). |
| `schedules` | object | no | (defaults) | See [schedules](#schedules). |
| `personalization` | object | no | unset | Cross-machine sync of Claude state. See [personalization](#personalization) and [Personalization sync]({{ '/personalization/' | relative_url }}). |

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
```

| Key | Type | Required | Description |
|---|---|---|---|
| `state_db` | path | **yes** | SQLite database for sessions, locks, telegram_pending, automation_decisions. |
| `worktrees` | path | **yes** | Where ctrlrelay creates per-session `git worktree` directories. |
| `bare_repos` | path | **yes** | Where ctrlrelay clones bare mirrors of each configured repo. |
| `contexts` | path | **yes** | Per-repo context directory (looked up as `<contexts>/<owner-repo>/CLAUDE.md`). If a `CLAUDE.md` exists, it is symlinked into the worktree at session start. |
| `skills` | path | **yes** | Claude Code skills directory used by `ctrlrelay skills audit` and `ctrlrelay skills list`. |
| `repo_root` | path | no | Convention root for repo clones. When set, `repos[].local_path` may be omitted and is derived as `${repo_root}/${owner.lower()}/${repo}` (since v0.4.0). Without `repo_root`, every repo entry must declare its own `local_path` (legacy behaviour). |
| `owner_aliases` | object | no | **Deprecated since v0.4.0.** Pre-0.4.0 this remapped the on-disk folder name (e.g. `SemClone` -> `SEMCL.ONE`). The path resolver now always uses `owner.lower()`, so aliases are no longer consulted. Parsing is retained so 0.3.x configs still load; a `DeprecationWarning` is emitted when a non-empty mapping is supplied. To silence: drop the `owner_aliases` block, rename your on-disk folder to match `owner.lower()`, or set a per-repo `local_path` override for any that genuinely deviate. |

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
| `output_format` | string | `"json"` | Not used. The relay always runs `claude -p --output-format stream-json --verbose`, because only that output carries the subscription usage it reads (see [usage](#usage)). Kept so existing configs still load. |

ctrlrelay always invokes claude with `--dangerously-skip-permissions` so the
agent does not pause on tool-permission prompts in headless runs. This is
intentional — the orchestrator runs unattended. The CLI's other unattended
modes either need every shell command allow-listed or filter by command text,
which a wrapper script walks past, so they are not used as a guard. The guard
is the next section: the agent holds no GitHub credentials.

### What the agent can reach (#219)

The dev and task agents run **without GitHub access**. ctrlrelay starts them
with:

- every `GH_*` and `GITHUB_*` variable removed (`GH_TOKEN`, `GITHUB_TOKEN`,
  `GH_ENTERPRISE_TOKEN`, ...), and every inherited `CTRLRELAY_*` variable
  (the chat bot tokens) except the two the checkpoint needs;
- `GH_CONFIG_DIR` pointed at a fresh empty directory, so `gh` finds no login;
- `DBUS_SESSION_BUS_ADDRESS` pointed at nothing, so `gh` and other libsecret
  clients cannot fall back to the desktop keyring;
- `credential.helper` reset to empty (through `GIT_CONFIG_COUNT`),
  `GIT_SSH_COMMAND=false` and `SSH_AUTH_SOCK` removed, so git cannot
  authenticate over HTTPS or SSH, including through an `insteadOf` rewrite;
- `GIT_TERMINAL_PROMPT=0`, so nothing waits on a prompt.

Everything else is inherited unchanged, so the model CLI and the project's
toolchain behave as before. ctrlrelay then does the GitHub side itself, with
its own credentials, after the agent exits:

- **dev:** the agent commits on the run's branch and writes a PR title and
  description in its DONE checkpoint. ctrlrelay pushes the branch and opens
  the pull request. After a CI or conflict fix round it pushes again, and it
  fetches the failed-job logs the agent used to read with `gh run view`.
- **task:** the agent writes its report in its DONE checkpoint and ctrlrelay
  posts it as the issue comment.
- **secops** still runs **with** the operator's GitHub access. Its agent
  reads Dependabot alerts and pull requests and merges what the policy
  allows during the run. Moving it would mean ctrlrelay doing those reads
  up front and the merges afterwards from a decision the agent returns.

Pushes are refused, and logged as `publish.push.refused`, for `main`,
`master`, the repository's default branch (read from the GitHub API, not
from the local clone) and any branch that is not the run's own. The push
goes to `https://github.com/<owner>/<repo>.git` (never `origin`) from a
private git dir with hooks off and only ctrlrelay's own git config, pushes the
commit it checked rather than the ref name, and uses `--force-with-lease`
against the head this session last pushed (recorded in the state DB). An
open PR found for the branch is reused only if its head repo is this repo and
its base is the default branch. Text the agent wrote is posted with `@`-mentions and
issue-closing keywords disabled and its length capped (20,000 characters);
the PR's own `Closes #N` is added by ctrlrelay.

**What this guarantees.** No GitHub credential is handed to the agent through
its environment. ctrlrelay chooses the push URL, the branch and the PR base.
`main`, `master`, the default branch and any branch other than the run's own
are refused, and a branch that already exists on GitHub is only pushed over if
its head is one ctrlrelay recorded pushing. Agent-written text is sanitized.
An agent that only uses its normal tooling (git, gh, ssh as configured) cannot
push. The push itself runs from a fresh relay-owned git dir under
`~/.ctrlrelay/push/`, reads the clone's branch heads from its ref files, and
never reads the clone's config, which every worktree shares.

**What it does not guarantee.** The agent still runs as your user. A
deliberately hostile agent can read `~/.config/gh/hosts.yml`, `~/.ssh` and
anything else in your home directory and use them by hand; it can write
anywhere your user can, including your dotfiles and ctrlrelay's install; and
it can write to `~/.ctrlrelay/push/`, the bare clone's object store and any
`objects/info/alternates` chain it adds there, racing a push. On macOS the
login keychain is reachable whatever the environment says. Closing that needs
a separate user or a read-only home (#236). The next setting hides the
credential paths; it does not stop the writes.

| Key | Type | Default | Description |
|---|---|---|---|
| `sandbox` | `"none"` or `"bwrap"` | `"none"` | `"bwrap"` runs the dev and task agents under bubblewrap with `sandbox_hide_paths` replaced by empty mounts. Linux only; needs `bwrap` installed and unprivileged user namespaces allowed (on Ubuntu 24.04+ an AppArmor profile for `bwrap`). ctrlrelay refuses to start if it is set and `bwrap` is missing. |
| `sandbox_hide_paths` | list of strings | `~/.ssh`, `~/.config/gh`, `~/.git-credentials`, `~/.config/git/credentials`, `~/.netrc`, `~/.local/share/keyrings`, `$XDG_RUNTIME_DIR/bus` | Paths hidden under `sandbox: bwrap`. A directory becomes an empty tmpfs, a file or socket becomes `/dev/null`, a missing path is skipped. |

## transport

The transport carries `BLOCKED_NEEDS_INPUT` questions out of ctrlrelay to a
human and routes the answer back.

### Choosing one

**Exactly one transport is active: the one named by `type`.** Every other
block in the file is inert — it may stay there, but nothing reads it.

| You want | Set `type` to | You need |
|---|---|---|
| A 1:1 chat with a bot | `"telegram"` | A bot token and your chat ID. |
| A channel on your own server | `"mattermost"` | A server URL, a bot token, and the channel's 26-character ID. |
| No chat at all | `"file_mock"` | Nothing. Questions go to a file; **nobody is asked anything.** |

**Running Telegram and Mattermost at the same time is not supported, and is
not planned.** Setting `type` to one does not fall back to the other, and a
question is delivered to one place or not at all. It was designed in
[#176](https://github.com/AInvirion/ctrlrelay/issues/176) and closed won't-do;
the design is still in that issue if anyone wants it.

Switching is a two-line change — set `type`, add that transport's block — plus
a restart of **both** daemons, which share `state.db`.

Every per-transport setting below is read from the **active** transport. A
Mattermost deployment takes `question_ttl_seconds` from the `mattermost` block,
not from a leftover `telegram` one.

```yaml
transport:
  type: "mattermost"   # or "telegram", or "file_mock"
  mattermost:
    url: "https://chat.example.com"
    bot_token_env: "CTRLRELAY_MATTERMOST_TOKEN"
    channel_id: "abcdefghijklmnopqrstuvwxyz"
    socket_path: "~/.ctrlrelay/ctrlrelay.sock"
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | enum | no | `"file_mock"` | One of `"telegram"`, `"mattermost"`, `"file_mock"`. |
| `telegram` | object | required when `type=telegram` | — | Telegram bridge settings — see below. |
| `mattermost` | object | required when `type=mattermost` | — | Mattermost bridge settings — see below. |
| `file_mock` | object | required when `type=file_mock` | — | Local-file fake transport, used for tests/dev. |

The block for the selected type is **required**: a config naming a type whose
block is absent is refused at load, loudly, rather than starting with no way to
reach anybody.

### transport.telegram

| Key | Type | Default | Description |
|---|---|---|---|
| `bot_token_env` | string | `"CTRLRELAY_TELEGRAM_TOKEN"` | Name of the environment variable holding the bot token. ctrlrelay never reads the token directly — only the variable name. |
| `chat_id` | int | `0` | Telegram chat ID the bridge sends messages to and accepts replies from. |
| `socket_path` | path | `"~/.ctrlrelay/ctrlrelay.sock"` | Unix socket path the bridge listens on. Pipelines connect to this socket as clients. |
| `ask_timeout_seconds` | int | `900` | How long a pipeline blocks waiting on your reply in-session. Minimum `60`. A reply arriving later still works: the question is persisted to `pending_resumes` and the every-minute sweeper drives the resume. |
| `question_ttl_seconds` | int | `172800` (48h) | How long an **unanswered** question stays routable before it is retired. Minimum `3600`. Without a ceiling those rows never die — a question whose PR was merged by hand weeks ago stays a live target for an orphan reply. The hourly `question_expiry_sweeper` also retires a question early once every issue/PR it cites is closed or merged. |
| `question_retention_days` | int | `90` | How long a **finished** question (expired, or answered and resumed) is kept before the hourly sweeper deletes it. Minimum `1`. Open questions are never deleted. The kept history is what lets a re-asked question be marked as a repeat. |

See [Telegram bridge]({{ '/bridge/' | relative_url }}) for the full setup walkthrough.

### transport.mattermost

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | string | — | Base URL of your Mattermost server, e.g. `https://chat.example.com`. Required. |
| `bot_token_env` | string | `"CTRLRELAY_MATTERMOST_TOKEN"` | Name of the environment variable holding the bot token. ctrlrelay never reads the token directly — only the variable name. |
| `channel_id` | string | `""` | The channel's **26-character ID**, not its name: a name is only unique within a team and can be renamed under you. Find it in the channel menu under *View Info*, or via `GET /api/v4/teams/name/{team}/channels/name/{channel}`. |
| `socket_path` | path | `"~/.ctrlrelay/ctrlrelay.sock"` | Unix socket path the bridge listens on. Pipelines connect to this socket as clients. |
| `ask_timeout_seconds` | int | `900` | Same meaning as the Telegram key above: it bounds only the in-session wait, and a later reply still resolves through `pending_resumes`. |
| `question_ttl_seconds` | int | `172800` (48h) | Same meaning as the Telegram key above. |
| `question_retention_days` | int | `90` | Same meaning as the Telegram key above. |

**The bot must be a member of that channel.** A bot without `post:all` cannot
post where it is not a member — and, the part that is easy to miss, it does not
receive that channel's reply events either, so answers would silently never
arrive.

Outbound is `POST /api/v4/posts`; inbound is a WebSocket on
`/api/v4/websocket` filtered to the one channel. A threaded reply's `root_id`
equals the question's post ID, which is how an answer is matched to its
question — the same role `reply_to_message_id` plays on Telegram.

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

## web

The read-only status page served by `ctrlrelay web start` (see
[Operations: status page]({{ '/operations/' | relative_url }}#status-page)). Every key is optional;
without a `web:` block the defaults below apply.

```yaml
web:
  host: "127.0.0.1"
  port: 8787
  history_days: 7
  refresh_seconds: 5
  github_check_seconds: 60
  stale_after_minutes: 15
  allowed_hosts: []          # e.g. ["mybox", "mybox.lan"]
```

| Key | Type | Default | Description |
|---|---|---|---|
| `host` | string | `"127.0.0.1"` | Address to listen on, exactly. Loopback by default so a fresh install is never reachable from other machines. Set it to this machine's LAN address to open the page from the home network. `0.0.0.0` listens on every interface and is warned about at startup. |
| `port` | int (1-65535) | `8787` | Port to listen on. |
| `history_days` | int (>= 1) | `7` | Days of feed the page shows, and what `/api/events` returns without `?since=`. |
| `refresh_seconds` | int (>= 1) | `5` | How often the page asks `/api/status` again. |
| `github_check_seconds` | int (>= 15) | `60` | How often the waiting line asks GitHub (one search, plus one query per `include_labels` label). |
| `stale_after_minutes` | int (>= 1) | `15` | A running card is marked "looks stuck" after this long with no new agent step, unless ctrlrelay itself has a CI wait or review open for it. |
| `allowed_hosts` | list of strings | `[]` | Extra names the page may be opened by. A request is served only when its `Host` header is `localhost`, `127.0.0.1`, `[::1]`, `host` (unless it is `0.0.0.0`/`::`), this machine's hostname, the hostname plus `.local`, or one of these, alone or followed by `:port`. Anything else, or no `Host`, gets `421`. Matching ignores case and a trailing dot. This stops a web page on another site from pointing its own name at this server (DNS rebinding) and reading it. |

The page has no login and shows repository names, issue titles and the
questions agents asked. **Never expose it to the internet.**

## claims

How machines that share one GitHub account keep off each other's issues.
See [Operations: several machines on one account]({{ '/operations/' | relative_url }}#several-machines-on-one-account).

```yaml
claims:
  race_wait_seconds: 5
  refresh_seconds: 300
  stale_after_seconds: 1800
```

| Key | Type | Default | Description |
|---|---|---|---|
| `race_wait_seconds` | number (0-60) | `5` | Wait between posting a claim and reading the comments back to see who won. |
| `refresh_seconds` | number (>= 30) | `300` | How often a working machine edits its claim to show it is alive. |
| `stale_after_seconds` | number (>= 300) | `1800` | A working claim not refreshed for this long is treated as a stopped machine's, and another machine may take the issue. Must be at least three times `refresh_seconds`. |

## usage

Pausing before the agent subscription's limit. Each run reports the
subscription's 5-hour and 7-day usage; the status page shows it, and a
machine stops taking new issues near the limit.

```yaml
usage:
  pause_at: 0.9
  probe_minutes: 30
```

| Key | Type | Default | Description |
|---|---|---|---|
| `pause_at` | number (above 0, up to 1) | `0.9` | Take no new issues while the 5-hour or 7-day window is at or above this share, or while the status is anything but `allowed`. Runs already going finish. New issues are taken again after the window's reset time. |
| `probe_minutes` | int (>= 0) | `30` | When the last reading is older than this, the daemon makes one tiny agent call (no tools, no MCP servers, no user or project settings and so no hooks, one turn, in an empty directory) to read the usage again, so an idle machine's numbers do not go stale. `0` turns it off. Each probe is one short call on the subscription. |

The usage comes from a field of the agent CLI's output that is not
documented. When it is missing or cannot be read, the page says
**unknown**, never 0%, and the machine does not pause on it. A run whose output carries no readable usage records unknown too, so an older number stops pausing the machine.

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
| `local_path` | path | conditional | derived | Where the repo is checked out on disk for human use. Optional when `paths.repo_root` is set (then derived as `${repo_root}/${owner.lower()}/${repo}`, since v0.4.0); required otherwise. An explicit value always wins as override. ctrlrelay itself uses bare mirrors under `paths.bare_repos`. |
| `dev_branch_template` | string | no | `"fix/issue-{n}"` | Branch-name template for dev-pipeline runs. `{n}` is replaced by the issue number. |
| `automation` | object | no | (defaults) | See [automation](#repos-automation). |
| `code_review` | object | no | (defaults) | Review run over an agent branch before its PR is handed over. See [code_review](#code_review). |
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
| `require_labels` | `[]` | Issue labels that gate the plain-assignment trigger: when set, assignment alone is no longer enough — the issue must also carry one of these labels. See [require_labels](#reposautomationrequire_labels) below. |
| `ci_wait_timeout_seconds` | `600` | Unused since #219 (the agent no longer waits on CI; ctrlrelay does). Still accepted so existing configs load. See [ci_wait_timeout_seconds](#reposautomationci_wait_timeout_seconds) below. |

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

### repos[].automation.require_labels

Assignment-only ends up too loose on repos with a long history: issues
self-assigned as personal reminders or notes, unrelated to automation, get
replayed into the dev pipeline the first time the poller sees the repo (or
after any config change that clears `poller_state.json` for it). Some of
those issues aren't code tasks at all — "create an account with a
third-party service", "look into pricing" — and shouldn't go anywhere near
an unsupervised `claude -p ... --dangerously-skip-permissions` run.

`require_labels` closes that gap: when configured, bare assignment is no
longer sufficient by itself. An issue must be **both** assigned to the
operator **and** carry at least one of the configured labels to be picked
up via the assignment path.

```yaml
repos:
  - name: "your-org/your-repo"
    local_path: "~/Projects/your-repo"
    automation:
      require_labels: ["ctrlrelay:auto"]
```

- Default: `[]`. An empty list preserves today's behavior — assignment
  alone is sufficient, no change for operators who haven't opted in.
- Matching is **case-insensitive**, same as `exclude_labels` / `include_labels`.
- This only tightens the **assignment** path. `include_labels` is
  unaffected — a label match there still admits the issue regardless of
  `require_labels`, since that path already treats the label as its own
  trust signal.
- An issue that's assigned but missing the required label is left
  **unmarked** (not added to `seen_issues`), so applying the label later
  still surfaces it on a subsequent poll — nothing is lost, it's just not
  picked up yet.
- `exclude_labels` is still checked first, same precedence as always.

### repos[].automation.ci_wait_timeout_seconds

**Not used by the dev pipeline since #219.** The agent no longer has GitHub
access, so it no longer waits on CI: ctrlrelay pushes the branch after the
agent exits and its own PR verifier waits on CI. The key is still accepted so
existing configs load. What follows describes the earlier behaviour.

The dev pipeline prompt told Claude to run `ctrlrelay ci wait --pr <PR>
--repo <repo> --timeout <N>` before signaling DONE — a single blocking call
that polls GitHub every 15s until CI finishes, fails, or the timeout hits.
No tokens get generated while it's blocked; on a repo with a slow CI suite,
that one call can eat most of a session's wall-clock time.

```yaml
repos:
  - name: "your-org/your-repo"
    local_path: "~/Projects/your-repo"
    automation:
      ci_wait_timeout_seconds: 300
```

- Default: `600` (10 minutes).
- Lower it to match how long the repo's CI actually takes — a repo with a
  2-minute CI suite gains nothing from a 10-minute cap.
- A timeout here isn't a failure: `ctrlrelay ci wait` exits 2 (not 1) when
  the deadline hits with checks still pending, and the prompt tells Claude
  to treat that as acceptable and hand off rather than loop.
- This does not change the overall session timeout (`agent.default_timeout_seconds`,
  default 1800s) — it only bounds the one `ci wait` call.

## schedules

In-process cron jobs run by the poller daemon. All expressions are
standard 5-field (minute hour dom month dow) and evaluate in the
top-level `timezone`. Malformed expressions fail at config load — they
won't silently disable the job at runtime.

```yaml
schedules:
  secops_cron: "0 6 * * *"          # daily 06:00 sweep across repos
  personalization_cron: "*/15 * * * *"  # optional auto-pull
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `secops_cron` | string | no | `"0 6 * * *"` | When to run the secops sweep (`ctrlrelay secops run` equivalent) across all configured repos. Set to e.g. `"0 6 * * 1"` for weekly Mondays. |
| `personalization_cron` | string | no | unset | When to auto-pull the personalization repo on this machine. Only effective when [`personalization`](#personalization) is also set. Skip-on-dirty: never rebases under uncommitted operator edits. Adoption is never performed by auto-pull — that stays an init-time concern. |

## personalization

Optional. Configures cross-machine sync of operator state (Claude
config, per-project memory, spec/superpower outputs) through a
separate (typically private) GitHub repo. See [Personalization
sync]({{ '/personalization/' | relative_url }}) for the full
walkthrough; this section documents the schema.

```yaml
personalization:
  repo: "your-handle/dotclaude"
  # checkout_path: "~/.ctrlrelay/personalization"   # default
  # main_branch: "main"                              # default
  # node_id: "studio-mac"                            # default: top-level node_id
  paths:
    - source: "global/CLAUDE.md"
      target: "~/.claude/CLAUDE.md"
    - source: "global/skills/"
      target: "~/.claude/skills/"
    - source: "claude-memory/${PROJECT}/"
      target: "~/.claude/projects/${PROJECT_ENCODED}/memory/"
      project_scoped: true
    - source: "specs/${PROJECT}/"
      target: "${PROJECT_PARENT}/specs/${PROJECT}/"
      project_scoped: true
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `repo` | string | **yes** | — | `<owner>/<repo>` slug for the personalization repo on github.com. |
| `checkout_path` | string | no | `~/.ctrlrelay/personalization` | Where ctrlrelay clones the repo on this machine. |
| `main_branch` | string | no | `"main"` | Long-lived integration branch. Per-machine branches rebase onto this. |
| `node_id` | string | no | top-level `node_id` | Identifies this machine's working branch (`personalization/<node_id>`). Override only if the top-level value isn't safe as a git branch component. |
| `paths` | list | **yes** | — | One entry per file/dir to sync. See [personalization.paths](#personalizationpaths). |

### personalization.paths

Each entry declares one source-to-target wiring. Trailing slashes
distinguish files from directories — `source: "global/CLAUDE.md"` is
a file, `source: "global/skills/"` is a directory; the target must
agree.

| Key | Type | Required | Description |
|---|---|---|---|
| `source` | string | **yes** | Path inside the personalization repo. Trailing slash means directory. |
| `target` | string | **yes** | On-disk path the symlink will be created at. Supports `${HOME}` and (when `project_scoped`) `${PROJECT}`, `${PROJECT_ENCODED}`, `${PROJECT_LOCAL}`, `${PROJECT_PARENT}`. |
| `project_scoped` | bool | no (default `false`) | When `true`, the entry expands once per repo in `repos:`, with the `${PROJECT_*}` placeholders resolved against that repo's `local_path`. |

The `${PROJECT}` slug uses **`<owner>--<repo>`** with a double
hyphen so `a-b/c` and `a/b-c` produce different slugs. `${PROJECT_ENCODED}`
matches Claude Code's own path encoding rule (`/Users/foo/Projects/bar`
→ `-Users-foo-Projects-bar`).

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

## code_review

A review the orchestrator runs over an agent's branch after CI is green,
posting the findings as a PR comment.

| Key | Type | Default | Description |
|---|---|---|---|
| `method` | string | `"cli"` | `"cli"` runs `cli_command`; `"off"` disables it. `"none"`/`"disabled"`/`"false"`/`"no"` mean off; legacy `"mcp_then_cli"` maps to `"cli"`. An unrecognised value falls back to `"cli"` rather than failing to load. |
| `cli_command` | string | `codex review -c sandbox_mode="read-only"` | Invoked with `--base <default branch>` in the session worktree. |
| `timeout_seconds` | int | `900` | Per-run cap, minimum 30. |
| `comment_on_pr` | bool | `true` | Post findings to the PR. When false they go to the log instead. |

### What this does and does not tell you

The comment is **unverified automated output**, and says so. It is not a
confirmation that anything was reviewed, and no label is applied.

That restraint is deliberate. Two attempts at a `code_review done` marker
were defeated by the party being reviewed, which authors every file the
reviewer reads:

- Inferring "a review happened" from the reviewer's prose fell to a
  committed `AGENTS.md` saying *respond with exactly: No findings* — exit
  0, zero commands executed, clean verdict, marker applied.
- Requiring evidence of a diff-reading command fell four ways:
  `.agents/skills/*/SKILL.md` steers the reviewer and loads even when the
  project is untrusted; `gh pr view --json files` caps at 100 paths so
  extra files push the guarded names off the list; the evidence match hit
  diff bodies, failed commands, and one run that reviewed a **different
  repository** after its sandbox could not bind the target path; and
  nothing pinned the review to the pushed head, so uncommitted changes
  were reviewed instead of the code.

Each fix is individually easy, which is the trap. Both attempts share a
root: inferring a property of a review from an unstructured transcript
produced by a tool that chooses for itself what to run and where.

So the findings are published and the claim is not made. Read the comment
as one more opinion.

### A note on `read-only`

The default `sandbox_mode="read-only"` means the reviewer cannot write to
disk or reach the network. It does **not** stop branch code from running
— a real review executed the repo's test suite. It runs as your user with
read access to your whole disk. The setting bounds the damage; it does
not eliminate it.
