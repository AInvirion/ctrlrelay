---
title: Chat bridge
layout: default
nav_order: 4
description: "Set up the chat bridge — Telegram or Mattermost — that delivers BLOCKED questions to a human and routes their answers back to a paused Claude session."
permalink: /bridge/
---

# Chat bridge

The bridge is a small daemon that:

1. Listens on a Unix socket at the configured transport's `socket_path`.
2. Forwards messages from ctrlrelay to a chat channel.
3. Streams replies back from that channel and delivers them to the socket
   client that asked.

It speaks **Telegram** or **Mattermost**. Pipelines never know which: they
talk to the socket, and the chat app is the bridge's business. Switching is a
one-line config change and no pipeline code moves.

**One at a time.** `transport.type` names the transport that is active, and the
other block is inert — it may stay in the file, but nothing reads it, including
its `question_ttl_seconds`. There is no fallback: a question goes to the one
you named or nowhere. Running both at once is **not supported and not
planned** — it was designed in
[#176](https://github.com/AInvirion/ctrlrelay/issues/176) and closed
won't-do. Reopen that issue rather than re-deriving the design if you need
it.

| | Telegram | Mattermost |
|---|---|---|
| Needs | a bot from BotFather, your chat id | a self-hosted server, a bot account, a channel id |
| Outbound | Bot API `sendMessage` | `POST /api/v4/posts` |
| Inbound | `getUpdates` long-poll | WebSocket `posted` events |
| "answer this one" | the reply gesture | reply **in the question's thread** |
| Tappable choices | reply keyboard | rendered as a numbered list you type |

**The one operator-facing difference worth knowing.** When several questions
are outstanding, the bridge can only route your answer if it knows which
question you mean. On Telegram that is Telegram's reply; on Mattermost it is
**replying inside the question's thread**. A loose message in the channel is
only routable when exactly one question is waiting — otherwise the bridge
refuses and tells you what is outstanding, rather than guessing.

Pipelines use it as the human-in-the-loop channel: when Claude writes a
`BLOCKED_NEEDS_INPUT` checkpoint, the dev pipeline calls `transport.ask(question)`,
which travels socket → bridge → Telegram → user → Telegram → bridge → socket
and returns as a string back into the resume call.

The bridge is implemented in
[`src/ctrlrelay/bridge/`](https://github.com/AInvirion/ctrlrelay/tree/main/src/ctrlrelay/bridge).

## Prerequisites

- A Telegram account.
- A registered bot (next section).
- Your numeric chat ID (next section).
- The bridge socket directory must exist and be writable. Default is
  `~/.ctrlrelay/`.

## 1 — Create a bot via BotFather

1. Open Telegram and message [`@BotFather`](https://t.me/botfather).
2. Send `/newbot`.
3. Choose a display name (e.g. `ctrlrelay orchestrator`).
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

## 3 — Configure ctrlrelay

Set the bot token in your environment (the bridge reads it from the env var
named in `transport.telegram.bot_token_env`):

```bash
export CTRLRELAY_TELEGRAM_TOKEN="123456:ABCdef-your-real-token"
```

Update `config/orchestrator.yaml`:

```yaml
transport:
  type: "telegram"
  telegram:
    bot_token_env: "CTRLRELAY_TELEGRAM_TOKEN"
    chat_id: 987654321              # your numeric chat ID
    socket_path: "~/.ctrlrelay/ctrlrelay.sock"
```

Validate:

```bash
ctrlrelay config validate
```

## 4 — Start the bridge

Foreground (handy when wiring up for the first time — Ctrl+C to stop):

```bash
ctrlrelay bridge start
```

Background (writes a PID file alongside the socket):

```bash
ctrlrelay bridge start --daemon
```

Check it's alive:

```bash
ctrlrelay bridge status
```

Stop it:

```bash
ctrlrelay bridge stop
```

## Mattermost instead of Telegram

Steps 1–2 above are Telegram-specific. For Mattermost, do this instead and
then rejoin at step 4.

### M1 — Enable bot accounts

**System Console → Integrations → Bot Accounts → Enable Bot Account Creation
= true.**

On a self-hosted install this is off by default. Note that editing
`config.json` on the server may not be enough — if the instance has no config
watcher the value will not take effect until `systemctl restart mattermost`,
and the live value is what matters. Read it back before believing it:

```bash
curl -s "https://<your-server>/api/v4/config/client?format=old" \
  | jq .EnableBotAccountCreation
```

`EnableUserAccessTokens` is **not** required. That setting governs *user*
personal access tokens; bot account tokens are managed separately. Leaving it
off avoids letting every user mint long-lived full-access tokens.

### M2 — Create the bot and its token

1. **Integrations → Bot Accounts → Add Bot Account.**
2. Username `ctrlrelay`, role **Member**. Leave `post:all` and `post:channels`
   **off** — the bot will be added to one channel, and a token with no reach
   beyond it is a smaller problem if it leaks.
3. **Create New Token** and copy it immediately. Mattermost shows it once.

### M3 — A channel, and put the bot in it

Create a channel for orchestrator questions, then invite the bot:

```
/invite @ctrlrelay
```

**This is not optional, and the reason is easy to miss.** A bot without
`post:all` cannot post where it is not a member — and it does not receive the
reply events for such a channel either. Forget this and questions fail to post
*and* answers would never arrive. `ctrlrelay bridge start` checks membership
before it binds its socket and refuses to start until the bot is invited.

The bot must also be on the team. If `/invite` complains, add it via the
team's **Invite People** first.

### M4 — Find the channel id

The id, not the name: a name is only unique within a team and can be renamed
under you, while the id is stable. From the channel's **View Info**, or:

```bash
curl -s -H "Authorization: Bearer $CTRLRELAY_MATTERMOST_TOKEN" \
  "https://<your-server>/api/v4/teams/name/<team>/channels/name/<channel>" \
  | jq -r .id
```

### M5 — Configure

```bash
export CTRLRELAY_MATTERMOST_TOKEN="your-bot-token"
```

```yaml
transport:
  type: "mattermost"
  mattermost:
    url: "https://chat.example.com"   # scheme required
    bot_token_env: "CTRLRELAY_MATTERMOST_TOKEN"
    channel_id: "emzhur1hwpyc38ehkfm5ppym8y"
    socket_path: "~/.ctrlrelay/ctrlrelay.sock"
    ask_timeout_seconds: 900
```

Then `ctrlrelay config validate`, and continue from step 4 above —
`ctrlrelay bridge start` is the same command for either transport.

**Editions.** Everything here works on free self-hosted Mattermost. The REST
API, the WebSocket, bot accounts and bot tokens are core features, not
licensed ones; the paid tiers cover SSO/SAML/LDAP, compliance export and high
availability, none of which the bridge touches.

## 5 — Send a test message

Once the bridge is running and reachable on its socket, send a one-off message
through it:

```bash
ctrlrelay bridge test --message "hello from ctrlrelay"
```

You should see the message appear in your Telegram chat almost immediately. If
you don't, see [Troubleshooting](#troubleshooting).

## How it integrates with pipelines

When you run `ctrlrelay poller start` (or `run dev`) with `transport.type:
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
[`src/ctrlrelay/bridge/protocol.py`](https://github.com/AInvirion/ctrlrelay/blob/main/src/ctrlrelay/bridge/protocol.py).

| `op` | Direction | Purpose |
|---|---|---|
| `send` | client → bridge | Fire-and-forget message into Telegram. |
| `ask` | client → bridge | Question that expects a reply. Optional `options[]` renders as a Telegram keyboard. |
| `ack` | bridge → client | Acknowledges receipt of `send`/`ask`. |
| `answer` | bridge → client | Reply text from the Telegram user, returned to the original `ask` caller. |
| `ping` / `pong` | both | Liveness check. |
| `error` | bridge → client | Error envelope (`error` and `message` fields). |

You generally don't need to speak the protocol directly — use
`ctrlrelay.transports.SocketTransport` from Python or the `bridge` CLI commands.

## Troubleshooting

**"Bridge not running" when calling `bridge test`** — start the bridge first with
`ctrlrelay bridge start --daemon`. Confirm with `bridge status`.

**No reply arrives in Telegram** — check the bot token: `curl
https://api.telegram.org/bot<TOKEN>/getMe` should return your bot. If it returns
`401`, the token is wrong or the bot was deleted.

**Replies don't reach the pipeline** — make sure you're replying _in the same
chat_ as `chat_id` in your config. If you're using a group chat, replying via
Telegram's "reply" gesture (long-press → Reply) helps the bridge match your
answer to the right pending question.

**`PID file exists` on start** — a previous run died without cleaning up. Run
`ctrlrelay bridge stop` to clear the stale PID, then start again.

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
~/.ctrlrelay/ctrlrelay.sock`) and restart.

## Sequence: BLOCKED question round-trip

```
   pipeline                bridge               chat app          user
      │                      │                      │               │
      │── ask("Which?") ────>│                      │               │
      │                      │─ post question ─────>│               │
      │                      │                      │── push ──────>│
      │                      │                      │               │
      │                      │                      │<── reply ─────│
      │                      │<─ reply + post id ───│               │
      │<── answer("the b") ──│                      │               │
```

The bridge remembers the id of every question it posts, so the reply's
"this is what I am answering" — Telegram's `reply_to_message_id`, Mattermost's
thread `root_id` — names the session exactly. That is also how an answer
arriving *after* the pipeline stopped waiting still drives a resume, via the
`pending_resumes` table.

The pipeline's `transport.ask()` call blocks (with the configured timeout) until
the bridge returns the answer. The pipeline then resumes the Claude session via
`claude --resume <session_id>` with a prompt of the form "User answered: …".
