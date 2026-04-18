# dev-sync

Local-first automation orchestrator for GitHub repos. Wraps Claude CLI to automate security triage, issue-to-PR workflows, and multi-repo operations.

📖 **Docs site:** [ainvirion.github.io/dev-sync](https://ainvirion.github.io/dev-sync/) — design specs, implementation plans, and operator guides, built from [`docs/`](docs/) with Jekyll + just-the-docs and deployed via GitHub Pages.

## Features

- **Secops Pipeline** - Automated security triage across repos (Dependabot alerts, security PRs)
- **Dev Pipeline** - Issue-to-PR automation (detect assigned issues, implement, open PR)
- **Config Sync** - Keep Claude/Codex config synced across devices
- **Human-in-the-loop** - Claude asks questions when blocked, you answer via CLI or Telegram

## Installation

```bash
# Clone the repo
git clone https://github.com/AInvirion/dev-sync.git
cd dev-sync

# Install with uv (recommended)
uv pip install -e .

# Or with pip
pip install -e .
```

### Requirements

- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command)
- [GitHub CLI](https://cli.github.com/) (`gh` command, authenticated)

## Quick Start

### 1. Configure

```bash
# Copy example config
cp config/orchestrator.example.yaml config/orchestrator.yaml

# Edit with your repos
vim config/orchestrator.yaml
```

Example config:
```yaml
version: "1"
node_id: "my-laptop"
timezone: "America/New_York"

paths:
  state_db: "~/.dev-sync/state.db"
  worktrees: "~/.dev-sync/worktrees"
  bare_repos: "~/.dev-sync/repos"
  contexts: "~/dev-sync/contexts"

claude:
  binary: "claude"
  default_timeout_seconds: 1800

repos:
  - name: "owner/repo"
    local_path: "~/Projects/repo"
    automation:
      dependabot_patch: auto
      dependabot_minor: ask
      dependabot_major: never
```

### 2. Run Dev Pipeline on an Issue

```bash
# Work on a specific GitHub issue
dev-sync run dev --issue 123 --repo owner/repo
```

This will:
1. Create a worktree with branch `fix/issue-123`
2. Spawn Claude to implement the fix
3. Open a PR referencing the issue
4. Track session state in the database

### 3. Run Secops Pipeline

```bash
# Security triage across all configured repos
dev-sync run secops
```

### 4. Start Issue Poller (Daemon)

```bash
# Watch for newly assigned issues and auto-process them
dev-sync poller start --interval 300

# Check status
dev-sync poller status

# Stop
dev-sync poller stop
```

## CLI Reference

### Pipeline Commands

```bash
# Run dev pipeline on an issue
dev-sync run dev --issue <number> --repo <owner/repo>

# Run secops pipeline
dev-sync run secops [--repo <owner/repo>]
```

### Poller Commands

```bash
# Start issue poller
dev-sync poller start [--interval 300] [--daemon]

# Check poller status
dev-sync poller status

# Stop poller
dev-sync poller stop
```

### Bridge Commands (Telegram)

```bash
# Start the Telegram bridge
dev-sync bridge start [--daemon]

# Check bridge status
dev-sync bridge status

# Send a test message
dev-sync bridge test -m "Hello from dev-sync!"

# Stop the bridge
dev-sync bridge stop
```

### Status Commands

```bash
# Show orchestrator status and recent sessions
dev-sync status
```

### Config Sync Commands

```bash
# Sync repos (clone/pull)
dev-sync repos

# Export Claude config
dev-sync export

# Import Claude config
dev-sync import
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        dev-sync CLI                         │
├──────────────┬──────────────┬──────────────┬───────────────┤
│  run dev     │  run secops  │   poller     │   sessions    │
└──────┬───────┴──────┬───────┴──────┬───────┴───────┬───────┘
       │              │              │               │
       ▼              ▼              ▼               ▼
┌─────────────────────────────────────────────────────────────┐
│                      Pipeline Layer                          │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐ │
│  │ DevPipeline │  │SecopsPipeline│ │   PostMergeHandler   │ │
│  └─────────────┘  └─────────────┘  └──────────────────────┘ │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                        Core Layer                            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ Dispatcher │  │  Worktree  │  │  GitHub    │            │
│  │  (Claude)  │  │  Manager   │  │    CLI     │            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │   StateDB  │  │   Poller   │  │ PRWatcher  │            │
│  └────────────┘  └────────────┘  └────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

## How It Works

### Dev Pipeline Flow

1. **Issue Detection** - Poller finds issues assigned to you
2. **Branch Creation** - Creates worktree with `fix/issue-{n}` branch
3. **Claude Session** - Spawns Claude CLI with issue context
4. **Implementation** - Claude implements using TDD
5. **PR Creation** - Pushes branch, opens PR
6. **Checkpoint** - Writes state file to signal completion
7. **Cleanup** - Removes worktree (unless blocked)

### Checkpoint Protocol

Claude signals completion by writing JSON to a state file:

```bash
# DONE - PR opened successfully
printf '{"version":"1","status":"DONE","session_id":"...","timestamp":"...","summary":"PR opened","outputs":{"pr_url":"...","pr_number":42}}' > /path/to/state.json

# BLOCKED - Need human input
printf '{"version":"1","status":"BLOCKED_NEEDS_INPUT","session_id":"...","timestamp":"...","question":"What should I do?"}' > /path/to/state.json

# FAILED - Something went wrong
printf '{"version":"1","status":"FAILED","session_id":"...","timestamp":"...","error":"Error message"}' > /path/to/state.json
```

### Session States

| Status | Description |
|--------|-------------|
| `running` | Claude session in progress |
| `done` | Completed successfully |
| `blocked` | Waiting for human input |
| `failed` | Error occurred |

## Configuration

### Repo Configuration

```yaml
repos:
  - name: "owner/repo"
    local_path: "~/Projects/repo"
    dev_branch_template: "fix/issue-{n}"  # Branch naming
    automation:
      dependabot_patch: auto   # auto-merge patch updates
      dependabot_minor: ask    # ask before minor updates
      dependabot_major: never  # never auto-merge major
```

### Automation Levels

| Level | Description |
|-------|-------------|
| `auto` | Auto-merge if CI passes |
| `ask` | Send notification, wait for approval |
| `never` | Skip entirely |

### Telegram Setup

1. Create a bot via [@BotFather](https://t.me/botfather) and get the token
2. Message your bot to initialize the chat
3. Get your chat ID: `curl "https://api.telegram.org/bot<TOKEN>/getUpdates"`
4. Configure:
```yaml
transport:
  type: telegram
  telegram:
    bot_token_env: "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: YOUR_CHAT_ID
    socket_path: "~/.dev-sync/dev-sync.sock"
```
5. Set the environment variable: `export DEV_SYNC_TELEGRAM_TOKEN="your-token"`
6. Start the bridge: `dev-sync bridge start --daemon`

## Running as a Service (macOS)

To run dev-sync automatically on login and after reboots:

### 1. Create launchd plist files

**Bridge service** (`~/Library/LaunchAgents/com.ainvirion.dev-sync-bridge.plist`):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ainvirion.dev-sync-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/dev-sync</string>
        <string>bridge</string>
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DEV_SYNC_TELEGRAM_TOKEN</key>
        <string>YOUR_BOT_TOKEN</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/path/to/dev-sync</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.dev-sync/logs/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.dev-sync/logs/bridge.error.log</string>
</dict>
</plist>
```

**Poller service** (`~/Library/LaunchAgents/com.ainvirion.dev-sync-poller.plist`):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ainvirion.dev-sync-poller</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/dev-sync</string>
        <string>poller</string>
        <string>start</string>
        <string>--interval</string>
        <string>300</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DEV_SYNC_TELEGRAM_TOKEN</key>
        <string>YOUR_BOT_TOKEN</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/path/to/dev-sync</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.dev-sync/logs/poller.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.dev-sync/logs/poller.error.log</string>
</dict>
</plist>
```

### 2. Create logs directory

```bash
mkdir -p ~/.dev-sync/logs
```

### 3. Load the services

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-poller.plist
```

### 4. Managing services

```bash
# Check status
launchctl list | grep dev-sync

# View logs
tail -f ~/.dev-sync/logs/poller.log
tail -f ~/.dev-sync/logs/bridge.log

# Stop services
launchctl bootout gui/$(id -u)/com.ainvirion.dev-sync-bridge
launchctl bootout gui/$(id -u)/com.ainvirion.dev-sync-poller

# Start services
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-poller.plist
```

## Development

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run linting
ruff check src/

# Run specific test
pytest tests/test_dev_pipeline.py -v
```

## License

MIT
