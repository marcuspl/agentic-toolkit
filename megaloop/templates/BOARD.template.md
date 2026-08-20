# <CAMPAIGN> Megaloop — Board (sequenced plan + live work registry)

Conductor-owned single-writer file. The conductor writes claims/status at dispatch
and on completion; sub-agents do NOT write here (they work in isolated worktrees
and report back). See PROTOCOL.md. State vocabulary: STATE_MODEL.md.

Started <YYYY-MM-DD>. Base branch: `<base>`. Campaign branch: `megaloop/<slug>`.

## Legend
Status: `TODO` · `BLOCKED` · `DISPATCHED` · `SELF-REVIEW` · `RETURNED` · `MERGED`
· `DONE` · `GATED` · `DEFERRED` · `WONTFIX` · `FAILED`.
Kind: `investigate` · `design` · `code` · `chore` · `fleet`⛔ · `push`⛔ · `deploy`⛔.

Switchboard provenance (rows promoted from chat via `INBOX.md`, megaloop ML-1):
the Notes column carries `src=INB-<n> chan=<channel> thread=<msgId>` — `src` makes
the inbox drain idempotent (dedup key), and `chan`/`thread` let the end-of-run
debrief (ML-2) reply the row's outcome back into the requester's chat thread.

## HARD GATES (never autonomous)
- Any live-infra mutation (`fleet`): CA/relay/registry changes, backfills, deploys.
- Any `git push` / PR open (`push`).
- Deploying any built artifact to a running host (`deploy`).
The loop grinds all `code`/`investigate`/`design` work to RETURNED/MERGED, then
STOPS at gates and surfaces them.

## Wave 1
| ID | Item | Kind | Deps | Files | Status | Branch | Verdict | Notes |
|----|------|------|------|-------|--------|--------|---------|-------|
| <id> | <one-line> | code | — | <file-set> | TODO | megaloop/<id> | pending | |

## Backlog (sequenced; scheduled after earlier waves)
| ID | Item | Kind | Deps |
|----|------|------|------|
| <id> | <one-line> | code | <dep-ids> |

## Merge log (campaign branch: megaloop/<slug>)
- <id> (`megaloop/<id>`) — <one-line result>. MERGED; build/test green.

## Operator-gate queue (nothing here runs autonomously)
- **<gate-id> — <gate_class>:** <what it's waiting on / what the operator decides>.

## Deferred → TECH_DEBT.md
Spotted-not-now items are logged in TECH_DEBT.md, not here.
