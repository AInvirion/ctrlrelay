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

ctrlrelay generates platform-appropriate service unit files for you with
`ctrlrelay install`. The two daemons it manages:

| Service | Command | Why you want it running |
|---|---|---|
| Bridge | `ctrlrelay bridge start` | Delivers BLOCKED questions to Telegram and routes answers back. |
| Poller | `ctrlrelay poller start --interval 300` | Watches GitHub for newly assigned issues and runs the dev pipeline. |

Both should restart on failure and on login.

`ctrlrelay bridge start` and `ctrlrelay poller start` daemonize by default
(fork, write a PID file, return to the shell). Under a process supervisor
(launchd, systemd) pass `--foreground` so the supervisor can track the PID
and restart on failure — the templates below already do this.

### macOS — launchd

Recommended: let `ctrlrelay install launchd` render the plists for you.
It substitutes `${USER}`, `${HOME}`, the absolute `ctrlrelay` path,
your working directory, and (when exported) `CTRLRELAY_TELEGRAM_TOKEN`
into the in-package templates and writes them to
`~/Library/LaunchAgents/`.

```bash
export CTRLRELAY_TELEGRAM_TOKEN=your-bot-token
ctrlrelay install launchd \
  --workdir ~/Projects/ctrlrelay \
  --label-prefix com.yourname \
  --poller-interval 300

# Then load the agents:
mkdir -p ~/.ctrlrelay/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yourname.ctrlrelay-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yourname.ctrlrelay-poller.plist
```

Pick a reverse-DNS `--label-prefix` you own (e.g. `com.yourname`); it
flows into both the filename and the `<Label>` value so `launchctl
list` output is unambiguous. Pass `--dry-run` first to see what would
be written; pass `--force` to overwrite a previously installed plist.

If you'd rather hand-write the plists, here are the full templates the
installer uses.

`~/Library/LaunchAgents/com.example.ctrlrelay-bridge.plist`:

{% raw %}
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.ctrlrelay-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ctrlrelay</string>
        <string>bridge</string>
        <string>start</string>
        <string>--foreground</string>
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

`~/Library/LaunchAgents/com.example.ctrlrelay-poller.plist`:

{% raw %}
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.ctrlrelay-poller</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ctrlrelay</string>
        <string>poller</string>
        <string>start</string>
        <string>--foreground</string>
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
    <!-- Give the in-process scheduler time to drain a running secops
         sweep on stop (worktree cleanup can take ~120s). launchd's
         default ExitTimeOut is 20s, which would SIGKILL mid-cleanup. -->
    <key>ExitTimeOut</key>
    <integer>180</integer>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.ctrlrelay/logs/poller.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.ctrlrelay/logs/poller.error.log</string>
</dict>
</plist>
```
{% endraw %}

Whether you used `ctrlrelay install launchd` or hand-wrote the plists,
manage the agents with:

```bash
launchctl list | grep ctrlrelay          # check loaded
launchctl bootout gui/$(id -u)/com.example.ctrlrelay-poller    # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.ctrlrelay-poller.plist  # start
```

### Linux — systemd

Recommended: `ctrlrelay install systemd` renders the user unit files
for you (no root needed) and writes them to `~/.config/systemd/user/`.

```bash
export CTRLRELAY_TELEGRAM_TOKEN=your-bot-token
ctrlrelay install systemd \
  --workdir ~/Projects/ctrlrelay \
  --poller-interval 300

# Then enable and start:
mkdir -p ~/.ctrlrelay/logs
systemctl --user daemon-reload
systemctl --user enable --now ctrlrelay-bridge.service
systemctl --user enable --now ctrlrelay-poller.service
```

To survive logout (start at boot, stop at shutdown):

```bash
sudo loginctl enable-linger "$USER"
```

If you'd rather hand-write the units, here are the full templates the
installer uses. Save under `~/.config/systemd/user/`.

`~/.config/systemd/user/ctrlrelay-bridge.service`:

```ini
[Unit]
Description=ctrlrelay Telegram bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Projects/ctrlrelay
Environment=CTRLRELAY_TELEGRAM_TOKEN=your-bot-token-here
ExecStart=%h/.local/bin/ctrlrelay bridge start --foreground
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
ExecStart=%h/.local/bin/ctrlrelay poller start --foreground --interval 300
Restart=always
RestartSec=5
# Give the in-process scheduler time to drain a running secops sweep on
# stop (worktree cleanup can take ~120s). systemd's default
# TimeoutStopSec is 90s, which would SIGKILL the poller mid-cleanup and
# leak git worktree admin state.
TimeoutStopSec=180
StandardOutput=append:%h/.ctrlrelay/logs/poller.log
StandardError=append:%h/.ctrlrelay/logs/poller.error.log

[Install]
WantedBy=default.target
```

Once written, enable and start with the same `systemctl --user enable
--now` commands shown earlier in the section. Check status / logs:

```bash
systemctl --user status ctrlrelay-poller
journalctl --user -u ctrlrelay-poller -f
```

## Scheduled jobs

The poller daemon also hosts an in-process cron (APScheduler). The jobs run
inside the same asyncio loop as the issue poll, so they inherit the poller's
supervision (launchd `KeepAlive` on macOS, systemd `Restart=always` on Linux)
without needing a separate `.timer` unit.

| Job | Default schedule | What it does |
|---|---|---|
| `secops` | `0 6 * * *` (6am daily) | Runs the secops pipeline across every configured repo — equivalent to `ctrlrelay run secops`. |

Schedules are configured under `schedules:` in `orchestrator.yaml`, using
standard 5-field cron expressions. The top-level `timezone:` controls how
they're interpreted:

```yaml
timezone: "America/Santiago"
schedules:
  secops_cron: "0 6 * * *"   # daily 6am; use "0 6 * * 1" for weekly (Mondays)
```

An invalid cron expression fails at config load time rather than silently
disabling the job. If the machine is asleep at the fire time, the job runs
when it wakes — up to one hour late; older misfires are coalesced into a
single run.

On poller stop, the scheduler waits up to **150 seconds** for an in-flight
job (e.g. `git worktree prune` cleanup at the end of a secops sweep) to
finish before letting the asyncio loop close. **This requires your
supervisor's stop timeout to be at least that generous — the example
unit files above set `ExitTimeOut=180` (launchd) and
`TimeoutStopSec=180` (systemd).** Without those, the platform default
(launchd 20s, systemd 90s) SIGKILLs the daemon before cleanup finishes
and leaves stale `git worktree` admin state behind. If you're upgrading
from an older plist/unit that didn't set these, add them and reload.

## When to restart

| You changed... | Restart... |
|---|---|
| `transport.*` (Telegram token, chat ID, socket path) | bridge **and** poller |
| `repos[]` (added/removed/renamed) | poller |
| `claude.*` (binary path, timeout) | poller |
| `paths.*` | both |
| Anything in `dashboard.*` | poller |
| `schedules.*` (cron expressions) | poller |
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
