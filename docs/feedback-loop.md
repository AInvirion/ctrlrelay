---
title: Feedback loop
layout: default
nav_order: 5
description: "How ctrlrelay sessions checkpoint, block on questions, and resume after a human answer."
permalink: /feedback-loop/
---

# Feedback loop

Every Claude session that ctrlrelay spawns ends with a single JSON checkpoint
file. The orchestrator reads that file, decides what to do, and (if the session
asked a question) routes a human's answer back to a resumed session.

This page is the operator-level explanation of the protocol — what the file
looks like, how `BLOCKED` questions reach you, and how the answer becomes a
resume prompt.

## The checkpoint file

When ctrlrelay spawns Claude, it sets two environment variables in the child
process:

| Variable | Value |
|---|---|
| `CTRLRELAY_SESSION_ID` | UUID-tagged ID like `dev-your-org-your-app-42-a3f9cdfe`. |
| `CTRLRELAY_STATE_FILE` | Absolute path to the per-session JSON state file (typically `<worktree>/.ctrlrelay/state.json`). |

Before exiting, the agent writes one of three statuses to that path. The
schema is enforced by pydantic in
[`src/ctrlrelay/core/checkpoint.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/core/checkpoint.py).

### DONE

```json
{
  "version": "1",
  "status": "DONE",
  "session_id": "dev-your-org-your-app-42-a3f9cdfe",
  "timestamp": "2026-04-18T10:15:30Z",
  "summary": "PR opened",
  "outputs": {
    "pr_url": "https://github.com/your-org/your-app/pull/57",
    "pr_number": 57
  }
}
```

`outputs` is free-form. The dev pipeline expects `pr_url` and `pr_number` —
without those, the post-handoff PR-verification loop is skipped.

### BLOCKED_NEEDS_INPUT

```json
{
  "version": "1",
  "status": "BLOCKED_NEEDS_INPUT",
  "session_id": "dev-your-org-your-app-42-a3f9cdfe",
  "timestamp": "2026-04-18T10:18:02Z",
  "question": "Should I delete the deprecated /v1 endpoints, or only mark them as deprecated?",
  "question_context": { "issue": 42 }
}
```

`question` is required when `status` is `BLOCKED_NEEDS_INPUT`.
`question_context` is optional structured metadata — callers can pass anything
useful for the human (e.g. file paths the question refers to).

### FAILED

```json
{
  "version": "1",
  "status": "FAILED",
  "session_id": "dev-your-org-your-app-42-a3f9cdfe",
  "timestamp": "2026-04-18T10:21:55Z",
  "error": "git push failed: branch protection requires PR approval",
  "recoverable": true
}
```

`error` is required. `recoverable` defaults to `true` — set `false` to signal a
hard failure that retries should not attempt.

## End-to-end BLOCKED → answer → resume

Here is the full flow when an issue triggers a dev-pipeline run that ends up
asking the operator a question. The pipeline lives in
[`src/ctrlrelay/pipelines/dev.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/pipelines/dev.py);
the resume mechanism uses Claude Code's native `--resume <session_id>`.

```
  GitHub          Poller         Pipeline         Claude        Bridge        Telegram        Operator
    │               │                │              │              │             │                │
1)  │── new issue ─>│                │              │              │             │                │
2)  │               │── handle ─────>│              │              │             │                │
3)  │               │                │── lock+wt ───┤              │             │                │
4)  │               │                │── claude -p ─>│              │             │                │
5)  │               │                │              │── work ──────┤              │                │
6)  │               │                │              │── BLOCKED ───┤              │                │
                          (writes /worktree/.ctrlrelay/state.json)
7)  │               │                │<── checkpoint │              │             │                │
8)  │               │                │── ask(q) ────────────────────>│             │                │
9)  │               │                │              │              │── send ────>│                │
10) │               │                │              │              │             │── push ──────>│
11) │               │                │              │              │             │<── reply ─────│
12) │               │                │              │              │<── poll ────│                │
13) │               │                │<── answer ───────────────────│             │                │
14) │               │                │── claude --resume ─>│        │             │                │
                          (prompt = "User answered: <text>. Continue.")
15) │               │                │              │── work ──────┤              │                │
16) │               │                │              │── DONE (PR) ─┤              │                │
17) │               │                │<── checkpoint│              │             │                │
18) │               │                │── verify CI + merge ─...                                    │
19) │               │                │── send "✅ PR ready" ────────>│── post ───>│                │
```

Step-by-step:

1. **Issue picked up.** The poller (`src/ctrlrelay/core/poller.py`) lists issues
   assigned to your GitHub username across configured repos and surfaces ones
   it has not seen before.
2. **Handler invoked.** The poller's CLI handler calls `run_dev_issue(...)` from
   `src/ctrlrelay/pipelines/dev.py`.
3. **Lock + worktree.** The pipeline acquires a per-repo lock from the SQLite
   state DB and creates a worktree on a new branch (default
   `fix/issue-{n}`).
4. **Claude spawned.** `ClaudeDispatcher.spawn_session` runs
   `claude -p <prompt> --output-format json --dangerously-skip-permissions`
   inside the worktree, with `CTRLRELAY_SESSION_ID` and `CTRLRELAY_STATE_FILE`
   set.
5–6. **Claude works, hits a question.** The agent writes a
   `BLOCKED_NEEDS_INPUT` checkpoint and exits.
7. **Pipeline reads checkpoint.** It sees `blocked=True` and a `question`.
8. **Bridge ask.** The pipeline calls `transport.ask(question)`, which sends an
   `ASK` op over the Unix socket to the bridge.
9. **Bridge → Telegram.** The bridge posts the question into the configured
   Telegram chat.
10. **Push to operator.** Telegram notifies the operator on their phone or
    desktop client.
11. **Operator replies** with free-form text (or taps a keyboard button if the
    question included `options`).
12. **Bridge polls Telegram.** The bridge's long-poll loop sees the new message
    and matches it to the oldest pending question (preferring matches by
    `reply_to_message_id` if the user used "reply").
13. **Answer returned.** The bridge writes an `ANSWER` op back to the original
    socket client. The pipeline's `transport.ask` call returns the answer text.
14. **Resume Claude.** `pipeline.resume(ctx, answer)` re-spawns Claude with
    `--resume <session_id>` and the literal prompt
    `"User answered: <text>\n\nContinue from where you left off."`.
15–17. **Loop until terminal.** If the resumed session blocks again, steps
    8–14 repeat (capped at `DEFAULT_MAX_BLOCKED_ROUNDS`, currently 5). On
    `DONE`/`FAILED`, the loop exits.
18. **PR verification.** If the dev pipeline got `DONE` with a `pr_url`, it
    verifies the PR is mergeable and CI is green. If not, it asks Claude to
    fix and re-verifies (up to `DEFAULT_MAX_FIX_ATTEMPTS`, currently 3).
19. **Operator notified.** The poller pushes a final `✅ PR ready: <url>` (or
    `❌ Failed: …`) message via the bridge.

## Things to know

- **The bridge must be running** for the answer route to work. If the
  transport call raises (no socket, timeout, etc.), the pipeline gives up
  cleanly and reports the session as failed rather than hanging.
- **Default per-question timeout is 300 seconds** (`Transport.ask`'s default).
  If you need more time, the pipeline will give up on _that_ round and the
  session ends as failed-after-blocked.
- **Maximum 5 BLOCKED rounds per session.** This bounds the chat noise from a
  pathological loop. After 5 unanswered/timed-out rounds the pipeline gives up.
- **State is durable.** The checkpoint file and the SQLite state DB persist
  across orchestrator restarts. If you kill `ctrlrelay` mid-session, the state
  is still on disk; resuming it is currently a manual operation (re-run the
  pipeline against the same issue).
- **`file_mock` is one-way.** The file-mock transport supports outbound
  notifications but does not implement the answer-on-reply round-trip used by
  Telegram. Use `file_mock` for tests only.

## Implementing your own BLOCKED in a Claude session

If you write a custom prompt or skill that runs under ctrlrelay, signal a
question from inside the Claude session using a shell `printf`:

```bash
# Inside a Claude session — both vars are set by the dispatcher.
printf '{"version":"1","status":"BLOCKED_NEEDS_INPUT","session_id":"%s","timestamp":"%s","question":"%s"}' \
  "$CTRLRELAY_SESSION_ID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "Should I migrate the schema in this PR or split it?" \
  > "$CTRLRELAY_STATE_FILE"
exit 0
```

The dev-pipeline prompt template embeds this exact pattern — see the
`_build_prompt` method in
[`src/ctrlrelay/pipelines/dev.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/pipelines/dev.py).
