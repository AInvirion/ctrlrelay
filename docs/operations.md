---
title: Operations
layout: default
nav_order: 7
description: "Run the bridge and poller as long-lived services on macOS (launchd) and Linux (systemd). Read logs and the state DB."
permalink: /operations/
---

# Operations

Once your config works interactively, you usually want the bridge and poller
running unattended. This page covers macOS launchd, Linux systemd, what to
restart after a config change, and how to inspect runtime state.

## Long-lived services

dev-sync ships with no service files — you write your own. The two daemons:

| Service | Command | Why you want it running |
|---|---|---|
| Bridge | `dev-sync bridge start` | Delivers BLOCKED questions to Telegram and routes answers back. |
| Poller | `dev-sync poller start --interval 300` | Watches GitHub for newly assigned issues and runs the dev pipeline. |

Both should restart on failure and on login.

### macOS — launchd

Save plist files under `~/Library/LaunchAgents/`. Use the **absolute** path to
`dev-sync` (run `which dev-sync` to find it) — launchd's PATH is minimal.

`~/Library/LaunchAgents/com.ainvirion.dev-sync-bridge.plist`:

{% raw %}
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
        <string>your-bot-token-here</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/Projects/dev-sync</string>
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
{% endraw %}

`~/Library/LaunchAgents/com.ainvirion.dev-sync-poller.plist`:

{% raw %}
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
        <string>your-bot-token-here</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/Projects/dev-sync</string>
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
{% endraw %}

Create the log directory and load the agents:

```bash
mkdir -p ~/.dev-sync/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-poller.plist
```

Manage them:

```bash
launchctl list | grep dev-sync          # check loaded
launchctl bootout gui/$(id -u)/com.ainvirion.dev-sync-poller    # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.dev-sync-poller.plist  # start
```

### Linux — systemd

Save unit files under `~/.config/systemd/user/`.

`~/.config/systemd/user/dev-sync-bridge.service`:

```ini
[Unit]
Description=dev-sync Telegram bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Projects/dev-sync
Environment=DEV_SYNC_TELEGRAM_TOKEN=your-bot-token-here
ExecStart=%h/.local/bin/dev-sync bridge start
Restart=always
RestartSec=5
StandardOutput=append:%h/.dev-sync/logs/bridge.log
StandardError=append:%h/.dev-sync/logs/bridge.error.log

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/dev-sync-poller.service`:

```ini
[Unit]
Description=dev-sync issue poller
After=network-online.target dev-sync-bridge.service

[Service]
Type=simple
WorkingDirectory=%h/Projects/dev-sync
Environment=DEV_SYNC_TELEGRAM_TOKEN=your-bot-token-here
ExecStart=%h/.local/bin/dev-sync poller start --interval 300
Restart=always
RestartSec=5
StandardOutput=append:%h/.dev-sync/logs/poller.log
StandardError=append:%h/.dev-sync/logs/poller.error.log

[Install]
WantedBy=default.target
```

Enable and start:

```bash
mkdir -p ~/.dev-sync/logs
systemctl --user daemon-reload
systemctl --user enable --now dev-sync-bridge.service
systemctl --user enable --now dev-sync-poller.service
```

To survive logout (start at boot, stop at shutdown):

```bash
sudo loginctl enable-linger "$USER"
```

Check status / logs:

```bash
systemctl --user status dev-sync-poller
journalctl --user -u dev-sync-poller -f
```

## When to restart

| You changed... | Restart... |
|---|---|
| `transport.*` (Telegram token, chat ID, socket path) | bridge **and** poller |
| `repos[]` (added/removed/renamed) | poller |
| `claude.*` (binary path, timeout) | poller |
| `paths.*` | both |
| Anything in `dashboard.*` | poller |
| Bot token env var | bridge **and** poller |

After a `dev-sync` package upgrade (`uv pip install -e .`), restart both.

After merging a PR that the dev pipeline opened, no manual restart is required —
the PR is referenced from the open branch in your bare repo, so cleanup is
handled by GitHub on merge. The worktree was already removed when the session
finished.

## Logs

By default, both daemons log to the paths configured in your launchd /
systemd unit files. The recommended location is `~/.dev-sync/logs/`.

Tail them:

```bash
tail -f ~/.dev-sync/logs/poller.log
tail -f ~/.dev-sync/logs/bridge.log
tail -f ~/.dev-sync/logs/poller.error.log
```

The poller prints one line per detected issue and one line per pipeline outcome.
The bridge prints connection events and Telegram API errors.

## Inspecting state

### `dev-sync status`

The fastest way to see what's happening:

```bash
dev-sync status
```

Shows held repo locks and the 5 most recent sessions with their statuses
(`running`, `done`, `blocked`, `failed`).

### State DB schema

The state DB is plain SQLite at `paths.state_db`. Open it with `sqlite3` for
ad-hoc queries:

```bash
sqlite3 ~/.dev-sync/state.db
sqlite> .tables
sqlite> .schema sessions
sqlite> SELECT id, pipeline, repo, status, started_at FROM sessions ORDER BY started_at DESC LIMIT 20;
```

Tables (defined in
[`src/dev_sync/core/state.py`](https://github.com/AInvirion/dev-sync/blob/main/src/dev_sync/core/state.py)):

- **`sessions`** — every pipeline run. Columns: `id`, `pipeline`, `repo`,
  `issue_number`, `worktree_path`, `status`, `blocked_question`, `started_at`,
  `ended_at`, `claude_exit_code`, `summary`.
- **`repo_locks`** — currently held per-repo locks. PK on `repo`.
- **`github_cursor`** — last-seen issue/PR timestamps per repo (used by the
  poller to bound API calls).
- **`telegram_pending`** — outstanding bridge questions awaiting an operator
  reply.
- **`automation_decisions`** — operator decisions on `ask`-policy automation
  prompts.

### Worktrees on disk

Per-session worktrees live under `paths.worktrees`, named
`<owner>-<repo>-<session-id>`. `dev-sync` removes them on `DONE` or terminal
`FAILED`; `BLOCKED` sessions keep their worktree so a manual operator can
inspect / resume.

If something goes wrong and you need to reclaim a stuck worktree:

```bash
git -C ~/.dev-sync/repos/your-org-your-app.git worktree list
git -C ~/.dev-sync/repos/your-org-your-app.git worktree remove --force /path/to/orphan
```

### Poller seen-set

The poller's seen-issue state is a JSON file at
`<state_db_dir>/poller_state.json`. If you want to force the poller to
re-process a specific issue, delete its number from `seen_issues[<repo>]` (or
delete the file entirely to reseed from scratch on next start).
