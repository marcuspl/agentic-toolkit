---
name: megaloop
description: Use this skill to run a big itemized todo campaign where rows are claimed and delegated to sub-agents that implement in isolated worktrees, self-swarm-review, then return branches for the orchestrator to merge — while keeping the main session's context lean because all state lives on disk (BOARD.md) and the per-row work runs in an ephemeral engine, not the conversation. Subcommands: gather (compile the master board from design docs/plans/tech-debt), wave (dispatch the next unblocked, conflict-free set), merge (integrate returned branches), status, gates, resume. Triggers on "/megaloop", "run a megaloop", "start/continue the loop", "gather the board", "run the next wave", "megaloop status".
version: 0.1.0
---

# Megaloop — delegated todo-campaign runner

A campaign is a big todo list whose rows are claimed, delegated to sub-agents
(each in an isolated git worktree), self-reviewed, and merged back. The point of
this skill is to make that loop **repeatable and context-cheap**: the main
session stays a thin, disposable conductor because

1. **All state lives on disk** — `BOARD.md` (the single-writer registry),
   `PROTOCOL.md` (the agent contract), `TECH_DEBT.md` (the deferred sidecar).
   The conductor holds nothing the board doesn't; it can be compacted anytime and
   re-hydrated with `resume`.
2. **The per-row work runs in an ephemeral engine**, not the conversation. The
   engine (a Workflow-tool JS script today; a `megaloop` Go binary later — same
   contract) spawns the sub-agents, runs the semaphore, and returns one summary.
   Nothing that accumulates is an LLM context, so the loop bottoms out instead of
   swelling.

Campaign files live at `.claude/<campaign-slug>/` in the target repo:
`BOARD.md`, `PROTOCOL.md`, `TECH_DEBT.md`. The state vocabulary (kind / status /
gate_class / review_verdict — four orthogonal axes) is in `STATE_MODEL.md` next
to this file; read it before `gather` or `wave`.

## The golden rules (never violate)

- **BOARD.md is single-writer: only the conductor writes it.** Sub-agents work in
  worktrees and return distilled reports; the conductor reflects their status.
- **Hard gates never run autonomously.** Any row of kind `fleet` (live infra
  mutation), `push` (git push / PR), or `deploy` (artifact to a running host) is
  parked in the operator-gate queue — never dispatched to an agent, never done by
  the conductor without explicit operator go-ahead.
- **Merges are serial and conductor-owned.** Agents produce green per-item
  branches; integrating them into the campaign branch (the default target) — or
  into `master` via the tested+reviewed path in `merge` step 2 — is done by the
  conductor (or a serial final stage), never in parallel (shared git index).
- **Never fabricate.** If the engine errors or an agent returns null, report it
  and stop the affected row — don't invent a result.

## Subcommands

Parse the skill args. First token selects the subcommand; default is `resume`.

---

### `gather` — compile the master board

Turn a pile of inputs into a reviewable `BOARD.md`. This formalizes the
"turn design docs into an itemized list" step. Args: a campaign slug and the
source inputs (`--from <paths...>`, plus the current plan/design docs and git
state).

1. **Collect sources.** The plan/design docs named in args, any existing
   `TECH_DEBT.md`, `git status`/`git log` for in-flight work, and the operator's
   brief.
2. **Extract candidates (fan out).** Spawn one `Explore`/reader agent per source
   (or use the engine's `gather` mode) to pull candidate items. Each candidate →
   a normalized row: `{id, title (one line), kind-guess, files-guess, deps-guess,
   doc-ref}`.
3. **Dedup + merge** overlapping candidates.
4. **Sequence into waves** by dependency order and file-set disjointness — rows
   that touch a shared file are serialized, not parallelized (see PROTOCOL).
5. **Tag gates.** Every `fleet`/`push`/`deploy` row gets a `gate_class`; flag any
   `security-crux` rows to hold for careful handling.
6. **Emit** `BOARD.md` (from `templates/BOARD.template.md`) and, if absent, seed
   `PROTOCOL.md` and `TECH_DEBT.md` from their templates.
7. **STOP.** Present the draft board and wait for the operator to approve/edit
   before any wave runs. Gather never dispatches work.

---

### `wave` — run the next wave

Dispatch the next set of unblocked, conflict-free rows through the engine.

1. **Read `BOARD.md` + `PROTOCOL.md`.** Compute the dispatchable set: `status =
   TODO`, all `deps` satisfied (`DONE`/`MERGED`), `kind` in
   {`code`,`investigate`,`design`}, and file-sets disjoint within the set (serialize
   conflicts across waves). **Exclude** all `fleet`/`push`/`deploy` rows — those
   are gates.
2. **Record claims in `BOARD.md` BEFORE dispatch** (status → `DISPATCHED`, branch
   `megaloop/<id>`). This is the single-writer claim.
3. **Run the engine.** Invoke the Workflow tool with the prototype engine script,
   passing the rows + config as `args`:

   ```
   Workflow({
     scriptPath: "<this skill dir>/engine/wave-runner.mjs",
     args: {
       campaignDir: ".claude/<slug>",
       config: { baseBranch: "<campaign base>", swarmReviewCmd: "swarm-review --preset code --git-diff --timeout 240", testCmd: "cd go && go test ./...", worktreeSetup: "<fast dep-prep for a cold worktree, e.g. link the main checkout's node_modules — see below>" },
       rows: [ /* the dispatchable rows, each with an item prompt built from its doc-ref + PROTOCOL contract */ ]
     }
   })
   ```

   (The skill dir is where this `SKILL.md` lives — typically
   `~/.claude/skills/megaloop/` or `<meta>/.claude/skills/megaloop/`.) The engine
   runs in the background and returns **one summary object**: `{returned, gates,
   failed}`. The conductor's context cost for the whole wave is that one object —
   not the agent transcripts.
4. **Reflect status in `BOARD.md`**: returned rows → `RETURNED` (with branch +
   review_verdict), gate-discoveries → `GATED` (+ gate_class) in the operator
   queue, failures → `FAILED` for triage, consciously-deferred spots → `DEFERRED`
   (moved to `TECH_DEBT.md`).
5. **Then `merge`** (below), then loop: recompute the dispatchable set and run the
   next wave until only gated/blocked work remains — at which point surface the
   gate queue and stop.

**Waves are resumable across incarnations — do not cram a whole wave into one.**
The engine commits each row's `megaloop/<id>` branch **before** the slow
test/review steps (commit-first durability), so an incarnation that runs out of
budget mid-wave still leaves durable `RETURNED` branches on disk. The next
incarnation (cron/`!nudge`) recomputes the board and continues — verifying and
merging the branches a prior incarnation left. So when budget is tight, make
**durable progress** (branches committed, board reflected) and let the loop carry
it; don't try to fit implement+verify+merge+debrief into a single headless
`claude -p` turn. **The classic budget sink is a cold worktree `npm install`** —
each isolation worktree starts with no deps. Set `config.worktreeSetup` to a fast
dep-prep so a row doesn't spend its whole budget reinstalling before it can
commit. **If `<campaignDir>/BUILD-RECIPES.md` exists, use its pinned, verified
`worktreeSetup`/`testCmd`/`baseBranch` for the area the dispatched rows touch —
do NOT re-derive.** A pinned recipe that's been run beats guessing: a wrong cold
`npm install` recipe is exactly what blew the first app auto-wave (T2).
Only when no recipe file covers the area do you discover it from the repo's
`package.json`/`Makefile` — and once proven, add it to `BUILD-RECIPES.md` so the
next incarnation inherits it. Note a copy of `node_modules` can be safer than a
symlink (Vite `/@fs` resolution); the recipe file records which.

*Switchboard mode:* write `PROGRESS phase=wave` before invoking the engine, and
**advance it whenever you start a new row/sub-agent** (that `at` timestamp is how
the supervisor sees a long wave making forward progress rather than hanging —
ML-6). The launcher keeps HEARTBEAT alive for you; PROGRESS is your progress mark.

---

### `merge` — integrate returned branches

Conductor-owned, serial.

1. For each `RETURNED` row, in dependency order: merge `megaloop/<id>` into the
   campaign branch (the default target), run the build/test (`config.testCmd`),
   confirm green.
   *Switchboard mode:* write `PROGRESS phase=merge currentRow=<id>` as each row is
   integrated (ML-6), so a hang shows up on the exact row being merged.
2. **Merging a row branch to `master` is allowed** (operator ruling 2026-07-28)
   **iff BOTH hold**, else it's a merge-target violation:
   (a) **tested** — the lane's FULL suite ran green on the merge RESULT: build
   the merge on a scratch branch off `master`, run `config.testCmd` there (green
   on the row branch pre-merge does not count); and
   (b) **reviewed** — the row's self-swarm-review pass is recorded on the BOARD
   row (review_verdict `passed` / `findings-fixed` / `findings-rejected`).
   Then attest BEFORE moving `master`: append one JSON line to
   `<campaignDir>/.merge-approvals.jsonl` —
   `{"row":"<id>","sha":"<merge-result sha>","tested":"<testCmd> green on <sha>",
   "reviewed":"<verdict>","at":<epoch-ms>}` — and fast-forward `master` to that
   exact SHA. The reference-transaction guard blocks any `master` move whose new
   SHA lacks a fresh attestation; the launcher ledgers attested moves as advisory
   `merge-to-master`, unattested ones as `merge-target-violation`.
3. On conflict or red: stop that row, note it, keep going with the rest; surface
   the blocked merge for the operator.
4. Update `BOARD.md`: `RETURNED` → `MERGED`, append to the **Merge log** with the
   one-line result. Preserve individual branches (per-PR choice later).
5. Never `git push` or open PRs here — that's a `push` gate. Prod deploy stays a
   `deploy` gate. The auto-merge-to-master path above changes neither.

---

### `status` — render the board

No LLM work. Read `BOARD.md` and print: per-wave row table (id, kind, status,
gate_class, branch), the merge log tail, and the open gate queue. Also `git
branch --list 'megaloop/*'` to reconcile branches vs board.

---

### `gates` — show the operator queue

List every `GATED` row grouped by `gate_class` (`deploy-gate` / `push-gate` /
`fleet-gate` / `product-question` / `security-crux` / `secrets/target`), each with
what it's waiting on and what the operator must decide/provide. This is the
"nothing here runs autonomously" queue.

---

### `resume` (default) — re-hydrate and continue

The compaction escape hatch. Read `PROTOCOL.md` + `BOARD.md` + the latest dated
handoff in the campaign dir, restate current wave + open gates in 3–4 lines, then
continue the loop (usually: run the next `wave`, else surface gates). This is what
makes the conductor disposable — after a compact, `/megaloop` alone picks up.

**Switchboard-enabled campaigns** (a `<campaignDir>/INBOX.md` exists — see the
"Switchboard integration" section below) run `resume` as a chat-driven,
lockfile-guarded, heartbeat-emitting **incarnation** with this fixed order:

```
0.  conductor.lock (ML-4)              — if SWITCHBOARD_LOCK_HELD is set, YOU are the
                                         authorized incarnation: proceed. Do NOT pgrep,
                                         do NOT read the lockfile, do NOT stand down.
                                         The launcher owns HEARTBEAT — you write PROGRESS
1.  PROGRESS phase=promote (ML-6)      — first progress mark for this incarnation
2.  drain INBOX.md → BOARD (ML-1)      — promote each line, reply "INB-n → Tn"
3.  consume approvals/ (ML-3)          — re-validate sender, release gated rows once
3b. consume control/ (ML-3b)           — apply operator !retry/!drop records once
4.  PROGRESS phase=wave; run wave       — ONLY if SWITCHBOARD_AUTO_WAVE is set (guard)
5.  PROGRESS phase=merge; run merge      — ONLY if a wave ran; serial, conductor-owned
6.  PROGRESS phase=debrief              — post debrief + gate queue — SKIP IF IDLE (guard)
7.  PROGRESS phase=idle                 — on exit (launcher releases the lock)
```

Each numbered step is specified in **Switchboard integration** below. The steps
are additive: a non-switchboard campaign (no `INBOX.md`) skips them entirely and
`resume` behaves exactly as before.

**Guards that make a cron/`!nudge`-fired incarnation safe to run unattended:**

- **Promotion-only by default (waves gated).** Steps 4–5 (autonomous code waves +
  merges) run **only when `SWITCHBOARD_AUTO_WAVE` is set**. Unset (the pilot
  default) → the incarnation promotes INBOX→BOARD, consumes approvals, and
  debriefs, but leaves promoted rows `TODO` for an **operator-run** `/megaloop
  wave`. No code executes on the repo without a human starting the wave — chat is
  a filing surface, not an auto-exec trigger.
- **Auto-wave fan-out cap.** When `SWITCHBOARD_AUTO_WAVE` is set, the launcher also
  sets **`SWITCHBOARD_MAX_ROWS_PER_RUN`** (default 3). Dispatch **at most that many
  rows** in this incarnation's wave, even if more are dispatchable — leave the rest
  `TODO` for the next incarnation. This bounds token burn per run regardless of
  how the engine spends it (the launcher enforces daily $/run-count budgets and a
  kill-switch around this; see `autowave.py`). Never exceed the cap to "finish
  faster."
- **Idle-quiet.** If the incarnation did **no work** (nothing promoted, no
  approval consumed, no wave/merge, no gate newly queued), it posts **nothing**
  and exits. A cron tick over an empty INBOX + idle board must be silent — else
  the channel gets a "nothing happened" debrief every interval. Post the
  debrief/gate-queue **only when there is activity to report.**

## Switchboard integration (chat-driven conductor)

A campaign can be **switchboard-enabled** — driven from Keybase chat by the
switchboard daemon. The full cross-host contract is
`<meta>/tools/switchboard/PROTOCOL.md`; this section is the conductor's slice of
it. The three switchboard components (dumb listener daemon, ephemeral frontdesk
dispatcher, this conductor) **never call each other** — they cooperate only
through files in the campaign dir (PROTOCOL §11). The conductor stays the single
BOARD writer; it additionally *reads/consumes* the dispatcher-written files and
*posts* to chat.

**Golden rules still hold, unchanged:** BOARD.md single-writer (only the
conductor), hard gates never autonomous, merges serial + conductor-owned. Chat is
a filing/approval surface, never a gate bypass — message *content* never releases
a gate; only a whitelisted `!approve` does (PROTOCOL §6 / D3).

### Detecting switchboard mode

The conductor treats a campaign as switchboard-enabled when
`<campaignDir>/INBOX.md` exists (the daemon/frontdesk create it). In that mode the
incarnation is fired headless (`claude -p "/megaloop resume"`, cron- or
`!nudge`-triggered) and reads its chat context from the environment the launcher
sets (mirrors the daemon⇄frontdesk spawn contract):

| Env var | Meaning | Fallback |
|---|---|---|
| `SWITCHBOARD_BOT_HOME` | the bot's isolated `KEYBASE_HOME` for `keybase -H` | `routes.yaml: bot_home` |
| `SWITCHBOARD_HOST` | host label for `⟦sb:…host=⟧` markers | `routes.yaml: host` |
| `SWITCHBOARD_APPROVERS` | approver whitelist JSON (by gate class) | `routes.yaml: approvers` |
| `SWITCHBOARD_HUMANS` | humans JSON | `routes.yaml: humans` |
| `SWITCHBOARD_CAMPAIGN_DIR` | absolute campaign dir | derive from cwd + route |

The campaign's **channel** (where debriefs/gate-queues/promotions post) is the
`routes.yaml` route whose `campaignDir` matches this campaign.

### Shared on-disk files (PROTOCOL §11)

| File | Owner | Conductor's role |
|---|---|---|
| `<campaignDir>/BOARD.md` | **conductor** | single writer (unchanged) |
| `<campaignDir>/INBOX.md` | frontdesk appends (flock) | **drains** under flock (ML-1) |
| `<campaignDir>/INBOX.seq` | shared | monotonic `INB-<n>` counter (max-seen+1 if absent) |
| `<campaignDir>/approvals/<id>.json` | switchboard daemon creates one per `!approve` (deterministic authz, no LLM) | **consumes once** (ML-3) — EXCEPT `gateClass=fix-and-ship`: leave those; the daemon-fired executor validates + consumes them |
| `<campaignDir>/control/<action>-<id>.json` | switchboard daemon (`!retry`/`!drop`) | **consumes once** (ML-3b) |
| `<campaignDir>/receipts/<id>.json` | **conductor** writes at VERIFY-pass | `{"row","sha","suites":{…counts},"ts":<ms>}` — the machine-checkable proof `integrate-and-ship.sh` requires before a fix-and-ship merge; `sha` = the verified branch head. Also append a `verify-pass` ledger event |
| `<campaignDir>/ledger.jsonl` | any dumb-code actor appends | append `promoted` / `wave-start` / `verify-pass` / `merged` / `wave-fail` one-liners at those transitions (spec §6). **Schema, exactly:** `{"ts": "<UTC ISO8601 'YYYY-MM-DDTHH:MM:SSZ' STRING — never epoch>", "row": "<str>", "event": "<str>", "actor": "conductor", "detail": {<flat str map>}}` — renderers tolerate drift but don't rely on it |
| `<campaignDir>/HEARTBEAT` | **conductor** / engine | **writes** at each step (ML-6) |
| `<campaignDir>/conductor.lock` | flock sentinel | **holds** for the incarnation (ML-4) |
| `<campaignDir>/dispatch-log.jsonl` | daemon (append-only) | read-only (audit); do not touch |

### Posting to chat

The conductor posts by shelling out to keybase with the bot's home — the same
adapter surface the frontdesk uses (PROTOCOL §9). Never build the request with
shell string interpolation; hand keybase a `json.dumps`-built body on stdin:

```
keybase -H "$SWITCHBOARD_BOT_HOME" chat api   # request JSON on stdin
```

Every message the conductor posts is threaded via `reply_to` where a
`threadRoot`/`msgId` exists and **must end with a `⟦sb:…⟧` marker** (PROTOCOL §4)
— this is both the machine-readable structure and the loop guard (a message
carrying a marker is dropped by the daemon at decision step 3, so the conductor
never dispatches its own posts). Marker kinds the conductor emits: `promote`,
`wavestart`, `gatequeue`, `debrief` (and, via the engine/supervisor, `alert`).

**Wave-start announce** (step 4, operator ask 2026-07-11): the moment this
incarnation has CHOSEN its dispatch rows — after the fan-out cap is applied,
before the first row agent spawns — post **one** channel-level line naming
exactly what is about to be worked, so a requester sees pickup immediately
instead of waiting for the end-of-run debrief:

```
🔨 wave starting — working on: T16 (export JPEG all-black fix), T18 (open-chat scroll position) · 2 of 4 dispatchable (cap 3/run)
⟦sb:wavestart inc=3f9a1c host=laptop rows=T16,T18⟧
```

Rules: titles trimmed to ~60 chars; include the left-behind count when the cap
bit (requesters of un-picked rows learn they're queued, not lost). Idle-quiet
still holds — no dispatchable rows ⇒ no announce. Channel-level only (no
`reply_to`): per-row threaded replies remain the debrief's job. The per-row
`wave-start` LEDGER events are unchanged and still appended per row.

**Launcher owns the announce on the auto-wave path (do NOT double-post).** When
`SWITCHBOARD_LOCK_HELD` is set — i.e. you were launched by `conductor-run.py`
(the `--auto-wave` path) — the **launcher** posts the `⟦sb:wavestart⟧` line
deterministically, by polling the BOARD for rows you flip to `DISPATCHED` (it
owns this the same way it owns HEARTBEAT + the `🌊` cost line). So on that path
you do your normal job — write the `DISPATCHED` claims to BOARD *before*
dispatch (megaloop wave step 2) and append the per-row `wave-start` ledger
events — but you do **not** post the wavestart message yourself; the DISPATCHED
claim IS the trigger. Only post it yourself on a **bare in-session run**
(`SWITCHBOARD_LOCK_HELD` unset, no launcher), where there is no launcher to do
it. This keeps the announce reliable — it fires from a deterministic board
transition, not from the LLM remembering a step.

### ML-4 — conductor lockfile (serial-merge protection)

An incarnation must be the only one running for a campaign (protects the
serial-merge golden rule against cron/`!nudge` overlap). The launcher wraps the
incarnation in a non-blocking flock on `<campaignDir>/conductor.lock`:

```
flock -n "$SWITCHBOARD_CAMPAIGN_DIR/conductor.lock" -c 'claude -p "/megaloop resume"'
```

`flock -n` runs the incarnation **only if the lock is free**; if another
incarnation holds it, flock exits non-zero without running — that is the "skip if
held" behavior, and it is exactly what the daemon's `!nudge` relies on (PROTOCOL
§11: "fires an incarnation only if the lock is free"). The lock is held by the
**launcher** (the `flock` wrapper, or `tools/switchboard/conductor-run.py` which
flocks in-process) for the incarnation's whole lifetime and released on exit.

**The conductor (you, the `claude -p` incarnation) must NOT do any lock/liveness
checking** — an LLM cannot hold an fd across reasoning steps, which is *why* the
launcher holds it. Your launcher signals this with **`SWITCHBOARD_LOCK_HELD`**:
- **set** (the normal path, via `conductor-run.py` or a `flock` wrapper) → the
  lock is already held for you. You ARE the authorized incarnation. **Do not run
  `pgrep`, do not read `conductor.lock`, do not look for "another incarnation,"
  do not stand down.** Any running `claude -p /megaloop resume` you could find is
  *yourself*. Go straight to step 1 (HEARTBEAT) and do the work. Detecting and
  killing a stuck sibling is the **daemon supervisor's** job (SB-5), never yours.
- **unset** (a bare manual `resume` with no launcher) → you may `flock -n` the
  lockfile yourself and exit 0 if held, but prefer running via the launcher.

The **launcher** (not you) records `pid`, `launcherPid`, and `incarnationId` in
HEARTBEAT, and keeps it beating, so the supervisor can identify and kill a stuck
holder. Your only liveness duty is PROGRESS (ML-6).

### ML-6 — liveness (launcher HEARTBEAT) + progress (your PROGRESS)

Liveness splits into two signals with two owners — the design that lets a long
wave run without the supervisor false-killing it (a fixed "stale after 10 min"
can't tell a busy `wave` from a hung `promote`):

- **HEARTBEAT — the launcher owns it.** `conductor-run.py` rewrites
  `<campaignDir>/HEARTBEAT` every ~60s for as long as your `claude` process is
  alive. You do **not** write it. It carries `pid`/`launcherPid` (what to kill)
  and `lastBeat` (process is alive).

- **PROGRESS — you own it.** At each phase transition and whenever `currentRow`
  changes, **atomically** rewrite `<campaignDir>/PROGRESS` (temp file in the same
  dir, then `rename` — never a partial write). This is your forward-progress
  mark; the launcher folds its `at` into HEARTBEAT's `progressAt`, and the
  supervisor uses that to tell "busy and progressing" from "stuck". Shape
  (PROTOCOL §11, exact keys):

  ```json
  {"incarnationId":"3f9a1c","phase":"wave","currentRow":"T41","at":1783480343575}
  ```

  - `incarnationId` — read from **`$SWITCHBOARD_INCARNATION`** (the launcher
    generated it; matching it is how HEARTBEAT and PROGRESS correlate). If unset
    (a bare manual run), generate a short hex once.
  - `phase` ∈ `{promote, wave, merge, debrief, idle}` — the seven-step order above.
  - `currentRow` — the row being merged/dispatched right now, else `null`.
  - `at` — epoch **ms**, set to *now* on every write.

Advance PROGRESS at least at each phase transition and whenever `currentRow`
changes; in a wave, advance it as each row/sub-agent starts so a stall lands on
the exact row. The daemon supervisor (SB-5) then reads HEARTBEAT: lock held +
fresh `lastBeat` + `progressAt` advancing → healthy at any duration; lock held +
`progressAt` frozen past the stall window on an active phase → stuck → kill `pid`;
lock free + `pid` alive + `launcherPid` dead → orphan → kill. You never need to
tolerate or overwrite a stale HEARTBEAT — the launcher handles all of that.

### ML-1 — step 0: inbox promotion (INBOX.md → BOARD)

Before computing a wave, drain the inbox. `INBOX.md` is a fenced ` ```jsonl `
block (PROTOCOL §11); each line is
`{inbId, ts, sender, channel, msgId, threadRoot, kindGuess, body}`.

1. Open `<campaignDir>/INBOX.md` under `flock` (the frontdesk appends under the
   same lock; hold it for the whole drain — the conductor is the sole drainer).
2. Parse every JSONL line, in order. For each:
   - **Dedup.** Skip if a BOARD row already records this `inbId`. The conductor
     stamps the source id in the row's Notes as `src=INB-<n>` (see the BOARD
     template) — this makes the drain idempotent across a crash mid-drain.
   - **Finalize kind.** `kindGuess` is the frontdesk's *guess* (D5). The conductor
     decides the real `kind` (`investigate`/`design`/`code`/`chore`, or a gate
     kind `fleet`/`push`/`deploy`) per STATE_MODEL.md.
   - **fix-and-ship intent (phone-loop spec §5).** If — and ONLY if — the body
     carries an explicit ship phrase ("and ship it", "ship this", "get it on my
     phone"), the row is `kind=code` with `gate_class=fix-and-ship`: it runs the
     wave + verify normally, then parks `GATED` (never auto-merges), and one
     whitelisted `!approve` releases MERGE **and** SHIP via the daemon-fired
     executor. Never infer ship intent from urgency/severity — the approval's
     stated scope must match what the requester literally typed. The gate-queue
     line MUST state the widened scope:
     `• T9  fix-and-ship → !approve T9 — MERGES megaloop/T9 + SHIPS TestFlight  (operator)`
   - **Version convention (operator, 2026-07-10):** a fix release bumps semver
     PATCH (the fix-and-ship executor passes `--bump patch`); a feature release
     (deploy row batching new features) bumps MINOR (`ship_command` passes
     `--bump minor`). A plumbing re-cut with no version change is the exception
     — the requester must say so explicitly and the operator runs
     `ship-from-chat.sh` by hand without `--bump`.
   - **Deps + wave placement.** Infer deps + file-set and sequence the row into a
     wave (or the backlog) by the same rules as `gather` (deps satisfied,
     file-sets disjoint). Gate-kind rows go straight to the operator-gate queue.
   - **Allocate a BOARD id** (`T<n>`) and append the row (single-writer), stamping
     `src=INB-<n> chan=<channel> thread=<threadRoot|msgId>` in Notes so the
     debrief (ML-2) can thread the outcome back to the requester.
   - **Post the promotion reply** (PROTOCOL §8), threaded to the line's
     `threadRoot` (or its `msgId` when `threadRoot` is null):
     ```
     INB-7 → T42 (design)  ⟦sb:promote inb=INB-7 row=T42 kind=design⟧
     ```
3. **Rewrite** the JSONL block, removing the consumed lines and keeping any that
   failed to promote (note the failure; retry next incarnation). Safe because the
   conductor holds the flock and is the only drainer.
4. Release the flock.

Ordering for crash-safety: append the BOARD row (with `src=INB-n`) **before**
posting its reply and **before** the final rewrite. A crash after the BOARD append
is recovered next run by the `src=INB-n` dedup; at worst a promotion reply posts
twice, which is harmless (the marker keeps it out of the dispatch loop).

### ML-3 — gate flow (post the queue, consume approvals)

**Consume** (step 3, before the wave, so rows approved since the last run dispatch
this incarnation). Scan `<campaignDir>/approvals/*.json` — each is
`{"id":"T45","approvedBy":"operator","channel":"app","msgId":812,"sentAt":…,"gateClass":"push-gate"}`
(PROTOCOL §6). **Skip `gateClass=fix-and-ship` records entirely** — the daemon
fires `integrate_and_ship_command` on those the moment they're filed; that
executor validates and consumes them itself (spec §5). Consuming one here would
race the executor and could double-release. For every other record:

1. **RE-VALIDATE the sender** against the approver whitelist *for the row's actual
   gate class* (defense in depth — never trust the record's own claim):
   `push-gate`/`deploy-gate`/`fleet-gate`/`security-crux` → `approvers.default`
   (`operator`); `product-question` → `approvers.product-question`
   (`operator`, `teammate`). Also confirm the row's `gate_class` on
   BOARD **matches** the record's `gateClass` (don't let a push approval release a
   product-question row).
2. **If valid:** release the row **exactly once** — flip BOARD status from `GATED`
   to its follow-on (`TODO` so dependent `code` rows dispatch, or `DONE` for a
   decision-type gate), record `approvedBy`+`msgId` in the merge log, and post an
   in-thread confirmation (threaded to the record's `msgId`, ending
   `⟦sb:gatequeue …⟧` or a short `⟦sb:debrief …⟧`).
3. **If invalid** (sender not whitelisted, or `gateClass` mismatch): make **no
   BOARD change** (PROTOCOL §6) and log the rejection (optionally a ⚠️ note).
4. **Delete the approval file** either way — approvals are consumed exactly once
   (PROTOCOL §11). Deleting an already-released duplicate is idempotent.

### ML-3b — control flow (consume operator !retry / !drop)

**Consume** (step 3b, right after approvals, before the wave). Scan
`<campaignDir>/control/*.json` — the daemon files one per operator `!retry <id>`
/ `!drop <id>` (deterministic authz against `humans`, no LLM), shaped
`{"id":"T45","action":"retry","by":"operator","channel":…,"msgId":…,"sentAt":…}`.
For each record:

1. **retry:** if the row exists and its status is a stuck one (`FAILED`,
   `RETURNED`, `BLOCKED`, `DEFERRED`), flip it back to `TODO` so this
   incarnation's wave can dispatch it; append a `retried` ledger event
   (`{"row","event":"retried","actor":"conductor","detail":{"by":…}}` to
   `<campaignDir>/ledger.jsonl`). A row already `TODO`/running/`DONE` → no
   change, note it in the debrief.
2. **drop:** flip the row to `WONTFIX` (note `by`+`msgId` in the Notes cell),
   archive/abandon its `megaloop/<id>` branch if one exists, append a `dropped`
   ledger event. A `DONE`/`MERGED` row → no change, say so in the debrief.
3. **Delete the control file** either way — control records are consumed exactly
   once (PROTOCOL §11), same as approvals. Reply threaded to the record's
   `msgId` with what happened.

These records schedule/deschedule work only — they can NEVER release a gate
(that stays `!approve`-only, §6).

**Post the queue** (in the debrief phase, reflecting rows still `GATED` after the
wave/merge), grouped by gate class with the approve instruction and who may give
it (PROTOCOL §8):

```
Open gates — app
• T45  push-gate    → `!approve T45`   (operator)
• T46  product-question → `!approve T46` (operator / teammate)
⟦sb:gatequeue host=workstation⟧
```

### ML-2 — end-of-incarnation debrief

In the `debrief` phase (step 6) the conductor is the "orchestrator responds in
chat" piece. Post **one** message per project channel this incarnation touched,
summarizing merged / dispatched / failed / open gates (PROTOCOL §8):

```
Switchboard debrief — app (incarnation 3f9a1c)
• merged:      T40, T41
• dispatched:  T42, T43
• failed:      T44 (build red — triage)
• gates:       T45 push-gate → reply `!approve T45`
⟦sb:debrief inc=3f9a1c host=workstation⟧
```

`inc` is the `incarnationId` (same as HEARTBEAT); `host` is `SWITCHBOARD_HOST`.
Then, **per row that carries a `thread=<msgId>` provenance** (from its ML-1
promotion), post a short threaded reply to that `msgId` with just that row's
outcome, so the original requester sees the result in their own thread. Rows with
no `thread` provenance appear only in the single channel-level debrief.

### ML-5 — scheduling (GATED — do not enable here)

Running conductors headless on a timer (systemd/cron every ~30m, quiet hours +
per-day incarnation budget) is **operator-gated** (BOARD HARD GATES; enabled per
project only after the HD-2 pilot). This section defines the incarnation's chat
behavior; it does **not** create or enable any timer.

## Engine note (prototype → Go)

Today the engine is `engine/wave-runner.mjs`, driven by the Workflow tool
(JS-only, runs inside a live session). It is written to be **mechanically
portable to a `megaloop` Go binary** — see `engine/PORT_TO_GO.md` for the
construct-by-construct map (pipeline stage → worker goroutine, `agent()` → `claude
-p --output-format json`, `schema` → struct unmarshal). The BOARD/PROTOCOL
contract is language-independent, so proving it here makes the Go port a
reimplementation, not a redesign. Once ported, `wave`/`gather` can run headless
(cron) and park at gates with no live session.
