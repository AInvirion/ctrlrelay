---
title: Architecture
layout: default
nav_order: 8
description: "Layer diagram, dispatcher / Claude interaction, state-DB shape, and worktree lifecycle for contributors."
permalink: /architecture/
---

# Architecture

This page is for people working _on_ ctrlrelay, not just _with_ it. For an
operator-level view of the BLOCKED protocol see
[Feedback loop]({{ '/feedback-loop/' | relative_url }}).

## Layered overview

```
┌────────────────────────────────────────────────────────────────────────┐
│  CLI / daemons                                                         │
│  src/ctrlrelay/cli.py        (Typer app, subcommand groups)             │
│  src/ctrlrelay/bridge/__main__.py (bridge daemon entry point)           │
└──────────────┬─────────────────────────┬───────────────────────────────┘
               │                         │
               ▼                         ▼
┌──────────────────────────┐    ┌──────────────────────────┐
│  Pipelines               │    │  Bridge                  │
│  src/ctrlrelay/pipelines/ │    │  src/ctrlrelay/bridge/    │
│   - dev.py               │    │   - server.py            │
│   - secops.py            │    │   - protocol.py          │
│   - post_merge.py        │    │   - telegram_handler.py  │
│   - base.py              │    └──────────────────────────┘
└──────────────┬───────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Core                                                                  │
│  src/ctrlrelay/core/                                                    │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐           │
│  │ Dispatcher │ │ Worktree   │ │ GitHub CLI │ │ Checkpoint │           │
│  │ (claude -p)│ │ Manager    │ │ wrapper    │ │ protocol   │           │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘           │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐           │
│  │ StateDB    │ │ Poller     │ │ PRWatcher  │ │ Config     │           │
│  │ (SQLite)   │ │            │ │            │ │ (pydantic) │           │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘           │
└────────────────────────────────────────────────────────────────────────┘
                                     ▲
┌────────────────────────────────────┴───────────────────────────────────┐
│  Transports (pluggable)            Dashboard (optional, push-only)      │
│  src/ctrlrelay/transports/          src/ctrlrelay/dashboard/              │
│   - SocketTransport (telegram)      - DashboardClient                  │
│   - FileMockTransport (tests)                                           │
└─────────────────────────────────────────────────────────────────────────┘
```

The CLI is a thin shell over async functions in `pipelines/` and `core/`. The
bridge is a separate daemon that pipelines reach over a Unix socket — it is
deliberately decoupled so it can be restarted without affecting in-flight
pipeline work.

## Dispatcher ↔ Claude interaction

`ClaudeDispatcher` (in
[`src/ctrlrelay/core/dispatcher.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/dispatcher.py))
is the only place ctrlrelay calls out to Claude Code.

- **Spawn shape:** `claude -p <prompt> --output-format json --dangerously-skip-permissions [--resume <session_id>]`.
- **Working directory:** the session's worktree (so file edits land on the
  right branch).
- **Environment additions:** `CTRLRELAY_SESSION_ID` and `CTRLRELAY_STATE_FILE`
  are the only contract between dispatcher and child. Anything else the agent
  needs (issue body, PR rules, etc.) goes into the prompt.
- **Result:** `SessionResult(session_id, exit_code, state, stdout, stderr)`.
  `state` is the parsed `CheckpointState` if a state file was written, else
  `None`. The wrapper exposes `.success`, `.blocked`, `.failed` properties for
  pattern-matching pipelines.
- **Resume:** `resume_session_id` translates to `--resume <id>`, which has
  Claude rejoin the same conversation history. ctrlrelay sets the resume id to
  the same session id it spawned with, so a single dev-pipeline session can
  span many `BLOCKED → answer → resume` round-trips.

The dispatcher does NOT proxy or wrap the Claude session — it shells out the
same way you'd shell out to `gh` or `git`. This is deliberate.

## State DB shape

Schema in
[`src/ctrlrelay/core/state.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/state.py).
SQLite, single file at `paths.state_db`.

| Table | Columns | Notes |
|---|---|---|
| `sessions` | `id`, `pipeline`, `repo`, `issue_number`, `worktree_path`, `status`, `blocked_question`, `started_at`, `ended_at`, `claude_exit_code`, `summary` | Indexed on `repo` and `status`. One row per pipeline run. |
| `repo_locks` | `repo` (PK), `session_id`, `acquired_at` | Acquired with `acquire_lock`, released in the pipeline's `finally`. |
| `github_cursor` | `repo`, `last_checked_at`, `last_seen_issue_update` | Bounds GitHub API calls in the poller. |
| `telegram_pending` | `request_id`, `session_id`, `question`, `asked_at`, `answered_at`, `answer` | Outstanding bridge questions. |
| `automation_decisions` | `id`, `repo`, `operation`, `policy`, `item_id`, `decision`, `decided_by`, `decided_at`, `context` | Audit trail for `ask`-policy decisions. |

Methods on `StateDB`: `acquire_lock`, `release_lock`, `get_lock_holder`,
`list_locks`, plus raw `execute` / `commit`. The pipeline code uses raw SQL
for the `sessions` table on purpose — kept thin and grep-able.

## Worktree lifecycle

`WorktreeManager` (in
[`src/ctrlrelay/core/worktree.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/worktree.py))
is a thin wrapper over `git worktree`. Conventions:

- **Bare repo path:** `<paths.bare_repos>/<owner>-<repo>.git`. Cloned on first
  use, fetched on each subsequent use.
- **Worktree path:** `<paths.worktrees>/<owner>-<repo>-<session_id>`.
- **Branch:** for the dev pipeline, derived from `dev_branch_template` with
  `{n}` → issue number. Default `fix/issue-{n}`.

Lifecycle in `run_dev_issue`:

```
ensure_bare_repo(repo)
branch_preexisted = await branch_exists_locally(repo, branch)
worktree_path = create_worktree_with_new_branch(repo, session_id, branch)
... claude session runs ...
on DONE:    remove_worktree (keep branch — open PR refs it)
on BLOCKED: keep both (operator may resume)
on FAILED:  remove_worktree
            if not branch_preexisted and not on remote: delete_branch
```

The "preexisted" guard prevents a retry from clobbering a branch that already
holds an open PR from a previous successful session.

## PR verifier and post-merge

After a `DONE`-with-`pr_url` checkpoint, the dev pipeline runs
[`PRVerifier`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/pr_verifier.py)
to confirm the PR is mergeable and CI is green. If not, it builds a fix prompt
(merge conflicts → "rebase and resolve", failing checks → "investigate and
fix"), resumes the same Claude session with that prompt, and re-verifies. Up
to `DEFAULT_MAX_FIX_ATTEMPTS` (3) attempts.

`PRWatcher` (in
[`src/ctrlrelay/core/pr_watcher.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/pr_watcher.py))
polls a PR for merge status; the post-merge handler in
[`src/ctrlrelay/pipelines/post_merge.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/pipelines/post_merge.py)
closes the originating issue and notifies the operator once the PR lands.
These are not yet wired into the standard dev-pipeline flow; they're available
for future use.

## Bridge architecture

The bridge daemon (`ctrlrelay/bridge/server.py`) listens on a Unix socket
(mode `0o600` — owner-only). It speaks newline-delimited JSON, defined in
`bridge/protocol.py`:

```
client                bridge                 telegram_handler         Telegram
  │── ASK(q) ────────>│                          │                       │
  │                   │── send(q) ──────────────>│── sendMessage ───────>│
  │                   │                          │                       │
  │                   │  (long-poll loop)        │                       │
  │                   │<── handle(text, reply_to_id) ─                   │
  │<── ANSWER(text) ──│                          │                       │
```

Matching reply → pending question:

1. If the Telegram message has `reply_to_message_id` and that ID matches a
   pending request, route to that request.
2. Otherwise, deliver to the oldest pending request (FIFO).

Multiple in-flight asks are supported. The bridge keeps a queue of pending
asks per writer-socket and retains the original socket so the answer routes
back to the correct caller.

Bridge restart safety: pending in-memory state is lost on bridge restart.
Pipelines that were waiting on `transport.ask` will time out (default 300s)
and the session will be marked failed-after-blocked. Restart the bridge
**before** restarting the poller to minimise this window.

## Why this shape

A few decisions worth flagging:

- **Local-first, file-based protocol.** Everything between ctrlrelay and Claude
  is a file under the worktree. No long-lived shared memory, no socket
  contract with the agent. Survives orchestrator restart trivially.
- **Subprocess-not-library.** `claude` is invoked as a subprocess. ctrlrelay
  has no Python coupling to Claude Code's internals — it exchanges a prompt
  in and a JSON file out. Versioning is decoupled.
- **Per-repo serial, parallel across repos.** Repo locks in SQLite enforce
  this. A failed lock acquisition fails the session immediately rather than
  queueing.
- **No background event loop.** Each CLI command spins up its own
  `asyncio.run(...)`. The poller and bridge each own one event loop while they
  run. Simpler to reason about than a long-lived shared loop.
- **Bridge as separate process.** Restartable independent of pipelines. The
  cost is the small in-memory pending-asks state that does not survive a
  bridge restart.

## Where to read next

- [`src/ctrlrelay/core/config.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/config.py)
  — pydantic schema, the source of truth for the YAML.
- [`src/ctrlrelay/pipelines/dev.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/pipelines/dev.py)
  — full dev-pipeline orchestration including BLOCKED loop and PR-fix loop.
- [`src/ctrlrelay/core/dispatcher.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/dispatcher.py)
  — the only place we call `claude`.
- [`tests/`](https://github.com/AInvirion/ctrlrelay/tree/main/tests) — covers
  every public surface; treat tests as executable specifications.
