---
title: Adding a machine
layout: default
nav_order: 11
description: "Run ctrlrelay on a second machine, on the same GitHub account and repositories, without the two taking the same work."
permalink: /adding-a-machine/
---

# Adding a machine

Several machines can run ctrlrelay on the same GitHub account and the same
repositories. Each one takes issues the others have not claimed, each
scheduled sweep runs once across them, and any machine's status page shows
all of them. This page sets up a second machine, `node-b`, next to a running
`node-a`.

Read [Operations: several machines on one account]({{ '/operations/' | relative_url }}#several-machines-on-one-account)
for how the claims work.

## 1. Install

Install ctrlrelay the same way as on the first machine
([Getting started]({{ '/getting-started/' | relative_url }})), the **same
version** on every machine. A machine on a version from before the
multi-machine work reads every failure comment as freeing every claim and
posts claims with no machine name.

## 2. Its own GitHub login

Log `gh` in on this machine with its own token:

```bash
gh auth login
```

Use the same GitHub **account** as the other machines (claims only count when
they come from the account the relay runs as), but a **token of its own**.
A token copied between machines is revoked for all of them at once.

## 3. Its own agent login

Log the agent CLI in on this machine. Each machine can use its own
subscription; it pauses on its own when its usage nears the limit
(`usage.pause_at`). Two machines on one subscription share one set of usage
numbers and pause together, which is correct.

## 4. A config with its own `node_id`

Copy `orchestrator.yaml` from the first machine and change:

```yaml
node_id: "node-b"          # a name no other machine on this account uses
```

The `node_id` is on every claim, every chat message and the status page, so
two machines with the same one look like one. If you leave it out, the
hostname is used, which is fine when hostnames differ. ctrlrelay refuses to
start with a placeholder such as `my-laptop`, `localhost` or `unknown`.

Keep `repos`, `schedules` and `timezone` the same on every machine: the
scheduled sweep is split by its scheduled time, so machines must agree on the
schedule.

## 5. Peers, for the status page

To see every machine on any machine's page, list the others' status pages:

```yaml
# on node-b
web:
  host: "192.168.1.21"           # an address node-a can reach
  allowed_hosts: ["node-b"]      # the name node-a uses for this machine
fleet:
  peers: ["http://node-a:8787"]
  account_label: "max-2"         # optional: which subscription, never an email
```

and the mirror image on `node-a`. A peer refuses a name it does not know
(HTTP 421, shown as "bad host header"): each machine's `web.allowed_hosts`
must hold the name the others use for it.

## 6. Chat

- **Mattermost:** machines can share one bot and channel. Set
  `transport.mattermost.shared: true` on every machine. Answer a question by
  replying to it; a message that is not a reply must start with the machine's
  name, `node-b: ...`.
- **Telegram:** each machine needs **its own bot** (create one with
  @BotFather). Telegram lets one process read a bot at a time.

See [Operations: chat with several machines]({{ '/operations/' | relative_url }}#chat-with-several-machines).

## 7. Check it

```bash
ctrlrelay node check
```

```
✓ node_id: node-b (named on claims, chat and the page)
✓ GitHub login: <the account>
✓ agent login: works; 12% of the 5-hour window
✓ peer http://node-a:8787: reachable, node-a
```

`--no-agent` skips the agent check, which makes one tiny call on the
subscription. A peer that answers with this machine's own name means two
machines share a `node_id` (or the URL points back here).

## 8. Start it

Start the bridge, the poller and the status page as on the first machine
([Operations]({{ '/operations/' | relative_url }})). At startup, and while it
runs, the poller warns, in its log and in chat, if a peer answers with its
name or another process is refreshing claims under its name.

Then open either machine's status page: the **Machines** strip shows both.
