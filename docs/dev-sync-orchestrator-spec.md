---
title: Orchestrator Build Spec
layout: default
nav_order: 3
---

# dev-sync Orchestrator — Build Spec

A local-first, cron-driven orchestrator that wraps `claude -p` (headless Claude Code) to run secops and dev pipelines across multiple GitHub repos, with Telegram for human-in-the-loop and a DigitalOcean-hosted dashboard for heartbeats and status.

Audience: a developer (peer) building this out, plus Claude Code itself consuming this as a build instruction. Do not treat this as immutable — flag anything that doesn't survive contact with reality.

---

## 1. Goals and non-goals

### Goals

- Run two pipelines end-to-end on the user's Ubuntu desktop: **secops** (daily 6am, GitHub security/PR/issue triage across repos) and **dev** (triggered when the user self-assigns a GitHub issue, runs the superpowers flow to implementation and PR).
- Use Claude Code headlessly (`claude -p`) as the execution unit. Never proxy or wrap the Claude Code session itself — shell out to it the same way one shells out to `gh` or `doctl`.
- Human-in-the-loop via Telegram: when a session needs input, it checkpoints cleanly, the orchestrator asks the user, and resumes on reply.
- Heartbeat + status dashboard hosted on a small DigitalOcean app, receiving signals from the local orchestrator.
- Never commit `CLAUDE.md` or other AI-config files to the user's OSS project repos. Per-repo context lives outside the repo and is symlinked in at session start.
- Serialize work per-repo, parallel across repos.

### Non-goals

- No third-party orchestration wrappers (vibe-kanban, paperclip, etc.).
- No Claude Code Routines or Managed Agents for the core flow. (These were considered; user wants local.)
- No auto-merge of dev PRs (user reviews and merges). Secops may auto-merge green+minor dependabot PRs — configurable per repo.
- No multi-user support. Single operator.

---

## 2. System shape

```
┌───────────────────────────────────────────────────────────────┐
│  Ubuntu desktop (always-on, UPS-backed)                       │
│                                                               │
│  ┌──────────────────────┐     ┌──────────────────────┐        │
│  │ orchestrator.service │◄───►│ telegram-bridge.svc  │        │
│  │ (Python, systemd)    │     │ (Python, systemd)    │        │
│  │                      │     └──────────┬───────────┘        │
│  │  - internal sched    │                │ Bot API            │
│  │  - session dispatch  │                ▼                    │
│  │  - state (sqlite)    │        ┌──────────────┐             │
│  │  - heartbeat push    │        │   Telegram   │             │
│  └──────┬───────────────┘        └──────────────┘             │
│         │ spawns subprocess                                    │
│         ▼                                                      │
│  ┌──────────────────────┐                                     │
│  │ claude -p ...        │  ← uses local skills, MCP servers   │
│  │ (short-lived)        │    shells out to gh, doctl          │
│  └──────────────────────┘                                     │
└────────────┬──────────────────────────────────────────────────┘
             │ HTTPS POST /heartbeat, /event
             ▼
┌───────────────────────────────────────────────────────────────┐
│  DigitalOcean App Platform (tiny FastAPI app)                 │
│                                                               │
│  - /heartbeat  (receives pings, alerts if stale)              │
│  - /event      (receives structured events)                   │
│  - /           (dashboard UI: status, recent runs, alerts)    │
│  - sqlite file on attached volume                             │
└───────────────────────────────────────────────────────────────┘
```

Two separate deliverables:

1. **`dev-sync` repo** on the user's machine — the orchestrator and telegram bridge (Python). Lives alongside existing configs and skills in the already-existing `dev-sync` repo.
2. **`dev-sync-dashboard`** — a small FastAPI app deployed to DigitalOcean App Platform. The peer builds this.

---

## 3. Repository layout

Extend the existing `dev-sync` repo:

```
dev-sync/
├── skills/                        # existing; user's Claude Code skills
│   ├── gh-dashboard/
│   ├── gh-secops/
│   ├── deploy-verify/
│   └── superpowers/               # user already has this
├── contexts/                      # per-repo CLAUDE.md, NEVER committed to OSS repos
│   ├── repo-a/CLAUDE.md
│   └── repo-b/CLAUDE.md
├── orchestrator/                  # new: the daemon
│   ├── pyproject.toml
│   ├── src/dev_sync/
│   │   ├── __init__.py
│   │   ├── main.py                # entrypoint, event loop
│   │   ├── config.py              # YAML config loader
│   │   ├── scheduler.py           # internal cron (APScheduler)
│   │   ├── state.py               # sqlite access layer
│   │   ├── dispatcher.py          # spawns claude -p subprocesses
│   │   ├── session.py             # session lifecycle, checkpoint protocol
│   │   ├── github.py              # gh CLI wrapper
│   │   ├── worktree.py            # git worktree mgmt
│   │   ├── telegram_client.py     # talks to the bridge via unix socket
│   │   ├── dashboard_client.py    # HTTPS to DO dashboard
│   │   ├── hooks.py               # git hook installer (attribution scrub)
│   │   └── pipelines/
│   │       ├── secops.py
│   │       └── dev.py
│   └── tests/
├── telegram_bridge/               # new: separate process
│   ├── pyproject.toml
│   └── src/bridge/
│       ├── __init__.py
│       ├── main.py                # aiogram bot + unix socket server
│       └── protocol.py            # message schema
├── systemd/                       # new: unit files
│   ├── dev-sync-orchestrator.service
│   └── dev-sync-telegram.service
├── hooks/                         # new: git hooks
│   ├── commit-msg                 # strips AI attribution
│   └── prepare-commit-msg
├── config/
│   ├── orchestrator.yaml.example  # repos, schedules, secrets references
│   └── .env.example
└── README.md

dev-sync-dashboard/                # SEPARATE REPO, peer builds this
├── pyproject.toml
├── src/dashboard/
│   ├── main.py                    # FastAPI app
│   ├── models.py                  # pydantic
│   ├── db.py                      # sqlite on persistent volume
│   └── templates/
│       └── index.html
├── .do/app.yaml                   # DigitalOcean App Platform spec
└── README.md
```

Python 3.12 + `uv` for dependency management in both.

---

## 4. Core design decisions (and why)

### 4.1 Session-end checkpointing, not live pause

Headless `claude -p` can't pause mid-execution to wait for user input. Instead:

- Every skill invocation ends in one of three states, written to a known file (`.dev-sync/state.json` in the worktree, or `stdout` as structured JSON): `DONE`, `BLOCKED_NEEDS_INPUT`, `FAILED`.
- When `BLOCKED_NEEDS_INPUT`, the skill writes: the question, relevant context, and the session ID. Then exits cleanly.
- Orchestrator sees the state, forwards the question to Telegram, waits for user reply.
- On reply, orchestrator runs `claude --resume <session-id>` with the user's answer appended as the next user turn, plus instruction to continue from checkpoint.

This means the **superpowers skill (and any skill that might need input) MUST follow this protocol**. Writing the protocol is part of the build: a shared `skill-lib` helper in `dev-sync/skills/_lib/` that skills import to emit state.

### 4.2 One worktree per active dev session

- Working directory for dev sessions: `~/.dev-sync/worktrees/<repo>-<issue-N>/`
- Created via `git worktree add` from a bare clone the orchestrator maintains at `~/.dev-sync/repos/<repo>.git`.
- Allows parallel dev sessions across different repos without collision.
- Per-repo lock in sqlite prevents two sessions for the same repo simultaneously (secops and dev queue against each other).

### 4.3 Per-repo CLAUDE.md lives outside the repo

- `dev-sync/contexts/<repo>/CLAUDE.md` is the source of truth.
- Session start: orchestrator symlinks `contexts/<repo>/CLAUDE.md` → worktree root.
- Session end: orchestrator removes the symlink before any git operation that might stage it.
- A `.gitignore` line for `CLAUDE.md` is added to the worktree's `.git/info/exclude` (not `.gitignore` — that would commit) as defense in depth.
- Context updates (Claude learned something) happen in a dedicated `update-context` skill that writes to `dev-sync/contexts/<repo>/CLAUDE.md` directly via absolute path.

### 4.4 Attribution scrubbing

Two layers:

1. **Git hooks** (`commit-msg` and `prepare-commit-msg`) installed globally via `git config --global core.hooksPath ~/dev-sync/hooks`. Strips:
   - `Co-Authored-By: Claude <...>`
   - `🤖 Generated with [Claude Code]...`
   - `Co-Authored-By: Claude Code`
   - Any line matching `/claude code|anthropic/i` in a commit trailer.

2. **PR body post-processor** — after `gh pr create`, run a scrubber on the PR body via `gh pr edit --body`. Same regex set.

The user noted their local Claude Code doesn't add attribution. This is still worth having because: (a) it may slip into PR bodies, CHANGELOGs, or docs that Claude writes, (b) it's defense-in-depth if behavior changes, (c) costs nothing.

### 4.5 Post-merge bug-fix loop protection

When the user reports a bug on Telegram after a deployed PR:

- Orchestrator fetches recent PRs for the repo via `gh`, identifies the recently-merged one.
- Checks if the original branch still exists locally and on remote.
- **Always creates a fresh branch from `main`** for the fix, named per the user's convention (e.g., `fix/issue-N-postmerge`). Does NOT attempt to reuse the old branch.
- Claude's prompt for the fix session is given the new branch name explicitly and told the previous branch has been deleted.
- Structurally impossible for Claude to waste tokens pushing to a deleted branch.

### 4.6 State: sqlite, single file

`~/.dev-sync/state.db`. Tables:

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,             -- claude session id
  pipeline TEXT NOT NULL,          -- 'secops' | 'dev'
  repo TEXT NOT NULL,
  issue_number INTEGER,
  worktree_path TEXT,
  status TEXT NOT NULL,            -- 'running' | 'blocked' | 'done' | 'failed'
  blocked_question TEXT,           -- when status = 'blocked'
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  claude_exit_code INTEGER,
  summary TEXT                     -- short human-readable summary
);

CREATE TABLE repo_locks (
  repo TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  acquired_at INTEGER NOT NULL
);

CREATE TABLE github_cursor (
  repo TEXT PRIMARY KEY,
  last_checked_at INTEGER NOT NULL,
  last_seen_issue_update TEXT      -- ISO8601 from gh
);

CREATE TABLE telegram_pending (
  request_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  question TEXT NOT NULL,
  asked_at INTEGER NOT NULL,
  answered_at INTEGER,
  answer TEXT
);
```

No migrations framework needed at this scale. `schema.sql` applied on startup with `CREATE TABLE IF NOT EXISTS`.

### 4.7 Scheduling

Use **APScheduler** (Python) inside the orchestrator process. Not OS cron. Reasons:

- State is in-process (can query "is this job running?" before re-firing).
- Jobs can reference shared config and sqlite directly.
- Restart-safe (jobs don't double-fire on systemd restart).

Jobs:

- `secops_morning` — cron: `0 6 * * *` (6am daily, user's local TZ).
- `github_poll` — interval: 5 minutes. Polls all configured repos for newly-assigned issues. Triggers dev pipeline per new assignment.
- `heartbeat` — interval: 5 minutes AND after each `github_poll` run. Pushes status to dashboard.
- `telegram_answer_checker` — interval: 15 seconds. Checks for answered Telegram questions and resumes blocked sessions.
- `health_selfcheck` — interval: 1 minute. Verifies subprocess health, checks stale locks.

### 4.8 Telegram bridge as separate process

Why a separate process and not just import `aiogram` into the orchestrator:

- Decouples bot polling from orchestrator lifecycle. If the bot dies, orchestrator keeps running.
- Clean failure surface: orchestrator pushes a message; if the bridge is down, the push fails and goes to a retry queue.
- Easier to restart one without the other.

Communication: Unix domain socket at `/run/user/$UID/dev-sync.sock`. Simple line-delimited JSON protocol:

```json
{"op": "send", "chat_id": 123, "text": "...", "request_id": "r-abc"}
{"op": "ask", "chat_id": 123, "question": "...", "request_id": "r-abc"}
{"op": "answer", "request_id": "r-abc", "answer": "..."}
```

### 4.9 Dashboard on DigitalOcean

Minimal FastAPI app, ~300 lines. Deploy to DO App Platform (~$5/mo). Endpoints:

- `POST /heartbeat` — body: `{node_id, timestamp, orchestrator_status, active_sessions, last_github_poll}`. Stored in sqlite.
- `POST /event` — body: `{node_id, timestamp, level, pipeline, repo, message, session_id}`. Stored in sqlite.
- `GET /` — HTML dashboard: current status, last N events, alert if heartbeat stale >10 min, active sessions table.
- `GET /api/status` — JSON for programmatic consumers.

Auth: a single shared bearer token in an env var on both sides. Rotate manually. The dashboard is not high-value enough to warrant full OAuth.

Stale heartbeat detection: server-side job every minute checks `NOW - last_heartbeat`. If >10min, the dashboard renders an "ORCHESTRATOR DOWN" banner. Optionally: push a Telegram alert via bot API from the dashboard itself (you'd pass the bot token to the dashboard env for this — decide yes/no during build).

---

## 5. The checkpoint protocol skills must follow

Every skill that might block on user input follows this contract:

**When the skill is ready to hand off to Claude to reason/act**: just runs normally.

**When the skill needs the user**: before exiting, write to `$DEV_SYNC_STATE_FILE` (env var set by orchestrator):

```json
{
  "status": "BLOCKED_NEEDS_INPUT",
  "session_id": "sess_abc123",
  "question": "Should I pin to 2.4.1 or bump to 2.5.0? The changelog mentions a breaking change in <detail>.",
  "context": {
    "repo": "my-repo",
    "pr": 42,
    "commits_ahead": 3
  }
}
```

Then exit 0. Orchestrator interprets `BLOCKED_NEEDS_INPUT` and routes to Telegram.

**When done**:

```json
{
  "status": "DONE",
  "session_id": "sess_abc123",
  "summary": "Merged 3 dependabot PRs, skipped 1 (breaking change in dep X), deployed verified green."
}
```

**When failed**:

```json
{
  "status": "FAILED",
  "session_id": "sess_abc123",
  "error": "gh CLI returned 404 for repo X; check token scope.",
  "recoverable": false
}
```

Provide a tiny Python helper in `dev-sync/skills/_lib/checkpoint.py` that skills can import if they're Python, or a bash function `dev_sync_checkpoint` for shell skills.

---

## 6. Pipelines

### 6.1 Secops pipeline

Trigger: cron at 6am daily.

Orchestrator:

1. Acquire global "secops running" flag in sqlite (prevents dev pipeline from firing during secops).
2. For each configured repo (in order):
   1. Acquire repo lock.
   2. Create/update worktree from `main`.
   3. Symlink context CLAUDE.md.
   4. Spawn `claude -p` with prompt: "Execute /gh-dashboard for repo X. Then for each item it surfaces, execute /gh-secops. Follow the skill's auto-merge rules. Report via state file."
   5. Parse state file result.
   6. If auto-merged PRs exist and repo deploys to DO, spawn second session with `/deploy-verify` prompt.
   7. Release repo lock.
3. Push aggregated summary to dashboard as an event.
4. Release global flag.

No Telegram message on success (too noisy). Telegram only if something went wrong or needs human review (ping with link to dashboard).

### 6.2 Dev pipeline

Trigger: 5-minute GitHub poll detects a new issue assigned to the user.

Orchestrator:

1. Acquire repo lock (queue if busy).
2. Create worktree from `main`, branch name from config template (user-chosen; default `fix/issue-N`). **Do NOT use `claude/` prefix** — user rejected it.
3. Symlink context CLAUDE.md.
4. Spawn `claude -p` with prompt: "Issue N in repo X has been assigned to you. Execute: validate issue still applies, clarify any ambiguity (checkpoint if needed), run /superpowers flow, implement, test, run codex review, address feedback, push branch, open PR. Wait at PR-opened state — do not merge."
5. Session runs until it either hits BLOCKED_NEEDS_INPUT, DONE (PR opened), or FAILED.
6. On DONE-PR-opened: Telegram ping "PR #N ready for review: <url>." Orchestrator watches for merge event (poll or webhook — poll is fine).
7. On merge detected: spawn `/deploy-verify` session. On success: close the GH issue with a comment. On failure: Telegram alert with logs.
8. Release repo lock.

### 6.3 Post-merge bug flow (manual user trigger)

User messages Telegram: `bug <repo> <description>`.

1. Orchestrator looks up recent merges in that repo.
2. Creates a fresh `fix/issue-postmerge-<timestamp>` branch from `main`.
3. Spawns `claude -p` with prompt including the bug report and the explicit branch name.
4. Same flow as dev pipeline from step 4 onward.

---

## 7. Configuration

`~/dev-sync/config/orchestrator.yaml`:

```yaml
node_id: "ubuntu-desktop-01"
timezone: "America/New_York"          # user sets

paths:
  state_db: "~/.dev-sync/state.db"
  worktrees: "~/.dev-sync/worktrees"
  bare_repos: "~/.dev-sync/repos"
  contexts: "~/dev-sync/contexts"
  skills: "~/dev-sync/skills"

claude:
  binary: "claude"                    # or full path
  default_timeout_seconds: 1800       # 30min per session
  output_format: "json"

github:
  cli: "gh"
  poll_interval_seconds: 300

telegram:
  socket_path: "/run/user/1000/dev-sync.sock"
  chat_id: 123456789                  # user's personal chat

dashboard:
  url: "https://dev-sync-dashboard.ondigitalocean.app"
  auth_token_env: "DEV_SYNC_DASHBOARD_TOKEN"
  heartbeat_interval_seconds: 300

repos:
  - name: "user/project-a"
    local_path: "~/code/project-a"
    deploys_to_digitalocean: true
    do_app_id: "abc-123-def"
    dev_branch_template: "fix/issue-{n}"
    issue_clarification_mode: "telegram"  # "telegram" | "github_comment" | "both"
    code_review:
      method: "mcp_then_cli"              # "mcp_only" | "cli_only" | "mcp_then_cli"
      mcp_tool: "mcp__codex-reviewer__codex_review"
      cli_command: "codex-cli review"
    deploy_verify_timeout_seconds: 1200   # 20 min
    secops:
      auto_merge_dependabot: true
      auto_merge_if_green_and_minor: true
  - name: "user/project-b"
    ...
```

Secrets (`.env`, never committed):

```
GITHUB_TOKEN=...
TELEGRAM_BOT_TOKEN=...
DEV_SYNC_DASHBOARD_TOKEN=...
DIGITALOCEAN_API_TOKEN=...
```

---

## 8. Phased implementation order

Recommend the peer builds the dashboard in parallel with you building the orchestrator skeleton.

### Phase 0 — Skeleton (orchestrator, ~1 day)

- systemd units, empty `main.py` that logs and heartbeats.
- sqlite schema + migrations applied at startup.
- YAML config loader with validation.
- Dashboard client that pushes heartbeat every 5 min.
- **Gate**: you can `systemctl start dev-sync-orchestrator`, watch heartbeats land on the dashboard for 10 min, restart cleanly.

### Phase 1 — Telegram bridge (~0.5 day)

- Bridge process with unix socket.
- `send`, `ask`, `answer` ops.
- Orchestrator `telegram_client.py` calling the bridge.
- **Gate**: `dev-sync-cli telegram send "hello"` delivers to your phone.

### Phase 2 — Claude dispatcher (~1 day)

- `dispatcher.py` spawns `claude -p` with env vars, captures JSON output.
- Worktree creation and cleanup.
- State file parsing (DONE / BLOCKED / FAILED).
- Attribution scrub hooks installed.
- **Gate**: a toy skill that writes `DONE` with a summary runs end-to-end.

### Phase 3 — Secops pipeline (~1 day)

- `pipelines/secops.py` wiring your existing `/gh-dashboard` and `/gh-secops` skills.
- Cron job at 6am.
- Aggregated dashboard event on completion.
- **Gate**: Run manually via `dev-sync-cli trigger secops`, verify full flow on one repo.

### Phase 4 — Dev pipeline (~1–2 days)

- GitHub poller.
- `pipelines/dev.py` with worktree, branch creation, prompt assembly.
- Checkpoint protocol with real back-and-forth over Telegram.
- PR-watch for merge detection.
- `/deploy-verify` handoff.
- **Gate**: Self-assign a real issue, watch the full flow through to PR-opened, answer a question via Telegram, see the PR land.

### Phase 5 — Hardening (~1 day)

- Stale session reaping (kill `claude -p` after timeout, mark session failed).
- Retry logic for dashboard pushes.
- Graceful shutdown (finish in-flight sessions or hand off cleanly).
- Log rotation.
- README with runbook.

---

## 9. Dashboard spec (separate repo, peer builds)

Repo: `dev-sync-dashboard`. FastAPI + Jinja2 templates + sqlite + `.do/app.yaml` for DO App Platform.

### API

All writes require `Authorization: Bearer <token>` matching `DASHBOARD_TOKEN` env var.

**POST `/heartbeat`**
```json
{
  "node_id": "ubuntu-desktop-01",
  "timestamp": "2026-04-16T14:23:00Z",
  "orchestrator_version": "0.1.0",
  "uptime_seconds": 3600,
  "active_sessions": 2,
  "last_github_poll": "2026-04-16T14:20:00Z",
  "last_github_poll_status": "ok"
}
```

**POST `/event`**
```json
{
  "node_id": "ubuntu-desktop-01",
  "timestamp": "2026-04-16T14:23:00Z",
  "level": "info|warn|error",
  "pipeline": "secops|dev|system",
  "repo": "user/project-a",
  "session_id": "sess_abc123",
  "message": "Secops run complete: 3 PRs merged, 1 needs review",
  "data": {}
}
```

**GET `/`** — HTML dashboard. Shows:
- Big status indicator: GREEN (heartbeat <6 min old), YELLOW (6–10 min), RED (>10 min).
- Active sessions table (from last heartbeat payload).
- Last 50 events, filterable by level/pipeline/repo.
- Time-since-last-heartbeat live counter.

**GET `/api/status`** — same info as `/` but JSON.

### Storage

sqlite on a mounted DO App Platform volume (`/data`). Tables:

```sql
CREATE TABLE heartbeats (
  id INTEGER PRIMARY KEY,
  node_id TEXT,
  received_at INTEGER,
  payload TEXT  -- raw JSON
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  node_id TEXT,
  ts INTEGER,
  level TEXT,
  pipeline TEXT,
  repo TEXT,
  session_id TEXT,
  message TEXT,
  data TEXT
);
```

Retention: prune heartbeats >7 days old, events >30 days old, via a background task.

### Alerting (optional, decide during build)

If `ALERT_TELEGRAM_BOT_TOKEN` and `ALERT_CHAT_ID` env vars are set, and the dashboard detects no heartbeat for >10 min, it sends a Telegram alert directly (independent of the local bridge, which is presumably also dead if heartbeats stopped).

### Deployment

`.do/app.yaml` minimal spec, $5/mo basic instance, 1GB mounted volume for sqlite. GitHub-connected for auto-deploy on push to `main`.

---

## 10. Security notes

- All tokens in `.env` files with `chmod 600`. Never committed.
- Unix socket permissions: `srw-------` (owner only).
- The dashboard bearer token should be a 32-byte random hex string. Rotate quarterly (manual).
- Worktrees are under `~/.dev-sync/` with restrictive perms.
- `claude -p` inherits the user's environment — no extra sandboxing. This is a single-user machine and the user trusts Claude Code.
- If secops ever runs against an untrusted repo, revisit this. For the user's own OSS projects, the threat model is low.

---

## 11. Resolved design decisions

These were open questions resolved during spec finalization:

1. **Stale `claude -p` timeout.** 30min default timeout. Validated empirically during hardening; adjust per-repo if needed.

2. **Dev pipeline issue clarification.** Start with Telegram-only (no autonomous GH issue comments). **Configurable per repo** via `issue_clarification_mode`:
   ```yaml
   repos:
     - name: "user/project-a"
       issue_clarification_mode: "telegram"  # "telegram" | "github_comment" | "both"
   ```

3. **Code review invocation (codex).** Try MCP tool (`mcp__codex-reviewer__codex_review`) first; if MCP fails, fall back to `codex-cli review <path>`. **Configurable per repo**:
   ```yaml
   repos:
     - name: "user/project-a"
       code_review:
         method: "mcp_then_cli"  # "mcp_only" | "cli_only" | "mcp_then_cli"
         mcp_tool: "mcp__codex-reviewer__codex_review"
         cli_command: "codex-cli review"
   ```

   **Note on circular validation:** Claude produces the code and a separate codex instance validates it. This is intentional - codex runs as an independent reviewer with its own context, not as a self-check within the same session. The orchestrator spawns codex review as a distinct step after implementation, breaking the "validate your own work" loop.

4. **`/deploy-verify` timeout.** 20 minutes per repo before marking failed. DO deploys typically take 2-5 min; this allows headroom for slow builds.

5. **Log location and retention.** Systemd journal for daemon logs, per-session logs at `~/.dev-sync/logs/<session-id>.log`, 30-day auto-rotation.

---

## 12. What NOT to do

To save Claude Code cycles when it executes this spec:

- Do not build a generic agent framework. This is a narrow tool for one operator's flow.
- Do not add a web UI to the local orchestrator. The DO dashboard is the UI.
- Do not try to pause `claude -p` mid-execution. Checkpoint and resume.
- Do not commit `CLAUDE.md` or `.dev-sync/` to any project repo. These are always external.
- Do not use any branch naming that starts with `claude/`. User rejects this.
- Do not add co-authorship attribution anywhere. The git hooks enforce this, but don't work around them.
- Do not import `anthropic` SDK to talk to Claude directly. Shell out to `claude -p`. The subscription is what powers this.
- Do not add a queue system (Redis, RabbitMQ). sqlite + in-process APScheduler is enough for this scale.
