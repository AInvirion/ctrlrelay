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

ctrlrelay ships with no service files — you write your own. The two daemons:

| Service | Command | Why you want it running |
|---|---|---|
| Bridge | `ctrlrelay bridge start` | Delivers BLOCKED questions to Telegram and routes answers back. |
| Poller | `ctrlrelay poller start --interval 300` | Watches GitHub for newly assigned issues and runs the dev pipeline. |

Both should restart on failure and on login.

### macOS — launchd

Save plist files under `~/Library/LaunchAgents/`. Use the **absolute** path to
`ctrlrelay` (run `which ctrlrelay` to find it) — launchd's PATH is minimal.

`~/Library/LaunchAgents/com.ainvirion.ctrlrelay-bridge.plist`:

{% raw %}
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ainvirion.ctrlrelay-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ctrlrelay</string>
        <string>bridge</string>
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CTRLRELAY_TELEGRAM_TOKEN</key>
        <string>your-bot-token-here</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/Projects/ctrlrelay</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.ctrlrelay/logs/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.ctrlrelay/logs/bridge.error.log</string>
</dict>
</plist>
```
{% endraw %}

`~/Library/LaunchAgents/com.ainvirion.ctrlrelay-poller.plist`:

{% raw %}
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ainvirion.ctrlrelay-poller</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ctrlrelay</string>
        <string>poller</string>
        <string>start</string>
        <string>--interval</string>
        <string>300</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CTRLRELAY_TELEGRAM_TOKEN</key>
        <string>your-bot-token-here</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/Projects/ctrlrelay</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.ctrlrelay/logs/poller.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.ctrlrelay/logs/poller.error.log</string>
</dict>
</plist>
```
{% endraw %}

Create the log directory and load the agents:

```bash
mkdir -p ~/.ctrlrelay/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.ctrlrelay-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.ctrlrelay-poller.plist
```

Manage them:

```bash
launchctl list | grep ctrlrelay          # check loaded
launchctl bootout gui/$(id -u)/com.ainvirion.ctrlrelay-poller    # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ainvirion.ctrlrelay-poller.plist  # start
```

### Linux — systemd

Save unit files under `~/.config/systemd/user/`.

`~/.config/systemd/user/ctrlrelay-bridge.service`:

```ini
[Unit]
Description=ctrlrelay Telegram bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Projects/ctrlrelay
Environment=CTRLRELAY_TELEGRAM_TOKEN=your-bot-token-here
ExecStart=%h/.local/bin/ctrlrelay bridge start
Restart=always
RestartSec=5
StandardOutput=append:%h/.ctrlrelay/logs/bridge.log
StandardError=append:%h/.ctrlrelay/logs/bridge.error.log

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/ctrlrelay-poller.service`:

```ini
[Unit]
Description=ctrlrelay issue poller
After=network-online.target ctrlrelay-bridge.service

[Service]
Type=simple
WorkingDirectory=%h/Projects/ctrlrelay
Environment=CTRLRELAY_TELEGRAM_TOKEN=your-bot-token-here
ExecStart=%h/.local/bin/ctrlrelay poller start --interval 300
Restart=always
RestartSec=5
StandardOutput=append:%h/.ctrlrelay/logs/poller.log
StandardError=append:%h/.ctrlrelay/logs/poller.error.log

[Install]
WantedBy=default.target
```

Enable and start:

```bash
mkdir -p ~/.ctrlrelay/logs
systemctl --user daemon-reload
systemctl --user enable --now ctrlrelay-bridge.service
systemctl --user enable --now ctrlrelay-poller.service
```

To survive logout (start at boot, stop at shutdown):

```bash
sudo loginctl enable-linger "$USER"
```

Check status / logs:

```bash
systemctl --user status ctrlrelay-poller
journalctl --user -u ctrlrelay-poller -f
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

After a `ctrlrelay` package upgrade (`uv pip install -e .`), restart both.

After merging a PR that the dev pipeline opened, no manual restart is required —
the PR is referenced from the open branch in your bare repo, so cleanup is
handled by GitHub on merge. The worktree was already removed when the session
finished.

## Logs

By default, both daemons log to the paths configured in your launchd /
systemd unit files. The recommended location is `~/.ctrlrelay/logs/`.

Tail them:

```bash
tail -f ~/.ctrlrelay/logs/poller.log
tail -f ~/.ctrlrelay/logs/bridge.log
tail -f ~/.ctrlrelay/logs/poller.error.log
```

The poller prints one line per detected issue and one line per pipeline outcome.
The bridge prints connection events and Telegram API errors.

## Inspecting state

### `ctrlrelay status`

The fastest way to see what's happening:

```bash
ctrlrelay status
```

Shows held repo locks and the 5 most recent sessions with their statuses
(`running`, `done`, `blocked`, `failed`).

### State DB schema

The state DB is plain SQLite at `paths.state_db`. Open it with `sqlite3` for
ad-hoc queries:

```bash
sqlite3 ~/.ctrlrelay/state.db
sqlite> .tables
sqlite> .schema sessions
sqlite> SELECT id, pipeline, repo, status, started_at FROM sessions ORDER BY started_at DESC LIMIT 20;
```

Tables (defined in
[`src/ctrlrelay/core/state.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/state.py)):

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
`<owner>-<repo>-<session-id>`. `ctrlrelay` removes them on `DONE` or terminal
`FAILED`; `BLOCKED` sessions keep their worktree so a manual operator can
inspect / resume.

If something goes wrong and you need to reclaim a stuck worktree:

```bash
git -C ~/.ctrlrelay/repos/your-org-your-app.git worktree list
git -C ~/.ctrlrelay/repos/your-org-your-app.git worktree remove --force /path/to/orphan
```

### Poller seen-set

The poller's seen-issue state is a JSON file at
`<state_db_dir>/poller_state.json`. If you want to force the poller to
re-process a specific issue, delete its number from `seen_issues[<repo>]` (or
delete the file entirely to reseed from scratch on next start).
