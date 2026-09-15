# Switchboard — `routes.yaml` schema (SB-0b)

Per-host configuration for the listener daemon: which channels this host owns,
where each one routes, and who is trusted. **One `routes.yaml` per host.** The
live file lives at `~/.config/switchboard/routes.yaml` (outside the repo — it
names a host's bot identity and local paths); a committable, value-free example
is `routes.example.yaml` next to this doc.

Companion: `PROTOCOL.md` (message contract, SB-0c),
`../../.claude/switchboard/BOARD.md` (decisions D1–D7).

---

## Top-level keys

| Key | Type | Req | Meaning |
|---|---|---|---|
| `version` | int | ✓ | schema version; `1` today |
| `host` | string | ✓ | host label, e.g. `workstation` / `laptop`; used in `⟦sb:…host=⟧` markers |
| `team` | string | ✓ | Keybase team; `acme_agents` |
| `bot` | string | ✓ | Keybase username this daemon logs in as (`bot_workstation` on this host; `bot_laptop` on the mac). The daemon drops its own messages by this name (loop guard, PROTOCOL §7). **Live dispatch stays gated until the bot is a team member (SB-0a team-add).** |
| `bot_home` | path | ✓ | the bot's isolated `KEYBASE_HOME` (`keybase -H <bot_home> …`), so the daemon never collides with an interactive `operator` session on the same box. `~/.config/switchboard/kb-<host>/`. |
| `humans` | [string] | ✓ | usernames with free-form dispatch + (subset) approval rights (PROTOCOL §5) |
| `approvers` | map | ✓ | who may `!approve` / control the daemon, by gate class (PROTOCOL §6) |
| `state_file` | path | ✓ | dedup cursor store (last-seen msgId per conv); sits next to this file |
| `defaults` | map | – | fallback per-route settings |
| `routes` | [route] | ✓ | the channel table (below) |

### `approvers`

```yaml
approvers:
  default: [operator]                       # push/deploy/fleet/security + !pause/!resume/!nudge
  product-question: [operator, teammate]
```
Keys are gate classes (from the megaloop state model); `default` covers every
class not otherwise listed and the daemon-control commands. A sender not on the
relevant list is ignored for that action (no state change).

### `defaults`

```yaml
defaults:
  concurrency: 2                 # max concurrent frontdesk dispatchers per host (SB-3)
  permission_profile: read-mostly # frontdesk small-task profile; mutating tracked files ⇒ demote to dev-task (FD-3)
  dispatcher_model: null          # optional claude -p model override; null = default
  cron_cadence_min: 30            # conductor timer cadence (min); supervisor flags "cron broken"
                                  # if no incarnation ran in >2× this. Only meaningful once ML-5
                                  # timers are enabled; default 30.
  # supervisor liveness (design D, SB-5) — optional; shown with defaults
  liveness_stale_sec: 180         # lock held but no launcher beat this long → wedged → kill
  progress_stall_min: 30          # active phase, no forward progress this long → stuck LLM → kill
  # --auto-wave safety harness (autowave.py) — optional; shown with defaults
  autowave_daily_usd: 15.0        # daily $ ceiling; over → auto-wave downgrades to promotion-only
  autowave_max_runs_day: 20       # daily auto-wave incarnation cap (hard refuse)
  autowave_max_rows_per_run: 3    # max rows one wave incarnation may dispatch (fan-out cap)
```

**Auto-wave guardrails** (`autowave.py`, enforced by `conductor-run.py`): beyond
the config caps, an instant **kill-switch** file `AUTOWAVE_OFF` (in the campaign
dir or `~/.config/switchboard/`) forces promotion-only while present, and every
auto-wave run's cost is recorded to `autowave-ledger.jsonl` (next to `state_file`)
and posted to the channel. `!status` shows today's spend vs caps.

---

## Route entries

Each `routes[]` entry binds one channel to a behavior. Channels not listed here
are **not listened to** on this host (that is how disjointness across hosts is
enforced, D4 — see invariants).

| Field | Type | Req | Meaning |
|---|---|---|---|
| `channel` | string | ✓ | Keybase `topic_name`, no `#` (e.g. `app`) |
| `mode` | enum | ✓ | `dispatch` \| `route` \| `command-only` \| `capture` (below) |
| `repo` | path | mode=dispatch | working dir the frontdesk dispatcher runs in (cwd) |
| `campaignDir` | path | mode=dispatch | megaloop campaign dir, **relative to `repo`**; holds `INBOX.md`, `BOARD.md`, `approvals/`, `HEARTBEAT` |
| `targets` | [target] | mode=route | classification → destination table (below) |
| `sink` | path | mode=capture | directory captured messages + payloads are appended to |
| `concurrency` | int | – | per-route override of `defaults.concurrency` |

### `mode`

- **`dispatch`** — full frontdesk. Human free-form + `!ask`/`!task` are
  classified (question / small-task / dev-task) and handled in `repo`; dev-tasks
  land in `campaignDir/INBOX.md` with a receipt. Requires `repo` + `campaignDir`.
- **`route`** — no local repo. The dispatcher only classifies the *target* and
  forwards: to a local repo's INBOX, or cross-host by posting `!task …` into the
  target channel (D4). This is `#product-chat`. Requires `targets`.
- **`command-only`** — act *only* on `!task`/`!ask` (even from humans); ignore
  free-form. For cross-agent chatter channels (`#agent-sync`) if/when they are
  listened to. Requires `repo`+`campaignDir` (for `!task`) or `targets`.
- **`capture`** — an inbox, not a front door. Every message is appended verbatim
  to `sink/capture-YYYY-MM.md` and acked 📥; attachments download to
  `sink/attachments/` and are linked from the entry. **Nothing here ever spawns
  an agent** — not even `!task`, which in a capture channel is just an idea that
  happens to start with that word. Daemon commands (`!status`, `!pause`) still
  work. Requires `sink`.

  *Why a fourth mode.* Capture and triage want opposite things. Capture must be
  instant, free, and lossless — you are thumbing an idea in from a train, and
  the cost of losing it is the whole idea. Triage wants to read a month of them
  together, and is fine being batched and deliberate. The other three modes fuse
  the two, so every captured fragment would spend a `claude -p` classifying it in
  isolation — the most expensive way to read the least context. Splitting them
  makes capture free and lets triage see the pile.

  *Verbatim is load-bearing.* The first sink (`~/dev/<repo>/raw`) is a
  directory whose README calls it "Marcus's own words, verbatim — source of
  truth, don't edit for polish." A capture that summarised on the way in would
  destroy exactly the property that makes it worth capturing. No LLM is on this
  path by construction.

### `targets` (mode=route only)

Ordered match list; first match wins, else the dispatcher asks in-thread (FD-6).
```yaml
targets:
  - match: app-ios        # keyword/heuristic the dispatcher classifies to
    via: channel                 # forward by posting `!task` into another channel
    channel: app-ios      #   (cross-host: laptop's daemon picks it up)
  - match: app
    via: inbox                   # file directly into a local repo's campaign INBOX
    repo: ~/dev/new/app
    campaignDir: .claude/<campaign>
```
`via: channel` is the **only** cross-host mechanism — chat is the transport, no
shared filesystem (D4). The forwarding dispatcher is the one exception to
"dispatchers don't post to other channels" (FD-6).

---

## Invariants (enforced/validated)

1. **Disjoint channels across hosts.** A given `channel` appears in exactly one
   host's `routes`. workstation owns `{app, platform, product-chat, resume,
   artilect-id(later)}`; laptop owns `{app-ios}`. Overlap = a message
   processed twice (D4). This is a fleet-wide invariant the schema can't check
   alone — keep it true by convention and review.
2. **`route` never dispatches locally**; it only classifies + forwards. Only
   `dispatch`/`command-only` spawn a frontdesk that touches a repo.
3. **`campaignDir` is relative to `repo`** so the daemon can `cd repo &&` resolve
   campaign state uniformly.
4. **`bot` ≠ a human in `humans`.** The daemon must be able to tell itself apart;
   once SB-0a lands, `bot` is the per-host account and this holds. Under the
   shared `operator` identity it does not — hence live dispatch is gated
   (PROTOCOL §7).
5. **Unlisted channels are silent.** `#general` and any channel not in `routes`
   are never listened to. Silent-by-design and silent-by-neglect look identical
   from inside the channel, so declare the deliberate ones in top-level
   **`unrouted_ok:`**; `digest.py` diffs `keybase chat list-channels` against
   `routes` ∪ `unrouted_ok` and reports the remainder. Four channels sat unrouted
   for weeks (2026-07→08) while people posted into them.

6. **A routed channel with no daemon is worse than an unrouted one**, because
   everyone believes it is heard. `digest.py` tests this directly — it tries to
   take each `daemon.<channel>.lock`, and a lock it can take is a channel nobody
   is listening to — and an unhealthy result **defeats the digest's idle-quiet
   fingerprinting**, because a persistently dead front door renders an identical
   digest every day and would otherwise be suppressed into silence.
