# dev-sync Orchestrator — Implementation Design

**Date:** 2026-04-17  
**Status:** Approved  
**Approach:** Minimal Viable Orchestrator (MVO) with PyPI-ready package structure

## Overview

A local-first, cron-driven orchestrator that wraps `claude -p` (headless Claude Code) to run secops and dev pipelines across multiple GitHub repos. Human-in-the-loop via Telegram, status dashboard on DigitalOcean.

**Key decisions from brainstorming:**
- Portable across macOS and Ubuntu
- Published as Python package on PyPI (`pip install dev-sync`)
- Config editable locally now, via dashboard later (sync on heartbeat)
- Configurable repo subset, expand over time
- Fine-grained automation policies per repo (auto/ask/never per operation)

## 1. Package Structure

```
dev-sync/
├── pyproject.toml
├── src/dev_sync/
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
dev-sync run secops [--repo REPO]
dev-sync run dev --issue 42 --repo X
dev-sync status

# Skill management
dev-sync skills audit [--fix]
dev-sync skills list

# Config
dev-sync config validate
dev-sync config repos

# Bridge
dev-sync bridge start [--daemon]
dev-sync bridge stop
dev-sync bridge status
dev-sync bridge test

# Daemon (Phase 5+)
dev-sync daemon start
dev-sync daemon stop
dev-sync daemon status
```

## 2. Checkpoint Protocol

The contract between orchestrator and skills. Skills write state to `$DEV_SYNC_STATE_FILE`.

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

```python
from dev_sync.checkpoint import checkpoint

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
  state_db: "~/.dev-sync/state.db"
  worktrees: "~/.dev-sync/worktrees"
  contexts: "~/dev-sync/contexts"
  skills: "~/dev-sync/claude-config/skills"

claude:
  binary: "claude"
  default_timeout_seconds: 1800
  output_format: "json"

transport:
  type: "telegram"  # or "file_mock"
  telegram:
    bot_token_env: "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: 123456789
    socket_path: "~/.dev-sync/dev-sync.sock"  # Linux: /run/user/$UID/dev-sync.sock
  file_mock:
    inbox: "~/.dev-sync/inbox.txt"
    outbox: "~/.dev-sync/outbox.txt"

dashboard:
  enabled: true
  url: "https://dev-sync-dashboard.example.com"
  auth_token_env: "DEV_SYNC_DASHBOARD_TOKEN"
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

Path: `~/.dev-sync/state.db`

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

```python
class AutomationPolicy:
    async def evaluate(
        self,
        repo: str,
        operation: str,
        context: dict,
    ) -> PolicyDecision:  # PROCEED | WAIT | SKIP

    async def await_decision(self, decision_id: str, timeout: int) -> bool
```

### Telegram Format for `ask`

```
🔔 Approval needed: dependabot_minor

Repo: AInvirion/TORtopus
PR: #42 - Bump requests 2.28.0 → 2.31.0

CI: ✅ green

Reply: ✅ approve | ❌ reject | ⏸️ skip
```

## 7. Telegram Bridge

Separate process, communicates via Unix socket.

### Architecture

```
Orchestrator ◄─── Unix socket ───► Telegram Bridge ◄──► Telegram API
```

**Socket path (cross-platform):**
- Linux: `/run/user/$UID/dev-sync.sock`
- macOS: `~/.dev-sync/dev-sync.sock`

Configurable via `transport.telegram.socket_path` in `orchestrator.yaml`.

### Socket Protocol

```json
{"op": "send", "request_id": "r-001", "text": "Secops complete"}
{"op": "ask", "request_id": "r-002", "question": "Approve?", "options": ["approve", "reject"]}
{"op": "answer", "request_id": "r-002", "answer": "approve"}
```

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

### Client (in dev-sync package)

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

### Server (separate repo: dev-sync-dashboard)

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

- **Phase 0:** `pip install -e .` works, `dev-sync config validate` passes
- **Phase 1:** `dev-sync skills audit` produces compliance report
- **Phase 2:** `dev-sync bridge test` delivers message to phone
- **Phase 3:** Secops runs on 2-3 repos, events on dashboard
- **Phase 4:** Self-assign issue, get PR notification
- **Phase 5:** Start daemon, wake up to secops summary
- **Phase 6:** Dashboard shows live status
- **Phase 7:** `pip install dev-sync` from PyPI works

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

This design extends `docs/dev-sync-orchestrator-spec.md` with:

1. **PyPI package structure** — publishable library/CLI
2. **Cross-platform support** — macOS + Ubuntu
3. **Granular automation policies** — 6 operations × auto/ask/never
4. **Transport abstraction** — Telegram/FileMock/CLI swappable
5. **ConfigProvider abstraction** — local files now, dashboard sync later
6. **Skill audit tool** — first checkpoint-compliant skill

The original spec remains the detailed reference for system behavior. This document captures the implementation design and decisions from brainstorming.
