---
title: Orchestrator Implementation Design
layout: default
parent: Specs
grand_parent: Design & history
nav_order: 1
---

# ctrlrelay Orchestrator — Implementation Design

**Date:** 2026-04-17  
**Status:** Approved  
**Approach:** Minimal Viable Orchestrator (MVO) with PyPI-ready package structure  
**Base spec:** `docs/ctrlrelay-orchestrator-spec.md` — this document is a delta, not standalone

> **Note:** This design extends the base spec with PyPI packaging, cross-platform support, and refined abstractions. For full behavioral details (pipeline flows, scheduling jobs, complete SQLite schema, security model), refer to the base spec. This document only overrides or adds to what's there.

## Overview

A local-first, cron-driven orchestrator that wraps `claude -p` (headless Claude Code) to run secops and dev pipelines across multiple GitHub repos. Human-in-the-loop via Telegram, status dashboard on DigitalOcean.

**Key decisions from brainstorming:**
- Portable across macOS and Ubuntu
- Published as Python package on PyPI (`pip install ctrlrelay`)
- Config editable locally now, via dashboard later (sync on heartbeat)
- Configurable repo subset, expand over time
- Fine-grained automation policies per repo (auto/ask/never per operation)

## 1. Package Structure

```
ctrlrelay/
├── pyproject.toml
├── src/ctrlrelay/
│   ├── __init__.py              # version, public API
│   ├── cli.py                   # Typer CLI
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py            # YAML loader, ConfigProvider abstraction
│   │   ├── state.py             # SQLite state layer
│   │   ├── dispatcher.py        # claude -p subprocess manager
│   │   ├── checkpoint.py        # Protocol parser/writer
│   │   ├── worktree.py          # git worktree management
│   │   └── github.py            # gh CLI wrapper
│   ├── transports/
│   │   ├── __init__.py
│   │   ├── base.py              # Transport protocol (ask/answer)
│   │   ├── telegram.py          # Real Telegram client
│   │   └── file_mock.py         # File-based mock for testing
│   ├── pipelines/
│   │   ├── __init__.py
│   │   ├── base.py              # Pipeline protocol
│   │   ├── secops.py
│   │   └── dev.py
│   └── dashboard/
│       ├── __init__.py
│       └── client.py            # Heartbeat + event push + config pull
├── tests/
├── config/
│   └── orchestrator.yaml.example
└── README.md
```

### CLI Commands

```bash
# Core operations
ctrlrelay run secops [--repo REPO]
ctrlrelay run dev --issue 42 --repo X
ctrlrelay status

# Skill management
ctrlrelay skills audit [--fix]
ctrlrelay skills list

# Config
ctrlrelay config validate
ctrlrelay config repos

# Bridge
ctrlrelay bridge start [--daemon]
ctrlrelay bridge stop
ctrlrelay bridge status
ctrlrelay bridge test

# Daemon (Phase 5+)
ctrlrelay daemon start
ctrlrelay daemon stop
ctrlrelay daemon status
```

## 2. Checkpoint Protocol

The contract between orchestrator and skills. Skills write state to `$CTRLRELAY_STATE_FILE`.

### Atomic Write Rules

1. **Write to temp, then rename:** `checkpoint.*` helpers write to `$CTRLRELAY_STATE_FILE.tmp`, then `os.rename()` to final path
2. **One checkpoint per session:** Only the final state matters; overwrites are allowed
3. **Orchestrator deletes on read:** After parsing, orchestrator removes the state file to prevent re-reads on restart
4. **Truncation protection:** Orchestrator validates JSON is complete before acting; incomplete file = FAILED

### State File Schema

```json
{
  "version": "1",
  "status": "DONE | BLOCKED_NEEDS_INPUT | FAILED",
  "session_id": "uuid-from-claude",
  "timestamp": "2026-04-17T12:00:00Z",
  
  "summary": "One-line human-readable result",
  
  "question": "What should I do about X?",
  "question_context": {
    "repo": "user/project",
    "pr": 42,
    "options": ["A: Do this", "B: Do that"]
  },
  
  "error": "Error message if FAILED",
  "recoverable": true,
  
  "outputs": {
    "pr_url": "https://github.com/...",
    "merged_prs": [101, 102],
    "issues_created": [5, 6]
  }
}
```

### Skill Helper Library

Public API re-exported from package root (`src/ctrlrelay/__init__.py`):

```python
from ctrlrelay import checkpoint  # Re-exported from ctrlrelay.core.checkpoint

checkpoint.done(summary="Merged 3 PRs", outputs={"merged_prs": [1,2,3]})

checkpoint.blocked(
    question="Pin to 2.4.1 or bump to 2.5.0?",
    context={"pr": 42, "options": ["2.4.1 (safe)", "2.5.0 (breaking)"]}
)

checkpoint.failed(error="gh CLI returned 404", recoverable=False)
```

### Orchestrator Behavior

| Status | Action |
|--------|--------|
| `DONE` | Log summary, release lock, push event to dashboard |
| `BLOCKED_NEEDS_INPUT` | Forward question to transport, wait, resume with `claude --resume <session_id>` |
| `FAILED` | Alert via transport, release lock, mark session failed |

### Cancellation & Timeout Flow

When `/cancel <session_id>` is received or session timeout expires:

1. **Kill process:** `SIGTERM` to `claude -p` subprocess, wait 5s, then `SIGKILL` if needed
2. **Update state:** Session status → `cancelled` or `timeout`
3. **Release lock:** Remove from `repo_locks` table
4. **Cleanup worktree:** Keep worktree for inspection; add to cleanup queue (reaped after 24h)
5. **Notify:** Push `session_cancelled` or `session_timeout` event to dashboard
6. **Clear pending:** Remove from `telegram_pending` table if waiting for input

## 3. Skill Audit Tool

First checkpoint-compliant skill. Checks existing skills for orchestrator readiness.

### Checks

| Check | Pass Criteria | Auto-fixable? |
|-------|---------------|---------------|
| Checkpoint calls | Uses checkpoint helpers or writes state file | No |
| No interactive prompts | No `input()`, `read -p`, `Confirm()` | No |
| No browser-only tools | No `mcp__playwright__*` without fallback | No |
| Exit codes | Exits 0 on DONE/BLOCKED, non-zero on FAILED | Yes |
| Context path | Uses `$REPO_CONTEXT_PATH` not hardcoded | Yes |
| Attribution-free | No "Claude", "Anthropic" in output | Yes |

### Output

```markdown
## Skill Audit Report

| Skill | Checkpoint | Headless | Context | Attribution | Status |
|-------|------------|----------|---------|-------------|--------|
| gh-secops | ❌ | ✅ | ❌ | ✅ | NOT READY |
| deploy-verify | ❌ | ✅ | ✅ | ✅ | NOT READY |
```

## 4. Configuration

### orchestrator.yaml

```yaml
version: "1"
node_id: "macbook-pro-01"
timezone: "America/Santiago"

paths:
  state_db: "~/.ctrlrelay/state.db"
  worktrees: "~/.ctrlrelay/worktrees"
  bare_repos: "~/.ctrlrelay/repos"    # Bare clones for worktree creation
  contexts: "~/.ctrlrelay/contexts"
  skills: "~/.ctrlrelay/claude-config/skills"

claude:
  binary: "claude"
  default_timeout_seconds: 1800
  output_format: "json"

transport:
  type: "telegram"  # or "file_mock"
  telegram:
    bot_token_env: "CTRLRELAY_TELEGRAM_TOKEN"
    chat_id: 123456789
    socket_path: "~/.ctrlrelay/ctrlrelay.sock"  # Linux: /run/user/$UID/ctrlrelay.sock
  file_mock:
    inbox: "~/.ctrlrelay/inbox.txt"
    outbox: "~/.ctrlrelay/outbox.txt"

dashboard:
  enabled: true
  url: "https://ctrlrelay-dashboard.example.com"
  auth_token_env: "CTRLRELAY_DASHBOARD_TOKEN"
  sync_config_on_heartbeat: false  # Future

repos:
  - name: "AInvirion/TORtopus"
    local_path: "~/Projects/AINVIRION/TORtopus"
    automation:
      dependabot_patch: auto
      dependabot_minor: ask
      dependabot_major: never
      codeql_dismiss: ask
      secret_alerts: never
      deploy_after_merge: auto
    deploy:
      provider: "digitalocean"
      app_id: "abc-123"
    code_review:
      method: "mcp_then_cli"
    dev_branch_template: "fix/issue-{n}"
```

### ConfigProvider Abstraction

```python
class ConfigProvider(Protocol):
    def load(self) -> Config: ...
    def get_repo(self, name: str) -> RepoConfig: ...
    def reload(self) -> None: ...

class FileConfigProvider(ConfigProvider):
    """Reads from local YAML file"""

class DashboardConfigProvider(ConfigProvider):
    """Future: fetches from dashboard API"""
```

## 5. State (SQLite)

Path: `~/.ctrlrelay/state.db`

> **Full schema:** See base spec section 4.6. This section shows the tables this design adds or modifies.

**Additional tables from base spec (not repeated here):** `github_cursor`, `telegram_pending`

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  pipeline TEXT NOT NULL,
  repo TEXT NOT NULL,
  issue_number INTEGER,
  worktree_path TEXT,
  status TEXT NOT NULL,
  blocked_question TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  claude_exit_code INTEGER,
  summary TEXT
);

CREATE TABLE repo_locks (
  repo TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  acquired_at INTEGER NOT NULL
);

CREATE TABLE automation_decisions (
  id INTEGER PRIMARY KEY,
  repo TEXT NOT NULL,
  operation TEXT NOT NULL,
  policy TEXT NOT NULL,
  item_id TEXT,
  decision TEXT,
  decided_by TEXT,
  decided_at INTEGER,
  context TEXT
);
```

## 6. Automation Policy System

Six policies per repo, each can be `auto`, `ask`, or `never`:

- `dependabot_patch`
- `dependabot_minor`
- `dependabot_major`
- `codeql_dismiss`
- `secret_alerts`
- `deploy_after_merge`

### Policy API

**Decision vocabulary (canonical):** `approve`, `reject`, `skip`, `timeout`

```python
class PolicyDecision(Enum):
    APPROVE = "approve"   # Proceed with action
    REJECT = "reject"     # Don't proceed, mark as rejected
    SKIP = "skip"         # Don't proceed, leave for manual handling
    TIMEOUT = "timeout"   # No response within deadline

class AutomationPolicy:
    async def evaluate(
        self,
        repo: str,
        operation: str,
        context: dict,
    ) -> PolicyDecision:
        """
        Returns immediately for 'auto' (APPROVE) or 'never' (SKIP) policies.
        For 'ask' policies, sends to transport and returns APPROVE/REJECT/SKIP/TIMEOUT.
        """

    async def await_decision(
        self, 
        decision_id: str, 
        timeout: int = 3600
    ) -> PolicyDecision:
        """Wait for user response. Returns TIMEOUT if no response."""
```

### Telegram Format for `ask`

```
🔔 Approval needed: dependabot_minor

Repo: AInvirion/TORtopus
PR: #42 - Bump requests 2.28.0 → 2.31.0

CI: ✅ green

Reply: ✅ approve | ❌ reject | ⏸️ skip
```

User response is normalized to `PolicyDecision` enum.

## 7. Telegram Bridge

Separate process, communicates via Unix socket.

### Architecture

```
Orchestrator ◄─── Unix socket ───► Telegram Bridge ◄──► Telegram API
```

**Socket path (cross-platform):**
- Linux: `/run/user/$UID/ctrlrelay.sock`
- macOS: `~/.ctrlrelay/ctrlrelay.sock`

Configurable via `transport.telegram.socket_path` in `orchestrator.yaml`.

### Socket Protocol

**Framing:** Newline-delimited JSON (one JSON object per line, `\n` terminated)

**Permissions:** Socket created with mode `0600` (owner only). Bridge validates peer UID matches owner.

```json
// Orchestrator → Bridge
{"op": "send", "request_id": "r-001", "text": "Secops complete"}
{"op": "ask", "request_id": "r-002", "question": "Approve?", "options": ["approve", "reject", "skip"]}

// Bridge → Orchestrator
{"op": "ack", "request_id": "r-001", "status": "sent"}
{"op": "ack", "request_id": "r-002", "status": "pending"}
{"op": "answer", "request_id": "r-002", "answer": "approve", "answered_at": "2026-04-17T12:00:00Z"}
{"op": "error", "request_id": "r-003", "error": "telegram_api_error", "message": "..."}
```

**Retry:** Orchestrator retries failed sends 3x with exponential backoff. After 3 failures, logs locally and continues.

**Liveness:** Orchestrator sends `{"op": "ping"}` every 30s; bridge responds `{"op": "pong"}`. No pong in 60s = bridge considered dead.

### Transport Abstraction

```python
class Transport(Protocol):
    async def send(self, message: str) -> None
    async def ask(self, question: str, options: list[str] | None, timeout: int) -> str

class TelegramTransport(Transport): ...
class FileMockTransport(Transport): ...  # For testing
class CLITransport(Transport): ...        # For manual testing
```

### Bot Commands

- `/status` - Orchestrator status, active sessions
- `/pending` - Questions awaiting answer
- `/repos` - Configured repos
- `/bug <repo> <description>` - Post-merge bug fix
- `/cancel <session_id>` - Cancel session

## 8. Dashboard Integration

### Client (in ctrlrelay package)

```python
class DashboardClient:
    async def heartbeat(self, status: HeartbeatPayload) -> HeartbeatResponse
    async def push_event(self, event: EventPayload) -> None
    async def fetch_config(self, version: str) -> dict | None  # Future
```

### Heartbeat Payload

```python
@dataclass
class HeartbeatPayload:
    node_id: str
    timestamp: str
    version: str
    uptime_seconds: int
    platform: str
    active_sessions: list[dict]
    last_github_poll: str | None
    last_github_poll_status: str
    repos_configured: int
    repos_active: int
```

### Event Types

- `session_started`, `session_completed`, `session_failed`, `session_blocked`, `session_resumed`
- `automation_decision`
- `pr_merged`, `pr_created`
- `deploy_started`, `deploy_completed`, `deploy_failed`

### Server (separate repo: ctrlrelay-dashboard)

FastAPI app on DigitalOcean App Platform.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/heartbeat` | Receive heartbeat |
| POST | `/event` | Receive event |
| GET | `/` | HTML dashboard |
| GET | `/api/status` | JSON status |
| GET | `/api/events` | Paginated events |
| GET | `/config` | Future: config for node |
| PUT | `/config` | Future: update config |

## 9. Implementation Phases

| Phase | Days | Deliverable |
|-------|------|-------------|
| 0 | 1-2 | Package skeleton, config, state |
| 1 | 3-5 | Checkpoint protocol, skill audit tool |
| 2 | 6-8 | Telegram bridge |
| 3 | 9-12 | Secops pipeline |
| 4 | 13-17 | Dev pipeline |
| 5 | 18-20 | Daemon + scheduling |
| 6 | 21-25 | Dashboard server |
| 7 | 26-30 | Hardening + PyPI release |

### Phase Gates

- **Phase 0:** `pip install -e .` works, `ctrlrelay config validate` passes
- **Phase 1:** `ctrlrelay skills audit` produces compliance report
- **Phase 2:** `ctrlrelay bridge test` delivers message to phone
- **Phase 3:** Secops runs on 2-3 repos, events logged locally (dashboard client queues if server unavailable)
- **Phase 4:** Self-assign issue, get PR notification
- **Phase 5:** Start daemon, wake up to secops summary
- **Phase 6:** Dashboard shows live status, queued events drain
- **Phase 7:** `pip install ctrlrelay` from PyPI works

## 10. What NOT to Do

From original spec, still applies:

- No generic agent framework — narrow tool for one operator
- No web UI on local orchestrator — dashboard is the UI
- No pause mid-execution — checkpoint and resume
- No `CLAUDE.md` in project repos — always external
- No `claude/` branch prefix
- No co-authorship attribution
- No direct `anthropic` SDK — shell out to `claude -p`
- No Redis/RabbitMQ — sqlite + APScheduler

## 11. Relationship to Original Spec

This design extends `docs/ctrlrelay-orchestrator-spec.md` with:

1. **PyPI package structure** — publishable library/CLI
2. **Cross-platform support** — macOS + Ubuntu
3. **Granular automation policies** — 6 operations × auto/ask/never
4. **Transport abstraction** — Telegram/FileMock/CLI swappable
5. **ConfigProvider abstraction** — local files now, dashboard sync later
6. **Skill audit tool** — first checkpoint-compliant skill

The original spec remains the detailed reference for system behavior. This document captures the implementation design and decisions from brainstorming.
