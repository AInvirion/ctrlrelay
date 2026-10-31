---
title: Status page design
layout: default
nav_order: 100
nav_exclude: true
---

# Status page: see what ctrlrelay is doing without reading the log

Issue: #209. Date: 2026-10-30.

## Why

ctrlrelay runs on alkaturbo and works well, but the only way to know what it is
doing is the log, and the log is not readable by a person. Oscar wants to open a
page on his laptop and see, at a glance:

- which agent is working, on which repo and issue, for how long, and **what it is
  doing right now** (reading, coding, testing, running the review tool, opening the PR);
- what is waiting to go next, and why it is waiting;
- what already happened: dispatched, PR opened, merged, blocked, failed, and every
  message or alert ctrlrelay sent to chat.

Other agents (the TPM, other sessions) should be able to read the same thing as JSON.

## Goals

1. A read-only web page served from the machine ctrlrelay runs on, reachable from
   other machines on the home network.
2. The same data as JSON, for agents.
3. A real **phase** for each running agent, not just "running".
4. A **waiting line** that is visible, with the reason each item waits.
5. A **history feed** of what ctrlrelay did and said, with times.

## Non-goals

- Controlling anything. No buttons, no forms, no endpoints that change state.
- Login or accounts. Anyone on the home network may open it; the same information
  is visible to them on GitHub.
- Showing several machines. One page per ctrlrelay instance.
- Replacing the log or the chat notifications.

## Anti-goals (it fails if it does any of these, even if it "works")

- **It changes what the daemon does.** A slow or broken page must never slow,
  block or crash the poller or the bridge, and must never hold a write lock on the
  shared database. The only daemon changes are the best-effort event writes and WAL
  mode described below.
- **It is reachable from the internet.** Some repos are private.
- **It shows a confident wrong answer.** If it cannot read something (no transcript
  yet, database busy, log missing), it says "unknown", never a guessed phase or an
  empty list that looks like "nothing running".
- **It leaks secrets.** No tokens, no environment, no full prompts.

## What the page shows (layout B, chosen)

Two columns. Header: host, ctrlrelay version, time of last poll, and what the poller
is busy with right now.

**Left: the work**

- **Running now.** One card per running session: repo, issue number and title,
  pipeline (`dev`, `task` or `secops`; secops runs have no issue, so the card says
  "secops sweep" instead), elapsed time. A phase bar:
  `reading → coding → testing → CI → review → PR`, with the current step highlighted.
  Under it, the **last command** and **how long ago** it ran.
  The card is marked **looks stuck** only when the agent's transcript has had no new
  step for 15 minutes **and** ctrlrelay is not itself in a CI wait or review for that
  session (those are long and quiet by design).
- **Needs you.** Blocked sessions waiting for an answer: repo, issue, the question,
  how long it has waited, and, for a secops question held back (#214), when it will
  next be asked. Kept apart from the waiting line: these are questions, not work.
- **Waiting.** Assigned issues that ctrlrelay would pick up and has not started, in
  the order it will take them, each with its reason, e.g. "same repo as #215" or
  "poller busy with #215".
- **Today.** Counts: dispatched, PRs opened, merged, failed, blocked, messages sent.

**Right: live feed**

Newest first, with the time and a kind tag: dispatched, PR opened, merged,
blocked (with the question), failed (with exit code), message sent, alert sent,
question held back, CI wait started/finished, review started/finished, daemon
restart. Agent phase changes (reading, coding, testing) are **not** listed; they
would drown everything else.

The page refreshes itself. Nobody reloads by hand.

## Where each piece of data comes from

| What | Source | Exists today? |
|---|---|---|
| Running sessions, repo, issue, pipeline, start time, worktree | `sessions` table (`status = 'running'`) | yes |
| Repo busy | `repo_locks` table | yes |
| Blocked + question, held-back time | `sessions.blocked_question`, `pending_resumes` (incl. `held_until`) | yes |
| Failed + exit code | `sessions.claude_exit_code`, `dispatch_health` | yes |
| Agent phase and last command | the agent's own live transcript (below) | yes, unused |
| CI wait and review phase | ctrlrelay runs these itself after the agent exits (`pipelines/dev.py`, `core/code_review.py`), so they are **not** in the transcript; they come from new events (below) | **no, must be added** |
| PR opened, merged | new events written where the PR URL is first known and where `dev.pr.merged` is logged (`pipelines/post_merge.py`) | **no, must be added** |
| Messages and alerts sent | new events where the bridge sends (`bridge/server.py`, today only a plain log line) and where alerts are delivered | **no, must be added** |
| Issue title | GitHub, fetched by the web process and cached | **no** (nothing stores it today) |
| Waiting line | the web process asks GitHub itself (below) | **no, must be added** |

### Agent phase: read the agent's live transcript

Measured on alkaturbo on 2026-10-30 with the running #215 agent: the agent CLI
writes a JSON-lines transcript as it works, a few seconds after each step, at

```
~/.claude/projects/<worktree path with "/", "." and "_" replaced by "-">/<uuid>.jsonl
```

The session's `worktree_path` is in the `sessions` table, so the folder is
known (newest `.jsonl` in it). Each `assistant` row holds `tool_use` entries with the
tool name and its input (`Bash` command, `Edit`/`Write` file path, `Read`/`Grep`
pattern) and a timestamp. Phase is derived from the most recent tool uses:

- `Read`, `Grep`, `Glob`, read-only shell (`cat`, `sed -n`, `grep`, `ls`, `git log/diff/show`) → **reading**
- `Edit`, `Write`, `NotebookEdit` → **coding**
- shell running a test runner (`pytest`, `npm test`, `npx playwright`, `go test`, `cargo test`, `uv run pytest`, ...) → **testing**
- `git push`, `gh pr create` → **PR**

If no transcript is found, phase is **unknown**. The agents need no change for this.

### CI wait and review: ctrlrelay says when it starts and stops them

After the agent exits, the dev pipeline waits for CI and runs the review itself. It
writes an event when each starts and ends. While one is open for a session, that is
the session's phase, and the card is not "looks stuck".

### Waiting line: the web process asks GitHub (Oscar's choice)

The poller works one batch at a time and does not look at GitHub again until the
current run, its CI wait and its review are done, so it cannot say what is waiting.
Instead the web process, once a minute:

1. asks GitHub, with **one search call across all repos**, for open issues assigned
   to the configured user;
2. keeps only issues in configured repos that pass the **same eligibility rules the
   poller uses** (labels to include, require and exclude; reuse the poller's own
   functions, do not copy them);
3. drops issues that are already running, blocked, or done according to `sessions`;
4. orders the rest the way the poller would and gives each a reason.

It uses the `gh` login that ctrlrelay already uses on that machine, as the same
user. **It only reads from GitHub.** If the call fails, the waiting line shows
**unknown** with the time of the last good answer, never an empty list.

### Events: a small durable table, written best-effort

A new `events` table holds the feed. The poller, the pipelines and the bridge add a
row at the points listed in the table above. **Every write is best-effort**: wrapped
so that a failure (database busy, disk full) is logged and swallowed, with a short
timeout, and never changes, delays or fails the work it describes. Rows older than
the history window are pruned by the existing hourly sweep.

### Shared database: switch to WAL

`state.db` uses SQLite's default journal today, where a reader can block a writer.
The poller/bridge open it in **WAL mode** from now on, so the web process can read
while they write. The web process still uses read-only connections and short reads,
and shows **unknown** if a read fails.

## How it runs

- A new command, `ctrlrelay web` (name is the builder's call), running as **its own
  process and its own systemd user unit**, next to the poller and bridge. It never
  runs inside the poller or bridge, so it cannot stall them.
- It opens `state.db` **read-only**. It never writes anything, locally or to GitHub.
- It reads transcript files read-only, and only the tail it needs.
- Endpoints:
  - `GET /` the page (one HTML file, no build step, no external CDN)
  - `GET /api/status` everything on the page, as JSON
  - `GET /api/events?since=<ts>` the feed, as JSON
- Configured in `orchestrator.yaml` under a new `web:` block: `host`, `port`,
  history days. **Defaults: `127.0.0.1`, port `8787`, 7 days.** alkaturbo's config sets
  `host` to its LAN address. The default stays local so a fresh install never exposes
  it by accident.
- There is already a `dashboard:` config block and `dashboard/client.py`, which push a
  heartbeat to a remote dashboard. The poller passes `dashboard=None` today. This page
  does not use or replace it; the two stay separate.

## Constraints

- Python 3.12, same package, same style as the rest of `src/ctrlrelay`.
- Prefer no new runtime dependency. The page is small and read-only; the standard
  library HTTP server or an already-present dependency should do.
- The only daemon changes allowed are the best-effort event writes and WAL mode
  above. Nothing else in the poller, pipelines or bridge changes behaviour.
- JSON fields are stable and documented, because other agents will depend on them.
- Every timestamp in JSON is epoch seconds plus an ISO string in UTC.

## Decided by Oscar

- Read-only. No login. Home network only.
- Layout B (work on the left, live feed on the right).
- Show the phase (coding, testing, review, PR), not only "running".
- JSON for agents (not XML or RSS).
- History includes dispatched, delivered (PR opened), merged, and messages/alerts sent.
- The waiting line comes from the web process asking GitHub, using the existing `gh`
  login, read-only, rather than waiting for #210.

## My calls (Oscar can overrule)

- Agent phase comes from the agent's transcript, not from asking agents to report it.
- CI wait and review phases come from new events written by the pipeline.
- Events are written best-effort by the daemon; a failed write never affects the work.
- `state.db` moves to WAL mode.
- A "Needs you" list for blocked questions, separate from the waiting line.
- Separate process and unit, read-only database connection.
- Port 8787, 7 days of history, refresh every 5 seconds by polling `/api/status`,
  GitHub checked once a minute.
- "Looks stuck" after 15 minutes with no agent step and no CI wait or review open.
- Agent phase changes are not listed in the feed.

## Left to the builder

- Exact HTML and styling.
- The `events` table columns and which call sites write to it.
- How the waiting line is cached between GitHub checks.
- The exact command patterns that map to each phase, as long as unknown stays unknown.
- The exact eligibility reuse and ordering for the waiting line, as long as it matches the poller.
- How issue titles are fetched and cached.
