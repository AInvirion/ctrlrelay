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
| Status page (optional) | `ctrlrelay web start` | Read-only page and JSON API of what ctrlrelay is doing. See [Status page](#status-page). |

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

## Status page

`ctrlrelay web start` serves a read-only page and JSON API showing what each
agent is doing (phase, current activity, last command), what is waiting and
why, the questions that need an answer, and a feed of what ctrlrelay did and
said. It is a third process next to the bridge and the poller, so a slow or
broken page can never stall them.

On a running card, **phase** (the bar) is the furthest step the session has
reached, from the agent's transcript plus ctrlrelay's own CI, review and PR
events, so it never moves back. **Current activity** is the most recent step:
an open CI wait or review if it started after the agent's latest step, else
that step. It can be earlier than the bar, for example a read after the PR
or a fix round after review. The card links the PR once ctrlrelay has
recorded its number.

It is **read-only**: it opens `state.db` read-only, reads the tail of each
agent's transcript, and asks GitHub (through the `gh` login ctrlrelay already
uses) for the waiting line once a minute. It writes nothing but its PID file
(`web.pid`, next to `state.db`), and serves `GET` only; any other method is
`405`. It has **no login** and shows repository names, issue titles and the
questions agents asked, so **never expose it to the internet**. Keep it on
loopback or on the home network.

| Path | What |
|---|---|
| `/` | The page. Refreshes itself every `web.refresh_seconds`. |
| `/api/status` | Everything on the page, as JSON (the contract is in `src/ctrlrelay/web/status.py`). A section that could not be read is `null` and `errors` says why; `[]` means "read, and empty". The feed holds at most the newest `feed_limit` (200) events: `feed_total` is how many the history window holds (`null` if unreadable) and `feed_truncated` is `true` when some were left out, in which case the page says "showing newest N of M". |
| `/api/events?since=<epoch seconds>` | The feed, newest first, at most `limit` (200) events. Without `since`, the last `web.history_days` days. Carries the same `feed_total` and `feed_truncated`, plus the `limit` used. |

The page header shows how long ago it last heard from the server ("updated
12s ago", ticking every second) and a dot that is green while the last good
answer is at most two refresh intervals old and red otherwise. Elapsed times
on cards tick between polls too.

Fields added in #232 (all in `/api/status`):

| Field | What |
|---|---|
| `url` | On every `running`, `needs_you`, `waiting.items`, `skipped` and `feed` item (and on `/api/events` events): a link to the GitHub issue, or for a feed event that names a PR by number, the PR. Built only as `https://github.com/<owner>/<repo>/issues/<n>` or `/pull/<n>`, from a repo matching `owner/name` and an integer; `null` otherwise (a secops run with no issue, a restart). A `pr_url` an agent reported is never used. |
| `refs` | On every `needs_you` item (#241): one `{"number", "url"}` per distinct `#N` in the question, in order, at most 20, with `url` built the same way as `url` above from the item's repo and N. A `#N` right after a letter, digit, `_`, `/` or `&` is not counted. The page turns each listed `#N` in the question into a link, so a secops question's PRs are clickable. `[]` when there are none. |
| `skipped` | Every open issue assigned to you that the poller will **not** pick up and is not running, from the same search as the waiting line (no extra GitHub calls): `repo`, `issue_number`, `title`, `url`, `updated`/`updated_ts` and `reason`, one of `repo not in config`, `excluded label <label>`, `already handled once (won't retry)`, `missing required label <label>` (several joined with "or": any one admits it), `assigned by someone else`. `null` until GitHub has answered once; `waiting.status` and `waiting.last_good_ts` say how current it is. The page shows it collapsed, with a count. |
| `server` | This machine's health, read on each request: `load_1m`, `load_5m`, `load_15m`, `cpu_count`, `memory` (`total_bytes`, `available_bytes`, `available_pct`, `low`), `disks` (one each for `home` and `worktrees`: `name`, `path`, `total_bytes`, `free_bytes`, `free_pct`, `low`), `uptime_s`, and `unavailable` (why any number is `null`; memory and uptime are `null` off Linux). `low` is true below 10% free and the page marks it. The whole section is `null`, with a line in `errors`, only if reading it failed outright. |

Try it on loopback first (the default `web.host` is `127.0.0.1`, port `8787`):

```bash
ctrlrelay web start
ctrlrelay web status
curl -s http://127.0.0.1:8787/api/status | python3 -m json.tool | head -40
curl -s "http://127.0.0.1:8787/api/events?since=$(( $(date +%s) - 3600 ))"
ctrlrelay web stop
```

To open it from another machine on the home network, set `web.host` in
`orchestrator.yaml` to this machine's LAN address (see
[web]({{ '/configuration/' | relative_url }}#web)), then run it under
systemd. `ctrlrelay install systemd` renders `ctrlrelay-web.service` with the
other two units. It refuses to overwrite units that already exist, so on a
host where the bridge and poller are installed, render into a scratch
directory and copy only the web unit:

```bash
ctrlrelay install systemd --workdir ~/Projects/ctrlrelay --target-dir ~/ctrlrelay-units
cp ~/ctrlrelay-units/ctrlrelay-web.service ~/.config/systemd/user/
rm -r ~/ctrlrelay-units   # the other two carry the chat token
systemctl --user daemon-reload
systemctl --user enable --now ctrlrelay-web.service
journalctl --user -u ctrlrelay-web -n 20
tail -n 20 ~/.ctrlrelay/logs/web.log
```

The first line in `web.log` is a `web.started` event naming the address it
bound. If `web.host` is `0.0.0.0` it also logs a warning: that listens on
every interface, which is rarely what you want.

The page answers only to names it knows: `localhost`, `127.0.0.1`, `[::1]`,
`web.host`, and this machine's own hostname (plus `.local`). A request whose
`Host` header carries any other name gets `421 misdirected request`, and the
refused name is logged once as `web.misdirected_host`. So if you open it from
another machine by a name that is none of those, for example a DNS name your
router hands out, add that name to `web.allowed_hosts` and restart the unit:

```yaml
web:
  host: "192.168.1.20"
  allowed_hosts: ["mybox.lan"]
```

`grep misdirected_host ~/.ctrlrelay/logs/web.log` shows any name that was
refused, which is the quickest way to find the one to add.

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
tail -f ~/.ctrlrelay/logs/web.log      # if the status page is installed
```

The poller prints one line per detected issue and one line per pipeline outcome.
The bridge prints connection events and Telegram API errors.

### Tracing one agent session

Log lines are newline-delimited JSON, and the dispatcher — the module that
spawns and supervises every agent subprocess — tags each of its events with
the session id, so one grep reconstructs a whole run:

```bash
grep dev-owner-repo-42- ~/.ctrlrelay/logs/poller.log | jq -c '{ts, level, event}'
```

The events it emits:

| Event | Level | When |
|---|---|---|
| `dispatcher.session.start` | INFO | Subprocess about to launch. Carries `binary`, `timeout`, `resume`, `prompt_hash`, `prompt_len`. |
| `dispatcher.session.finished` | INFO | Subprocess exited. Carries `exit_code`, `checkpoint_status`, `elapsed_ms`. |
| `dispatcher.session.subprocess_failed` | ERROR | Non-zero exit, with `stderr_tail`. |
| `dispatcher.session.timeout` | ERROR | The `timeout` seconds elapsed and the child was killed. |
| `dispatcher.session.spawn_failed` | ERROR | The binary could not be exec'd at all. |
| `dispatcher.session.cancelled` | INFO | Daemon shutdown killed the child mid-run. |
| `dispatcher.checkpoint.missing` | WARNING | The session ended without writing a checkpoint. |
| `dispatcher.checkpoint.read_failed` | ERROR | A checkpoint file existed but would not parse. |
| `dispatcher.binary.fallback` | WARNING | `claude` was not on `PATH`; a well-known path was used instead. Expect this when the unit's `PATH` is minimal. |
| `dispatcher.binary.unresolved` | WARNING | Not on `PATH` and not at any known path — sessions are about to start failing to spawn. |

Prompts and agent output never appear in plaintext: only a short SHA-256
prefix and a length are logged.

### Blocked-question events

When a session blocks on a question, the transport and the bridge each emit
their own record:

| Event | When |
|---|---|
| `dev.question.posted` | The question reached the operator. The transport emits it on the bridge's ACK; the bridge emits it once the Telegram API has accepted the message, and adds `telegram_msg_id`. |
| `dev.question.post_failed` | Delivery failed. Carries `reason` (`send_failed`, `bridge_error`, or the exception class on the bridge side) and a truncated `error`. |
| `dev.answer.received` | The operator's reply came back. |

Neither the question nor the answer is written to the log. Each event carries
`question_hash` / `question_length` (or `answer_hash` / `answer_length`)
instead — the hash is stable, so it still joins the poller's record to the
bridge's record for the same question:

```bash
grep 44509098fa6eeaf3 ~/.ctrlrelay/logs/*.log | jq -c '{ts, logger, event}'
```

This matters because these files and the systemd journal have no retention
policy tuned for human-entered content, and operator replies are free text.

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

A run that fails **before the agent starts** (reading the issue, posting
the claim comment, cloning, cutting the worktree) does not use the issue
up. The issue is un-marked and tried again on the next poll, up to 3 runs
in all, with a chat message each time. After the third it stays in
`seen_issues` and the message says so; fix the cause, then delete the
number as above. The count is kept in `pre_dispatch_failures` in the same
file, so a restart does not reset it. Each retry posts a fresh claim
comment and, when it fails, a fresh "nothing is working on this" comment
on the issue.

### A repo whose history was replaced

If the local copy's default branch can no longer fast-forward to GitHub's
(the repo was replaced by a new one with the same name, or its default
branch was force-pushed), ctrlrelay moves the copy to
`~/.ctrlrelay/repos/<owner>-<repo>.git.replaced-<UTC time>`, clones again,
and says so in chat. Nothing is deleted: remove the old copy once you do
not need it. This is refused while any worktree still uses the copy (a
blocked session keeps its worktree for the resume); the run then fails
with `default branch diverged ... not re-cloning while N worktree(s) still
use it`, and you can finish or clean up that session first.
