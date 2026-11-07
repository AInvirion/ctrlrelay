---
title: Registered mode (design)
layout: default
nav_order: 12
description: "Proposal: how a ctrlrelay node registers with a separate control plane, takes work from it and reports back, without opening a port or handing over a credential."
permalink: /registered-mode/
---

# Registered mode: design

**Status: agreed 2026-11-06, not built.** This is the ctrlrelay side of issue
#251. The control plane is a separate project; this page only says what a node
sends, what it accepts, and what it refuses. Nothing here is implemented. The
owner's decisions are at the end, under "Decisions".

## The model in one paragraph

A **node** is one ctrlrelay machine. It takes an issue, works it in its own
worktree and opens a pull request. The **control plane** is a separate service
that sees every node and decides which node gets which piece of work. In
**standalone** mode (today) a node polls GitHub itself. In **registered**
mode it asks the control plane for work instead. In both modes the node calls
out and never listens, keeps its own repo allowlist and credentials, and writes
its claim on the GitHub issue.

## Direction: the control plane is the brain

Set by the owner on 2026-11-06. The control plane is the **centralized
knowledge, brain, coordinator and role definition** of the factory:

- **Knowledge and memory:** history, per-repo learnings, design decisions,
  context. One store every node draws on, not one per node.
- **Coordination:** the work queue, routing, schedules, config (decisions 6
  and 7).
- **Roles:** what a node is asked to be for a piece of work (for example
  planner, builder, reviewer, triager), with the skills, context and, where
  needed, the credentials and agent config that role needs.

**Nodes are smart but can start clean.** A node brings the runtime: the
agent CLI, the sandbox, the pipeline steps, its claims and its own GitHub
access. Skills come with roles. It does not need to carry knowledge between runs: a fresh
machine, or one wiped, gets what it needs from the control plane when it
registers and when it takes work. Losing a node loses no knowledge.

**What this page covers and what it does not.** The protocol below (register,
heartbeat, get work, report, release, auth, outage, claims) is agreed and is
the first thing to build. Handing knowledge, roles and credentials from the
control plane to a node changes rule 3 below and turns the threat model
around: once the control plane holds what agents are told and some
credentials, protecting it becomes the main security job. That design is
[The control plane as the brain]({{ '/control-plane-brain/' | relative_url }})
(#372, agreed): what steers an agent, and config, is signed with an operator
key the server does not hold.

## What already exists and is reused

These shipped for several machines on one account. Registered mode builds on
them and changes none of them.

| Mechanism | Where | Role in registered mode |
|---|---|---|
| Issue claims: a comment carrying `<!-- ctrlrelay:claimed node=... id=... state=... -->`, a race check (lowest comment id wins), `working` claims refreshed every `claims.refresh_seconds`, stale after `claims.stale_after_seconds`, `held` claims never stale, `<!-- ctrlrelay:released id=... -->` | `src/ctrlrelay/core/claims.py`, [Operations: several machines]({{ '/operations/' | relative_url }}#several-machines-on-one-account) | **The lock.** An assignment from the control plane is only an offer; the claim decides. |
| Scheduled-job claims: `refs/ctrlrelay/claims/<job>/<window>`, created all-or-nothing | `src/ctrlrelay/core/job_claims.py`, [Operations: scheduled jobs]({{ '/operations/' | relative_url }}#with-several-machines-on-one-account) | Unchanged. A sweep runs once across nodes whoever asked for it. |
| Usage pause: `usage.pause_at`, `UsageGate`, `unknown` never read as 0% | `src/ctrlrelay/core/usage.py`, [Configuration: usage]({{ '/configuration/' | relative_url }}#usage) | The node still refuses new work while paused, whatever the control plane says. |
| `node` block in `/api/status` (`name`, `version`, `state`, `slots`, `usage`, `github_api`, `claims`, `heartbeat_at`, `last_poll_at`, `open_questions`) | `src/ctrlrelay/web/status.py`, `src/ctrlrelay/core/heartbeat.py` | **The heartbeat body.** The same block is sent to the control plane. |
| `fleet.peers`: each status page reads its peers' `/api/status` | `src/ctrlrelay/web/fleet.py`, [Configuration: fleet]({{ '/configuration/' | relative_url }}#fleet) | Still works; becomes optional once the control plane shows every node. |
| Chat per machine: messages tagged `[node-a]`, a node acts only on replies to its own posts | `src/ctrlrelay/bridge/`, [Operations: chat]({{ '/operations/' | relative_url }}#chat-with-several-machines) | Unchanged. Questions are still asked and answered in chat on the node. |
| `node_id` that must name the machine, `ctrlrelay node check` | `src/ctrlrelay/core/config.py`, `src/ctrlrelay/core/node_check.py`, [Adding a machine]({{ '/adding-a-machine/' | relative_url }}) | The node's identity to the control plane, alongside its key. |
| Output scrubbing | `src/ctrlrelay/core/redact.py` | Everything reported to the control plane goes through it. |

## Rules that hold in registered mode

The constraints from #251, plus one this design adds:

1. The node calls out over HTTPS; the control plane never calls in. No port.
2. The node refuses work outside its `repos` allowlist. The allowlist may
   come from the control plane's config (see "Config from the control plane").
3. Node credentials (the node key, the GitHub token, the agent login) never
   leave the node. Role credentials may come from the control plane, sealed
   to one node for one assignment
   ([brain design]({{ '/control-plane-brain/' | relative_url }}#credentials)).
4. Each node has its own key, revocable alone.
5. The node still writes its claim on the GitHub issue, with the same marker.
6. With no `control_plane:` block, standalone mode runs exactly as today.
7. **An assignment is an offer; the claim is the lock.** When the control
   plane and GitHub disagree about who has an issue, GitHub wins.

## Configuration

```yaml
mode: registered                       # default: standalone
control_plane:
  url: "https://cp.example.com"
  key_file: "~/.ctrlrelay/node.key"    # private key, created on the node, mode 0600
  ca_file: null                        # optional: pin the control plane's CA
  on_outage: hold                      # hold | standalone (see "Control plane outage")
  standalone_after_minutes: 30         # only with on_outage: standalone
  accept:                              # what this node offers to run
    pipelines: [dev, task, secops]
    webhook_tasks: []                  # repos that may receive webhook work; empty = none
```

`repos`, `claims`, `usage`, `schedules` and `transport` keep their current
meaning. A registered node does not run its GitHub issue poll; it still runs
its own claim refresh, merge watcher, question sweepers and usage probe.

## Protocol

JSON over HTTPS, under `/v1/`. Every request carries the node's signature
(see "Auth"). Every request body carries `protocol: 1`; a node refuses a
response with a major version it does not know, and says so in its log and
chat. All times are epoch seconds, set by the sender; the control plane uses
its own clock for leases.

### Register

Sent at startup and after any `401` once the key is valid again.

```
POST /v1/register
{
  "protocol": 1,
  "node_id": "node-a",
  "key_id": "ed25519:9f2c...",          # fingerprint of the public key
  "seal_key": "x25519:3a7d...",          # public key role credentials are sealed to
  "version": "0.18.0",
  "account_label": "max-2",              # fleet.account_label, never an email
  "capabilities": {
    "agents": ["claude"],                # agent.type values this node can run (#286)
    "agent_plugins": {                   # concrete identities/capabilities (#379)
      "claude": {
        "plugin_id": "claude", "plugin_version": "1",
        "resume": true, "structured_events": true,
        "usage": true, "sandbox": true,
        "models": [null]
      }
    },
    "assignable_agents": ["claude"],     # local agent.assignable_types policy
    "pipelines": ["dev", "task", "secops"],
    "slots": 1,
    "labels": ["needs:docker"]           # optional, free-form, for routing
  },
  "repos": ["owner/app", "owner/lib"],   # the allowlist, so the control plane can route
  "holding": [                           # node.claims: what it holds right now
    {"repo": "owner/app", "issue_number": 42, "claim_id": "3f9c0a1b2c4d",
     "state": "held", "assignment_id": "asg_7Hq..."}
  ]
}
→ 200 {"protocol": 1, "node_session": "ns_...", "heartbeat_seconds": 60,
       "lease_seconds": 2100, "commands": []}
→ 401 unknown or revoked key   → 409 node_id held by another key
```

`agent_plugins` is the local enrollment inventory, not permission for the
control plane to invent a CLI. `assignable_agents` is the smaller set an
assignment may explicitly select. A plugin without `structured_events` gets no
live activity/loop detection, one without `usage` is never counted as zero, and
one without `sandbox` remains visibly unconfined.

`holding` lets the control plane reconcile after a restart (see "Node death").

### Heartbeat

Every `heartbeat_seconds`, from its own task, so it moves while a run holds
the poll loop (as `heartbeat.json` does today).

```
POST /v1/heartbeat
{"protocol": 1, "node_session": "ns_...", "node": { ...the /api/status node block... }}
→ 200 {"commands": [{"id": "cmd_1", "do": "pause"}], "config": null}
```

`commands` is a fixed list and nothing else: `pause`, `resume`, `drain`
(finish current work, take no more), `release` (with an `assignment_id`; see
below), `reregister`. An unknown command is logged and ignored, never
guessed at. `config` is described under "Config from the control plane".

### Get work

Only when the node has a free slot, is not paused for usage, and is not
draining. Long-poll, up to 30 seconds.

```
POST /v1/work/next
{"protocol": 1, "node_session": "ns_...", "free_slots": 1}
→ 204                                   # nothing for you
→ 200 {
  "assignment_id": "asg_7Hq...",        # random, unguessable
  "kind": "issue",                      # issue | scheduled_job | webhook_task
  "repo": "owner/app",
  "issue_number": 42,                   # issue only
  "pipeline": "dev",
  "agent": {"type": "opencode"},       # optional; must be locally assignable
  "attempt": 1,
  "replaces_claim_id": null,            # set when a stale claim is being taken over
  "lease_seconds": 2100
}
```

An assignment names **what**, never **how**: no prompt, no command, no URL to
fetch, no branch name, no file, no executable and no free-form CLI arguments.
Its optional `agent.type` selects only a locally enrolled type present in
`assignable_agents`; omitting it uses the node's repository/global default. A
signed role may additionally select a model and agent settings within local
`role_limits`. The node refuses an unknown or disallowed selection and never
silently substitutes another plugin. The concrete identity is pinned to the run
before dispatch and reused for reports, CI fixes, retries and native resumes.

With brain content the assignment also names a `role`
and a `knowledge` snapshot by digest, and the node runs them only if they
carry an operator signature
([brain design]({{ '/control-plane-brain/' | relative_url }}#roles)). The node reads the issue from GitHub itself,
with its own token, exactly as a standalone poll would. A `scheduled_job`
names the job and its due time (`window`), and the node creates the same
`refs/ctrlrelay/claims/<job>/<window>` ref. A `webhook_task` is described
under "Where the moved items fit".

### Accept or refuse

The node checks the assignment **before** it posts a claim, then answers:

```
POST /v1/work/asg_7Hq.../accept   {"claim_id": "3f9c0a1b2c4d", "claim_comment_id": 1234567}
POST /v1/work/asg_7Hq.../refuse   {"reason": "repo_not_allowed"}
```

Refusal reasons: `repo_not_allowed`, `pipeline_not_offered`,
`agent_not_available`, `not_eligible` (the issue fails the node's own intake
rules, below), `claimed_elsewhere` (with the owner's node name, read from the
claim), `lost_race`, `paused`, `github_unreachable`. "Could not look" is its
own reason and never reported as "nothing there".

Checks, in order:

1. `repo` is in this node's `repos`.
2. `pipeline` is in `control_plane.accept.pipelines`, and the node can run it
   with an agent it has.
3. The issue passes the same intake rules a standalone poll applies today
   (`src/ctrlrelay/core/poller.py`): open, not archived, assigned by the
   configured user or carrying a trigger label that user added, no
   `exclude_labels`, `require_labels` met. So the control plane can only pick
   among issues the operator already marked for the relay.
4. The usage gate allows new work.
5. The existing claim path: no live claim from another node, post the claim,
   wait `claims.race_wait_seconds`, read again, lowest comment id wins. A loser
   posts its release as today and answers `lost_race`.

Only then does the run start.

### Report

Sent on every state change and at least every `claims.refresh_seconds`, right
after the claim refresh on GitHub. A report renews the assignment's lease.

```
POST /v1/work/asg_7Hq.../report
{
  "seq": 7,                             # per assignment; resends reuse it
  "state": "working",                   # working | held | blocked | done | failed | dropped
  "phase": "review",
  "claim_id": "3f9c0a1b2c4d",
  "agent": {"type": "opencode", "plugin_id": "opencode",
            "plugin_version": "1", "model": "local/qwen3-coder:30b",
            "provider": "local", "fingerprint": "9f2c..."},
  "pr": {"number": 88},                 # once known; a URL is built by the reader
  "question": "Which config key ...",   # blocked only, scrubbed, at most 500 characters
  "numbers": {"seconds": 1840, "cost_usd": null, "ci_rounds": 2}
}
→ 200 {"lease_until": 1793000000}   → 410 assignment withdrawn (see "Release")
```

`agent` reports the concrete selection that actually ran, including its
plugin/model identity, rather than merely echoing the request. A retry or
resume keeps that identity even if the node's defaults later change.

`held` means what it means for a claim: the run finished or waits for an
answer, and the lease no longer expires. Agent output, transcripts and output
tails are **not** sent. Free text that is sent (`question`, a failure
`summary`) goes through `src/ctrlrelay/core/redact.py` first.

### Release

Either side can end an assignment.

- **Node:** `POST /v1/work/<id>/release {"reason": "failed" | "dropped" | "done"}`
  after it posts `<!-- ctrlrelay:released id=... -->` on the issue, as today.
- **Control plane:** a `release` command, or `410` on a report. The node stops
  refreshing its claim and posts its release on the issue at the next safe
  point: before the agent starts, or after the run ends. It does **not** kill a
  running agent on the control plane's word; a run already going finishes and
  its PR is still opened, or the operator stops it on the node. This keeps a
  compromised control plane from throwing away work in progress, and it keeps
  the claim honest: the issue says "taken" for exactly as long as a node
  works on it.

### Idempotency and delivery

Every write is safe to resend: `accept`, `refuse` and `release` by
`assignment_id`, `report` by `(assignment_id, seq)`. A report that cannot be
sent is queued on disk beside the state database and sent in order once the
control plane answers. The queue is bounded (newest 500 per assignment, then
only the latest state is kept), and its length shows on the status page.

## Auth

**Enrolment.** `ctrlrelay node enroll` creates an Ed25519 key pair on the
node, writes the private key to `control_plane.key_file` (mode `0600`) and
prints the public key and its fingerprint. It also creates the X25519 pair
that role credentials are sealed to (sent as `seal_key` at register). The operator adds the public key to
the control plane under the node's name. The private key never leaves the
node and never appears in a log, chat message or status page.

**Requests.** Each request is signed with HTTP Message Signatures (RFC 9421):
method, path, a digest of the body, the `node_id`, a timestamp and a random
nonce. The control plane refuses a timestamp more than 5 minutes off its
clock, or a nonce it has seen in that window. TLS protects the channel; the
node checks the server certificate against the system store, or `ca_file`
when set.

**Rotation.** `ctrlrelay node rotate-key` creates a new pair and sends
`POST /v1/keys/rotate {"new_key": "..."}` signed with the **old** key. The
control plane accepts both for an overlap (default 1 hour), then only the new
one. No operator step is needed unless the old key is suspected lost.

**Revocation.** The operator revokes one node's key on the control plane.
That node's next request gets `401 revoked`. It then:

- takes no more registered work, and says so once in its log and chat;
- keeps runs already going to their end, because their claims, worktrees and
  credentials are local and the issue says who has them;
- does **not** fall back to standalone on its own: a revoked node waits for
  the operator. A `401` is never read as an outage.

Other nodes are not affected. Revoking a node's **GitHub** access is separate
and stays where it is today: each node has its own token
([Adding a machine]({{ '/adding-a-machine/' | relative_url }})).

## Control plane outage

"Could not reach the control plane" is not "no work". The node never treats
a failed request as an empty answer.

With `on_outage: hold` (the default):

- runs already going continue; their claims are refreshed **on GitHub** as
  today, which is what keeps other nodes off them;
- reports queue on disk and are resent in order;
- no new work is taken;
- the node retries with backoff (5 s doubling to 5 minutes), posts one chat
  line when it loses the control plane and one when it is back, and shows
  `control_plane: unreachable since <time>` in its `node` block.

With `on_outage: standalone`, after `standalone_after_minutes` the node also
starts its own GitHub poll, exactly as standalone mode. This is safe for
double work because every take still goes through the claim race check: an
issue the control plane gave another node is claimed on GitHub and is
skipped. When the control plane is back, the node registers again, sends what
it holds in `holding`, and stops polling. The cost is that the control plane's
routing choices (backend, headroom) are not applied during the outage.

## Node death mid-run

Two clocks run for one piece of work: the claim on GitHub
(`claims.stale_after_seconds`, default 1800) and the lease on the control
plane (`lease_seconds`). The rule: **the lease is always longer than the stale
time**, so the control plane can never offer an issue before GitHub would let
another node take it. The control plane enforces
`lease_seconds >= stale_after_seconds + 300`, from the value the node reports.

When a lease runs out:

1. The control plane marks the assignment `lost` and may offer it again, with
   `replaces_claim_id` set to the old claim.
2. The new node runs the **existing takeover path**: it reads the claim, and
   takes over only if the claim is stale by the rule in
   `src/ctrlrelay/core/claims.py`. Its claim names the one it replaced.
3. If the old claim is still fresh (the old node lost the control plane but
   not GitHub, and is still working), the new node answers
   `claimed_elsewhere` with the owner's name, and the control plane marks the
   old assignment alive again.
4. If the old node comes back, its claim was taken over: it logs
   `claims.taken_over`, stops refreshing, finishes its run as today, and reports
   `state: dropped`.

A `held` claim never goes stale and a `held` assignment never expires, so a
long wait for an answer never hands the issue to another node. Freeing such an
issue stays a human act: delete the claim comment, as today.

**Restart.** A node that restarts registers with `holding`. The control plane
reconciles: an assignment it has for that node and the node no longer holds
is closed as `dropped`; a claim the node holds that the control plane does not
know (taken in standalone mode, or during an outage) is recorded as an
assignment.

A scheduled job that dies loses its window, as today; the next window is a
new ref.

## Two nodes never work one issue

Three layers, and only the last has to be right:

1. The control plane offers one issue to one node at a time.
2. The node skips an issue with another node's live claim.
3. The race check on GitHub: lowest comment id wins, whatever either side
   believes.

**Decided (decision 8): no mixed fleets.** On one account, every node is
standalone or every node is registered; moving to registered mode moves all
of them. The only exception is the opt-in outage fallback (decision 1),
during which a registered node polls on its own for a while. The claims keep
that safe. The control plane flags any claim on a watched repo made by a
`node_id` it has not registered, since that means a standalone node is still
running. The mixed-version limit in
[Operations]({{ '/operations/' | relative_url }}#several-machines-on-one-account)
still holds: every node must be on a version with named claims.

## Where the moved items fit

**Webhook intake (from #227).** A webhook needs an open port, so it lands on
the control plane. The control plane turns it into a `webhook_task`
assignment carrying the payload as data:

```
{"kind": "webhook_task", "repo": "owner/app", "source": "error-tracker",
 "title": "TypeError in checkout", "body": "...", "dedupe_key": "evt_91f..."}
```

A node accepts one only for a repo listed in
`control_plane.accept.webhook_tasks` (empty by default). On accept it opens a
GitHub issue with its own token, from a fixed template: the title, the body
inside a fenced block marked as external data, the source, the `dedupe_key`
in a marker, and a `source:webhook` label. It looks for an open issue with
the same `dedupe_key` first and comments there instead. The issue then goes
through the normal path: triage (#227), claim, run. So all work is still a
GitHub issue, and webhook text reaches an agent only as issue text, which is
already untrusted. Whether such an issue may start a run without the
operator's assignment: no (decision 4).

**Numbers across nodes (#230).** Each node keeps its own numbers, as #230
builds them. Reports carry the per-run figures (`numbers`), and the heartbeat
carries the node's totals. The control plane adds them up; it computes
nothing a node cannot also show for itself.

**Shared per-repo learning (#225).** **Decided (decision 5): for now it lives
in the repository on GitHub**, readable by every node with the token it
already has, and changes only after the operator approves them, as #225 asks.
The control plane may show it, and may say a new version exists; the node
reads it from GitHub. **Later direction:** the control plane holds and hands
out this memory (see "Direction: the control plane is the brain"), designed
in #372.

**Routing by backend (#286).** Nodes declare `capabilities.agents` and
`labels` at register. The control plane only offers work a node says it can
run; the node checks again on accept and answers `agent_not_available` if it
cannot. Routing by usage headroom uses the `usage` in the heartbeat.

**Scheduled jobs.** **Decided (decision 7): configurable by the control
plane.** Schedules are part of the pushed config, and the control plane may
also send `scheduled_job` assignments directly. Either way the
`refs/ctrlrelay/claims/<job>/<window>` ref is still the lock, so a job runs
once across nodes.

## Config from the control plane

The epic asks for config handed to every node. **Decided (decision 6): the
control plane may set the full config**, so it is the one place every node's
settings are managed. A heartbeat answer may carry a `config` object with any
key of the node config: repos, labels, usage, slots, schedules, `agent`,
sandbox paths, transport.

Three things stay local, because without them the node cannot reach the
control plane or prove who it is: the `control_plane` block itself (with the
pinned operator keys), the node's key, and `node_id`. A pushed object that
sets any of them is refused whole, logged, and the last good config is kept.

**A pushed config must be signed with an operator key** (decision 2 of the
[brain design]({{ '/control-plane-brain/' | relative_url }})). An unsigned
or wrongly signed one is refused whole, like an invalid one. It also carries
a version and a `not_after`, and a node refuses one past its `not_after` or
below the config floor in the signed policy list, so an older, wider config
cannot be replayed
([brain design]({{ '/control-plane-brain/' | relative_url }}#signing)).

A pushed config is validated exactly like the file (unknown keys and bad
values refuse the whole object). It is applied between runs, never under a
run in progress, and shown on the status page with its version. The file on
disk is never edited: it is the config used before the first push, and after
a restart until the control plane answers.

## Threat model

**Assume the control plane is compromised.** What it can still do:

- see what nodes report: repo names, issue numbers, phases, scrubbed
  questions, numbers, the allowlist;
- withhold work, or send all work to one node;
- offer an eligible issue to a node that would not otherwise have picked it;
- pause or drain nodes;
- with webhook tasks enabled for a repo, open issues there through a node;
- read the knowledge, roles and role credentials it stores (see the
  [brain design]({{ '/control-plane-brain/' | relative_url }}#threat-model)).

What it cannot do, and what stops it:

| It cannot | Because |
|---|---|
| make a node work on a repo outside its allowlist | check 1 on accept, against the node's current `repos`; that list changes only by a config signed with an operator key |
| change a node's config | a pushed config must carry an operator signature |
| make a node work an issue the operator did not mark | check 3 reruns today's intake rules on the issue as GitHub serves it |
| put its own instructions in front of the agent | assignments carry no prompt, command, URL or branch; roles, skills, context and learnings are shown as instructions only when signed with an operator key; history is shown only as fenced data; webhook text enters only as a fenced issue body, only for opted-in repos |
| run a command on the node | the command list is fixed and has no arguments beyond an assignment id; there is no inbound port |
| get a node credential | the private key, GitHub token and agent login never leave the node; free text goes through `redact.py`; output tails and transcripts are not sent. Role credentials it holds are exposed if it is compromised, so they are scoped and short-lived ([brain design]({{ '/control-plane-brain/' | relative_url }}#threat-model)) |
| make two nodes work one issue | the GitHub claim decides, not the assignment |
| throw away a run in progress | a `release` stops at the next safe point and never kills an agent |
| impersonate another node | requests are signed per node; `409` on a `node_id` held by another key |

**Limits of that list.** A node still reads issue text and repository
content, which anyone with write access to the repo controls; that risk is
the same as standalone and is handled by the agent sandbox
([Configuration: what the agent can reach]({{ '/configuration/' | relative_url }}#what-the-agent-can-reach-219)),
not by this design. A compromised control plane that can also write to a
repository can create an issue and wait for the operator to assign it; the
assignment check only proves the operator marked it.

**Assume a node is compromised.** It can report false state and refuse work.
It cannot read other nodes' keys or work. Revoke its key on the control plane
and its token on GitHub; the others carry on.

## Non-goals

- Building the control plane. This page fixes the protocol it must speak.
- The control plane calling a node, or a node calling another node.
- The control plane holding or brokering GitHub tokens or agent logins, for now (decision 3 of the brain design, #372).
- Remote commands beyond the fixed list; any free-form instruction channel.
- Answering agent questions through the control plane. Chat stays on the node
  in the first version.
- Replacing GitHub claims with control-plane state.
- Several GitHub accounts, or a public hosted control plane.
- Forecasting token use; routing reacts to measured usage only.

## Decisions

Made by the owner: 1, 3 and 4 on 2026-11-03, the rest on 2026-11-06 (see #251).

1. **Outage default:** `hold`, with `standalone` opt-in per node.
2. **Auth:** signed requests (RFC 9421, Ed25519), one key per node, as in
   "Auth" above. No mutual TLS.
3. **An issue must still be marked by the operator** (assignment or trigger
   label) before a node takes it from the control plane. Revisit when the
   control plane has its own permission model.
4. **Webhook work** never runs without the operator's assignment. The node
   opens the issue, only for opted-in repos, and it waits for assignment or
   triage (#227).
5. **Shared per-repo learning:** in the repository on GitHub for now. The
   direction is the control plane as the brain: knowledge, memory, roles, and
   where needed role credentials and config, with nodes able to start clean
   (see "Direction: the control plane is the brain", designed in #372).
6. **Config from the control plane:** full config, except the `control_plane`
   block, the node key and `node_id`. Amended 2026-11-06: it must be signed
   with an operator key (#372).
7. **Scheduled jobs:** configurable by the control plane; the ref claim stays
   the lock.
8. **Mixed fleets:** not allowed. One account is all standalone or all
   registered, apart from the outage fallback.
