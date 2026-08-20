# Switchboard — Message protocol (SB-0c)

The **cross-host contract**. Every switchboard component — the dumb listener
daemon, the ephemeral frontdesk dispatcher, the megaloop conductor — reads and
writes messages by these rules. It is transport-specific to Keybase today but
the *envelope* and *command grammar* are the swappable surface: keep everything
Keybase-shaped in one adapter (see `## Transport adapter`), and this spec is what
the rest of the system depends on.

Companion docs: `routes.schema.md` (per-host routing config, SB-0b),
`../../.claude/switchboard/BOARD.md` (decisions D1–D7, task board).

---

## 1. The envelope

Every inbound message is normalized to one envelope before any component looks at
it. The right-hand column is the exact field in `keybase chat api` `read`/`listen`
output (`.msg.*`) — this is the only place those raw paths appear.

| Envelope field | Type | Keybase source (`.msg.…`) | Meaning |
|---|---|---|---|
| `sender` | string | `sender.username` | who sent it (authz key) |
| `senderDevice` | string | `sender.device_name` | device — disambiguates a shared identity (see §7) |
| `team` | string | `channel.name` | always `acme_agents` today |
| `channel` | string | `channel.topic_name` | the routed channel, e.g. `app` |
| `convId` | string | `conversation_id` | stable per-channel id; dedup key |
| `msgId` | int | `id` | per-conversation monotonic id; dedup + reply target |
| `threadRoot` | int \| null | `content.text.replyTo` | msgId this replies to; `null` = top-level |
| `body` | string | `content.text.body` | the message text |
| `sentAt` | int (ms) | `sent_at_ms` | wall clock |
| `type` | string | `content.type` | `text` / `attachment` / `reaction` / `system` / `edit` / `delete` / … |
| `attachmentFilename` | string | `content.attachment.object.filename` | original filename (`type == "attachment"`; else `""`) |
| `attachmentPath` | string | — (daemon-set) | **abs** path of the staged payload after the daemon downloads it (spec §4); `""` until staged |

**Only `type == "text"` and `type == "attachment"` are ever dispatchable** — for
attachments the caption (`content.attachment.object.title`) is normalized into
`body`, because a captioned screenshot IS a bug/feature report (spec §4).
`reaction`, `system` (channel joins, etc.), `edit`, `delete`, and every other
content type are consumed for bookkeeping (advance the dedup cursor) but never
routed to an agent. Attachment captions pass the same sender/marker tests as
text (§5/§7), so this stays the first line of the injection defense.

`threadRoot` is how every reply stays in-thread. A receipt replies to the
originating `msgId`; a debrief replies per-row to whatever `threadRoot` the
original request carried. A message with `threadRoot == null` is a new top-level
request.

---

## 2. Dispatch decision (daemon → frontdesk)

The daemon is **dumb code**. Its entire job per inbound message:

```
1. type == "text" or "attachment"?         no  → advance cursor, drop
   (attachments: stage the payload first — spec §4; caption-less = stage only)
2. already seen (convId, msgId ≤ cursor)?  yes → drop            (dedup, SB-1)
3. authored by us? (§7 self/marker test)   yes → advance cursor, drop  (loop guard)
4. dispatchable for this sender? (§5)       no → advance cursor, drop
5. daemon paused? (§4 !pause)              yes → react ⏸️, park, drop
6. otherwise:  react 👀  → spawn frontdesk dispatcher (cwd = route.repo)
                          → advance cursor
```

The daemon never classifies *what kind of work* a message is — that is the
frontdesk dispatcher's job (question / small-task / dev-task / gate-approval /
control). The daemon only decides *whether to hand it off at all*. Keeping this
line dumb is what lets the daemon run for months without drift (invariant, D7 /
handoff).

---

## 3. Commands

A command is a `body` whose **first token** is a `!word`. Commands are the only
way a non-human sender gets acted on (§5), and the only way a gate ever moves
(§6). Everything after the command token is the argument.

| Command | Handled by | Who may use it | Effect |
|---|---|---|---|
| `!ask <question>` | frontdesk | humans; bots (explicit) | force the question path — answer in-thread, then die |
| `!task <body>` | frontdesk | humans; bots (explicit) | force dev-task filing → append to `INBOX.md`, receipt `queued as INB-n` |
| `!approve <id>` | **daemon** (no LLM) | authz per §6, per gate class | write a sender-stamped approval record (deterministic authz + atomic write — moved in-daemon after the frontdesk classifier deadlocked deploy-gates); conductor consumes it to release a gated row — EXCEPT `deploy-gate`/`fix-and-ship` rows with a route executor configured, where the daemon also fires `ship_command`/`integrate_and_ship_command` (argv template, `{row}` substituted; the executor re-validates the approval from disk) |
| `!status` | **daemon** (no LLM) | any human | render BOARD + HEARTBEAT summary in-thread — free, no agent spawned (SB-6) |
| `!nudge` | **daemon** | approvers (§6) | fire an immediate conductor incarnation now (promotion-only), respecting the lockfile (SB-6) |
| `!recap [hours\|all]` | **daemon** | any human | render the campaign work ledger (`ledger.jsonl`) as a per-row timeline — "what happened while I was out" (default 24h) |
| `!retry <id>` | **daemon** | any human | file `control/retry-<id>.json`; the conductor flips a stuck row back to TODO (ML-3b) and an auto-wave conductor is fired if the lock is free |
| `!drop <id>` | **daemon** | any human | file `control/drop-<id>.json`; the conductor marks the row WONTFIX + archives its branch (ML-3b) |
| `!ping` | **daemon** | any human | liveness — daemon replies `pong ⟦sb:pong host=<host>⟧` |
| `!pause` | **daemon** | approvers (§6) | stop dispatching; keep acking new messages with ⏸️ (SB-4) |
| `!resume` | **daemon** | approvers (§6) | resume dispatching |

Notes:
- `!status` / `!ping` / `!pause` / `!resume` / `!nudge` / `!approve` / `!recap` /
  `!retry` / `!drop` are answered **inside the daemon** — no `claude -p` spawn,
  zero token cost. They are the chat-native liveness/control surface (SB-4, SB-6).
- **Attachments (spec §4):** an attachment message on a routed channel is staged
  by the daemon to `<campaignDir>/attachments/msg<id>.<ext>` (+ `.caption.txt`) —
  or, on a routing-only channel (mode=route, e.g. `#product-chat`), to a
  host-local spool `<stateDir>/campaigns/<channel>/attachments/` so it can be
  re-uploaded cross-host. A captioned one then dispatches the frontdesk with the
  staged **absolute** path appended to the body as `[attachment: <abspath>]` AND
  set on the envelope (`attachmentPath`) — absolute because campaign attachments
  are gitignored and a relative path dangles inside a megaloop worktree.
  Caption-less → stage + 📎 ack, and the payload is **linked to the sender's
  follow-up**: the next dispatched message from the same sender in that channel
  (within 10 min, or a thread-reply to the attachment at any age ≤24 h) carries
  the staged path — screenshot-then-description is one report split across two
  messages. Dispatchability mirrors §5: humans free-form (not on command-only
  channels); bots only via a `!task` caption — which is how a cross-host
  attachment forward (`fd.py forward --attach`, marker-less, invariant §10.6)
  arrives on the target host. The frontdesk files the paths both in the body and
  as a structured `attachments` INBOX-row field.
- **Event-driven conductor (spec §1):** when a frontdesk run grows the INBOX, the
  daemon debounces 120s and fires `conductor-run.py --auto-wave` itself; the
  scheduled tick is a mop-up heartbeat. Auto-wave budget/run caps still apply.
- `!ask` / `!task` spawn a frontdesk dispatcher. For **humans**, plain free-form
  text (no `!`) is *also* dispatched and the dispatcher classifies it; the
  explicit commands just pin the lane. For **bots**, only `!ask` / `!task` are
  ever acted on (§5).
- `!approve` is the *only* thing that can move a gate, and even then it does not
  edit the BOARD — it drops an approval record the conductor validates and
  consumes exactly once (§6).

---

## 4. Reactions & markers (the machine-visible layer)

**Emoji reactions** are the daemon's instant, human-visible acknowledgements. They
are posted by the daemon before any slow work, so a sender always knows they were
heard:

| Reaction | Meaning |
|---|---|
| 👀 | dispatchable — acked, frontdesk dispatcher spawning (SB-2) |
| ⏸️ | daemon paused (`!pause`) — message parked, not dispatched |
| ⚠️ | a failure/supervision alert applies (also posted as a threaded message, §8) |

**Structured markers** ride at the end of every message *the system itself
posts* (receipts, debriefs, promotions, pongs, alerts). A marker is a single
bracketed token:

```
⟦sb:<kind> key=val key=val⟧
```

Markers do two jobs:
1. **Loop prevention that survives a shared identity.** Until the per-host bot
   accounts exist (SB-0a), every agent posts as `operator`, so "ignore your
   own uid" is not enough. Any inbound message carrying a `⟦sb:…⟧` marker is
   treated as system-authored and dropped at decision step 3 (§2). This is the
   bridge that lets pieces be tested before SB-0a — but see §7: it is *not*
   sufficient to go live.
2. **Machine-readable structure** the conductor and daemon can parse back out
   (which INBOX row a receipt was for, which incarnation a debrief belongs to)
   without re-parsing prose.

Marker kinds: `receipt`, `promote`, `debrief`, `gatequeue`, `alert`, `pong`,
`status`. Humans can ignore them; they are terse by design (D5 — receipts *read*
as just "task").

---

## 5. Who gets dispatched (authz, D2/D3)

Two sender classes, declared in `routes.yaml` (`humans:` list; everyone else is a
bot/agent):

| Sender class | Free-form text | `!task` / `!ask` | `!approve` | daemon control |
|---|---|---|---|---|
| **human** (in `humans:`) | ✅ dispatched (frontdesk classifies) | ✅ | per §6 | per §6 |
| **bot / other agent** | ❌ ignored | ✅ only these prefixes | ❌ | ❌ |
| **self** (own bot / `⟦sb:⟧` marker) | ❌ never | ❌ never | ❌ | ❌ |

Rationale (D2): bots chatter freely without triggering work; humans don't have to
prefix everything; the system never answers itself. Combined with §7's
self-test, `bot ⇒ command-only` is the loop-prevention rule.

**Attachments follow the same matrix** via their caption (spec §4): a human's
free-form caption dispatches (the screenshot is the report), a bot's attachment
acts only on a `!task` caption (the cross-host forward), and a marker'd caption
is dropped. A caption-less human attachment is staged, never dispatched.

Channel `mode` (see `routes.schema.md`) narrows this further per channel — e.g. a
`command-only` channel (`#agent-sync`) ignores even human free-form and acts only
on `!task`/`!ask`.

---

## 6. Gates & the injection rule (D3 — the security spine)

> **Message *content* never flips a gate. Only an `!approve <id>` command from a
> whitelisted sender does.**

Chat channels are untrusted input. No amount of persuasive prose, quoted
"approval", or forwarded text may release a gated row. The mechanism:

1. A gated row (`push` / `deploy` / `fleet`, or a `product-question`) sits in the
   BOARD's gate queue. The conductor posts the queue to the channel with explicit
   instructions: `!approve <id>` (ML-3).
2. `!approve <id>` is handled **deterministically inside the daemon**
   (`commands._approve`), never by an LLM — it is a DAEMON command, not a
   dispatch (§10.4: no LLM in the gate path). The daemon reads the row's gate
   class from the BOARD's operator-gate queue, **validates the sender against
   that class's approver whitelist**, and, if allowed, writes a sender-stamped
   approval record to `.claude/<campaign>/approvals/<id>.json`:
   ```json
   {"id":"T45","approvedBy":"operator","channel":"app",
    "msgId":812,"sentAt":1783480343575,"gateClass":"push-gate"}
   ```
   The daemon **never edits the BOARD** — single-writer invariant. (Historically
   this was routed to the `claude -p` frontdesk, but headless auto-mode's safety
   classifier refuses to write a deploy approval — it deadlocked deploy-gates, so
   the deterministic authz+write moved in-daemon where it always belonged.)
3. On its next incarnation the conductor consumes the approval record: re-validates
   the sender against the whitelist (defense in depth), releases the row **exactly
   once**, deletes/marks the record, and confirms in-thread (ML-3).

Approver whitelist (D3):

| Gate class | Who may `!approve` |
|---|---|
| `push-gate` / `deploy-gate` / `fleet-gate` / `security-crux` | `operator` |
| `product-question` | `operator`, `teammate` |

Daemon-control commands (`!pause` / `!resume` / `!nudge`) use the same
`approvers.default` list. An `!approve` from a non-whitelisted sender is dropped
with no state change — silent to bots/non-approvers (no control surface); a known
human who targets a non-gated id or omits the id gets a corrective reply only. **Hard gates stay gated even when
filed via chat** — chat is a filing/approval surface, not a bypass (BOARD HARD
GATES; ship-ios always gated).

---

## 7. Loop prevention & the SB-0a gate

The system must never dispatch its own output. Three independent tests, in order:

1. **Own bot uid.** Once SB-0a lands, each daemon logs in as its host bot
   (`bot_workstation` / `bot_laptop`) and drops any message whose `sender` is
   its own bot username.
2. **`⟦sb:⟧` marker.** Any message carrying a system marker (§4) is dropped
   regardless of sender.
3. **Bot ⇒ command-only.** Other agents' messages act only via `!task`/`!ask`
   (§5), so an agent's free-form chatter never loops.

> ⛔ **Live blocker (SB-0a).** Today every agent — daemon, dispatchers,
> conductor — posts as `operator`. Test 1 collapses (a dispatcher's own
> reply looks like a human message), and *human* messages get free-form dispatch,
> so the marker test (2) is the *only* thing standing between the system and an
> infinite self-dispatch loop on any channel it posts to. That is too thin to
> trust in production. **The daemon must not go live listening on any channel the
> system itself posts to until the bot accounts exist.** Until SB-0a: build and
> unit-test components, dry-run against a scratch channel the system does *not*
> post into, but do not enable live dispatch. (Handoff gotcha #1; BOARD HARD
> GATES.)

---

## 8. Message formats (the shapes components emit)

All system-emitted messages end with a `⟦sb:…⟧` marker (§4). Bodies are Keybase
markdown.

**Receipt** — frontdesk, after filing a dev-task (FD-4). Threaded to the request's
`msgId`. Reads as just "task" (D5); kind is a marker detail, not shown as a lane:
```
queued as INB-7  ⟦sb:receipt inb=INB-7 kindGuess=code⟧
```

**Promotion** — conductor, when INBOX→BOARD (ML-1). Threaded to the row's
`threadRoot`:
```
INB-7 → T42 (design)  ⟦sb:promote inb=INB-7 row=T42 kind=design⟧
```

**Debrief** — conductor, end of incarnation (ML-2). One message to the channel;
per-row lines thread to their `threadRoot` where one exists:
```
Switchboard debrief — app (incarnation 3f9a1c)
• merged:      T40, T41
• dispatched:  T42, T43
• failed:      T44 (build red — triage)
• gates:       T45 push-gate → reply `!approve T45`
⟦sb:debrief inc=3f9a1c host=workstation⟧
```

**Gate queue** — conductor (ML-3), grouped by gate class, each with its approve
instruction and who may give it:
```
Open gates — app
• T45  push-gate    → `!approve T45`   (operator)
• T46  product-question → `!approve T46` (operator / teammate)
⟦sb:gatequeue host=workstation⟧
```

**Status** — daemon, answer to `!status` (SB-6, no LLM). Rendered from BOARD +
HEARTBEAT:
```
app — 12 rows: 3 TODO · 2 DISPATCHED · 1 RETURNED · 5 DONE · 1 GATED
conductor: last beat 3m ago (phase=merge, row=T41)  ·  daemon: up 4h12m
⟦sb:status host=workstation⟧
```

**Alert** — daemon supervision (D7 / SB-5, HD-1). A ⚠️ reaction *and* a threaded
message carrying phase/row context so a silent failure becomes loud:
```
⚠️ conductor incarnation 3f9a1c stale (no beat >10m) at phase=merge row=T41 — killed, next cron `resume` will recover
⟦sb:alert host=workstation kind=stale-incarnation row=T41⟧
```

---

## 9. Transport adapter (Keybase is swappable)

Keybase is Zoom-owned and in maintenance mode (handoff gotcha). Confine every
Keybase-specific call to one adapter module so the transport can be replaced
without touching the daemon logic, frontdesk, or conductor. The adapter's
surface is exactly:

- **`listen() → stream<Envelope>`** — wraps `keybase chat api-listen`, normalizes
  each event to the §1 envelope.
- **`react(convId, msgId, emoji)`** — the 👀 / ⏸️ ack (SB-2).
- **`reply(convId, threadRoot|null, body)`** — post a message, in-thread when
  `threadRoot` is set. Appends the caller's `⟦sb:…⟧` marker.
- **`read(convId, opts)`** — backfill / cursor recovery.

Keybase send has real JSON-quoting pain (body via temp file + `python
json.dumps`) — see the `keybase-agent-sync` memory in the app project
auto-memory. All of that lives in the adapter, nowhere else.

---

## 10. Invariants (do not break — the spec's teeth)

1. **Only `type=="text"` and captioned `type=="attachment"` dispatch.** (§1,
   spec §4) Attachment captions obey the same sender/marker rules as text;
   every other content type can't carry commands.
2. **Content never flips a gate; only whitelisted `!approve` does.** (§6) The
   injection defense.
3. **Approval records are consumed exactly once, sender re-validated.** (§6)
4. **The daemon is dumb code — no LLM in the dispatch/dedup/ack/supervision
   path.** (§2) LLMs are ephemeral (frontdesk) or incarnated (conductor).
5. **The dispatcher never writes BOARD.md.** It writes append-only `INBOX.md` and
   `approvals/`; the conductor is the single BOARD writer. (§6)
6. **Every system-emitted message carries a `⟦sb:…⟧` marker.** (§4) Loop guard +
   structure. **One deliberate exception:** the FD-6 cross-host `!task` forward
   (a dispatcher on host A posting into host B's channel, §5) is emitted *without*
   a marker — a marker'd message would be dropped by host B's loop guard before it
   could be dispatched. This is safe *only* because channel sets are disjoint
   across hosts (invariant 8): the forwarding daemon never listens on the target
   channel, so it never sees its own forward. A forward must therefore always be a
   real command (`!task …`) into a channel the emitter does not own.
7. **No live dispatch on a self-posted channel until SB-0a.** (§7)
8. **Channel sets are disjoint across hosts.** Cross-host routing = posting
   `!task` into the target host's channel; chat is the only transport (D4). No
   message is processed by two daemons.

---

## 11. Shared on-disk interfaces (the campaign dir)

The daemon, the frontdesk dispatcher, and the megaloop conductor never call each
other — they cooperate through files in the campaign dir (`<repo>/<campaignDir>`,
e.g. `~/dev/new/app/.claude/app-campaign/`). These are the contracts
that let the three be built independently. **Concurrency rule:** anything a
dispatcher writes is either append-only-under-flock or a uniquely-named file it
alone creates; the conductor is the only component that *rewrites* shared state.

### Campaign dir layout
```
<campaignDir>/
  BOARD.md              conductor-only (single writer; megaloop invariant)
  INBOX.md              dispatcher append-only ⟵⟶ conductor drains (flock both)
  approvals/<id>.json   dispatcher creates one per !approve; conductor consumes once
  HEARTBEAT             LAUNCHER-written liveness; daemon supervisor reads (§ below)
  PROGRESS              conductor-written progress mark (phase/currentRow); § below
  conductor.lock        flock held by a running conductor incarnation (ML-4)
  dispatch-log.jsonl    daemon append-only audit: one line per handled message (HD-1)
  attachments/          daemon-staged payloads: msg<id>.<ext> + msg<id>.caption.txt
                        (spec §4; gitignored — always referenced by ABS path)
  TECH_DEBT.md          conductor-only (deferred sidecar)
  .merge-approvals.jsonl conductor append-only: one line per tested+reviewed
                        merge-to-master ({row,sha,tested,reviewed,at}; read by the
                        launcher backstop + the reference-transaction hook)
```

### `INBOX.md` (FD-4 → ML-1)
Append-only queue the frontdesk fills and the conductor drains. To keep
multi-writer appends safe it is a **fenced JSONL block** — each dispatcher, under
`flock(INBOX.md)`, appends exactly one line; nothing edits existing lines:
````
# INBOX — <campaign>
Append-only. Dispatchers add lines under flock; the conductor drains under flock.
```jsonl
{"inbId":"INB-7","ts":1783480343575,"sender":"operator","channel":"app","msgId":812,"threadRoot":null,"kindGuess":"code","body":"add retry to the uploader"}
```
````
Rows filed from an attachment report additionally carry `"attachments": ["<abs
path>", …]` (spec §4) — the staged screenshot(s), also present in `body` as
`[attachment: …]` lines. Wave agents `Read` these paths directly (they are
absolute precisely so a worktree checkout can reach them).
Draining (conductor, ML-1, under flock): read all lines, promote each to a BOARD
row, then **rewrite** the JSONL block removing the consumed lines (safe because
the conductor holds the lock and is the only drainer). `inbId` is monotonic per
campaign (`INB-<n>`; source of `n` = a counter file `INBOX.seq` or max-seen+1).

### `approvals/<id>.json` (FD-5 → ML-3)
One file per `!approve`, created **by the daemon** (`commands._approve`) after
sender-authz passes (PROTOCOL §6). The conductor validates the sender **again**,
releases the gated row exactly once, then deletes the file. Shape is in §6.

### `HEARTBEAT` + `PROGRESS` — two-tier liveness (ML-6 → SB-5)

Liveness is split into two single-writer files so a long `wave` can run without
the supervisor false-killing it. One fixed "stale after N minutes" threshold
can't serve both a sub-second `promote` step and a multi-minute `wave`; two
orthogonal signals can.

**`HEARTBEAT` — the launcher owns it (liveness).** `conductor-run.py` rewrites it
(atomic temp+rename) every ~60s for as long as the `claude` child is alive, and
folds in the progress tier. The conductor does **not** write it.
```json
{"incarnationId":"3f9a1c","pid":48213,"launcherPid":48200,
 "campaign":"app-campaign","phase":"wave","currentRow":"T41",
 "lastBeat":1783480343575,"progressAt":1783480300000}
```
- `pid` — the `claude` incarnation (what the supervisor kills to stop work).
- `launcherPid` — the `conductor-run.py` process (the incarnation's supervisor).
- `lastBeat` — epoch ms, the launcher's liveness beat.
- `progressAt` — `max(this incarnation's PROGRESS.at, transcript mtime, start)`:
  the last time the incarnation did anything observable.

**`PROGRESS` — the conductor owns it (progress).** The `claude -p` incarnation
rewrites it (atomic) at each phase/row transition; `incarnationId` comes from
`$SWITCHBOARD_INCARNATION` so it correlates with HEARTBEAT.
```json
{"incarnationId":"3f9a1c","phase":"wave","currentRow":"T41","at":1783480343575}
```

The daemon supervisor (SB-5) reads `HEARTBEAT` + `conductor.lock` and applies the
design-D kill matrix: **lock held** + `lastBeat` stale (>3 min) → launcher wedged,
kill `pid`+`launcherPid`; **lock held** + active phase & `progressAt` frozen (>30
min) → stuck LLM, kill `pid`; **lock free** + `pid` alive & `launcherPid` dead →
orphan, kill `pid`; **lock free** + `pid` dead & no beat >2× cron cadence →
⚠️ cron-broken. A healthy wave (rows advancing, output flowing) trips none of
these at any duration. The supervisor **never unlinks the lock** — it kills, and
the launcher releases its own flock on exit. It **monitors, never links** — never
blocks dispatch. A PID-reuse guard checks the pid's cmdline before any SIGTERM.
`resume` need not tolerate a stale HEARTBEAT: the launcher owns and refreshes it.

### `conductor.lock` (ML-4 → SB-5/SB-6)
An flock file in the campaign dir. A conductor incarnation acquires it at start
and holds it for its lifetime; a second incarnation that can't acquire it exits
(protects the serial-merge invariant against cron overlap). `!nudge` (SB-6) fires
an incarnation only if the lock is free; `!status` (SB-6) reads BOARD + HEARTBEAT
without acquiring anything (zero LLM, never blocks).

### `dispatch-log.jsonl` (HD-1)
Daemon append-only, one line per handled message, so a silent dispatcher failure
becomes auditable:
```json
{"ts":1783480343575,"convId":"…","msgId":812,"sender":"…","outcome":"dispatch","transcript":"…/frontdesk-812.log","exit":0}
```
On a dispatcher crash/timeout the daemon also posts a ⚠️ threaded reply (§8).
