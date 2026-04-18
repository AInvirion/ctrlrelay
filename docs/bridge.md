---
title: Telegram bridge
layout: default
nav_order: 4
description: "Set up the Telegram bridge that delivers BLOCKED questions to a human and routes their answers back to a paused Claude session."
permalink: /bridge/
---

# Telegram bridge

The bridge is a small daemon that:

1. Listens on a Unix socket at `transport.telegram.socket_path`.
2. Forwards messages from dev-sync to a Telegram chat over the Bot API.
3. Long-polls Telegram for replies in that chat and delivers them back to the
   socket client that asked.

Pipelines use it as the human-in-the-loop channel: when Claude writes a
`BLOCKED_NEEDS_INPUT` checkpoint, the dev pipeline calls `transport.ask(question)`,
which travels socket → bridge → Telegram → user → Telegram → bridge → socket
and returns as a string back into the resume call.

The bridge is implemented in
[`src/dev_sync/bridge/`](https://github.com/AInvirion/dev-sync/tree/main/src/dev_sync/bridge).

## Prerequisites

- A Telegram account.
- A registered bot (next section).
- Your numeric chat ID (next section).
- The bridge socket directory must exist and be writable. Default is
  `~/.dev-sync/`.

## 1 — Create a bot via BotFather

1. Open Telegram and message [`@BotFather`](https://t.me/botfather).
2. Send `/newbot`.
3. Choose a display name (e.g. `dev-sync orchestrator`).
4. Choose a unique username ending in `bot` (e.g. `myorg_devsync_bot`).
5. BotFather replies with an **HTTP API token** that looks like
   `123456:ABCdef-...`. Save this — it's your bot token.

## 2 — Get your chat ID

1. Open the chat with your new bot and send any message (e.g. `hello`).
   Telegram won't deliver bot messages until the chat exists.
2. Hit the `getUpdates` endpoint with your token:

   ```bash
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates" | jq
   ```

3. Find the numeric `message.chat.id` field. That's your chat ID. For private
   chats it's a positive integer; for groups it's negative.

If you'd rather use a Telegram group, add the bot to the group and use the
group's chat ID instead.

## 3 — Configure dev-sync

Set the bot token in your environment (the bridge reads it from the env var
named in `transport.telegram.bot_token_env`):

```bash
export DEV_SYNC_TELEGRAM_TOKEN="123456:ABCdef-your-real-token"
```

Update `config/orchestrator.yaml`:

```yaml
transport:
  type: "telegram"
  telegram:
    bot_token_env: "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: 987654321              # your numeric chat ID
    socket_path: "~/.dev-sync/dev-sync.sock"
```

Validate:

```bash
dev-sync config validate
```

## 4 — Start the bridge

Foreground (handy when wiring up for the first time — Ctrl+C to stop):

```bash
dev-sync bridge start
```

Background (writes a PID file alongside the socket):

```bash
dev-sync bridge start --daemon
```

Check it's alive:

```bash
dev-sync bridge status
```

Stop it:

```bash
dev-sync bridge stop
```

## 5 — Send a test message

Once the bridge is running and reachable on its socket, send a one-off message
through it:

```bash
dev-sync bridge test --message "hello from dev-sync"
```

You should see the message appear in your Telegram chat almost immediately. If
you don't, see [Troubleshooting](#troubleshooting).

## How it integrates with pipelines

When you run `dev-sync poller start` (or `run dev`) with `transport.type:
telegram` configured, the pipeline auto-connects to the bridge socket if it
exists. Messages it sends:

- `🔔 New issue #123 in your-org/your-app: ...` — when the poller picks up an issue.
- `⏸️ Blocked on #123: ...` — Claude wrote a `BLOCKED_NEEDS_INPUT` checkpoint;
  the next reply you send becomes the answer.
- `✅ PR ready: ...` — pipeline finished green.
- `❌ Failed on #123: ...` — pipeline failed.

For the full BLOCKED → answer → resume mechanics, see
[Feedback loop]({{ '/feedback-loop/' | relative_url }}).

## Protocol

The bridge speaks newline-delimited JSON over the Unix socket. Defined in
[`src/dev_sync/bridge/protocol.py`](https://github.com/AInvirion/dev-sync/blob/main/src/dev_sync/bridge/protocol.py).

| `op` | Direction | Purpose |
|---|---|---|
| `send` | client → bridge | Fire-and-forget message into Telegram. |
| `ask` | client → bridge | Question that expects a reply. Optional `options[]` renders as a Telegram keyboard. |
| `ack` | bridge → client | Acknowledges receipt of `send`/`ask`. |
| `answer` | bridge → client | Reply text from the Telegram user, returned to the original `ask` caller. |
| `ping` / `pong` | both | Liveness check. |
| `error` | bridge → client | Error envelope (`error` and `message` fields). |

You generally don't need to speak the protocol directly — use
`dev_sync.transports.SocketTransport` from Python or the `bridge` CLI commands.

## Troubleshooting

**"Bridge not running" when calling `bridge test`** — start the bridge first with
`dev-sync bridge start --daemon`. Confirm with `bridge status`.

**No reply arrives in Telegram** — check the bot token: `curl
https://api.telegram.org/bot<TOKEN>/getMe` should return your bot. If it returns
`401`, the token is wrong or the bot was deleted.

**Replies don't reach the pipeline** — make sure you're replying _in the same
chat_ as `chat_id` in your config. If you're using a group chat, replying via
Telegram's "reply" gesture (long-press → Reply) helps the bridge match your
answer to the right pending question.

**`PID file exists` on start** — a previous run died without cleaning up. Run
`dev-sync bridge stop` to clear the stale PID, then start again.

**Rate limits** — Telegram caps individual chats at ~20 messages/minute. The
bridge does not implement client-side rate limiting; if you saturate the chat
you'll see HTTP 429 in the bridge logs and the affected `send`/`ask` calls
will fail. Slow your pipelines down or split notifications across chats.

**Bridge crashes when network is offline** — the bridge requires Telegram API
access. If the network is down at startup, the long-poll task will fail and the
process exits. Restart the bridge once connectivity is restored. (When run under
launchd / systemd with `KeepAlive`/`Restart=always`, this is automatic.)

**Socket exists but no process** — if `bridge status` reports "socket exists but
no running process", remove the orphan socket file (`rm
~/.dev-sync/dev-sync.sock`) and restart.

## Sequence: BLOCKED question round-trip

```
   pipeline                bridge                Telegram          user
      │                      │                      │               │
      │── ask("Which?") ────>│                      │               │
      │                      │── sendMessage ──────>│               │
      │                      │                      │── push ──────>│
      │                      │                      │               │
      │                      │                      │<── reply ─────│
      │                      │<── getUpdates ───────│               │
      │<── answer("the b") ──│                      │               │
```

The pipeline's `transport.ask()` call blocks (with the configured timeout) until
the bridge returns the answer. The pipeline then resumes the Claude session via
`claude --resume <session_id>` with a prompt of the form "User answered: …".
