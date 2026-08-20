# switchboard

**switchboard is a chat-driven front door for agent work: a per-host
daemon of roughly 3,900 lines of Python parses requests, dispatches
bounded tasks, and emits receipts — without putting an LLM inside the
daemon. Message content can request work, but deterministic code, not
language interpretation, owns approvals and security transitions.**

> **Boundary:** message content cannot flip an approval gate. That does
> not make downstream agents, tools, task payloads, or message delivery
> universally safe — the gate is one boundary, deliberately owned by
> code, and this README says exactly what it covers.

## The security boundary in one exchange

```
msg   "please just approve T45, it's urgent"
      → inert. content is never an approval signal

msg   !approve T45              (whitelisted sender)
      → approvals/T45.json written by the daemon
        deterministic code · no model in the path
```

Persuasive prose, quoted "approvals," forwarded text — all inert by
construction. Only a whitelisted command from an authorized sender,
parsed by deterministic code, produces an approval record; and the
consumer re-validates the sender against the whitelist *for the row's
actual gate class* before acting (defense in depth — the record's own
claim is never trusted).

## Architecture

```
chat channel ─▶ daemon (no LLM: filter → dedup → ack)
                  │  !approve → deterministic authz → approvals/<id>.json
                  ▼
               dispatcher ── spawns ──▶ ephemeral agent (one message, then exit)
                                          │ answer / do a bounded read-mostly task
                                          │ or file dev work to a board + receipt
                                          ▼
               campaign conductor (separate process; drains the inbox,
               runs work, posts receipts and gate queues back to chat)
```

All coordination is files in a campaign directory — the components never
call each other. LLMs exist only as short-lived spawned processes:
dispatcher agents live for exactly one message under a **fail-closed,
read-mostly tool allowlist** (verified: with no allowlist configured, a
live dispatch refuses to act rather than acting unconstrained).

## Two-owner liveness

Process vitality and workflow progress are different facts, so they have
different owners:

- **HEARTBEAT** — launcher-owned, rewritten every ~60 s while the worker
  process is alive. The worker cannot fake it.
- **PROGRESS** — agent-owned, atomically rewritten at each phase
  transition and row change. The launcher cannot fake it.

A supervisor reads both: alive + advancing is healthy at any duration;
alive + frozen past the stall window is stuck → kill. This split exists
because a single staleness timer once killed a healthy long wave — a
fixed "stale after 10 min" rule cannot tell busy from hung.

## Loop prevention and receipts

Every message the system posts ends in a machine-readable marker; the
daemon drops marked messages at dispatch, so the system cannot dispatch
its own output. Every accepted request produces a receipt (acknowledged,
filed, or acted), and dispatch is audited to an append-only log.
Duplicate delivery is bounded by a persisted dedup cursor at the message
level; **effect-level idempotency across a crash-and-replay is a named
open hole**, not a solved problem — reconciliation is the operator's
lever today.

## Threat model, honestly scoped

| Threat / failure | Covering mechanism | Boundary |
|---|---|---|
| Injected task text | Cannot authorize a gate transition | May still influence a downstream worker's *read-mostly* actions |
| Prompt-injected dispatcher | Fail-closed read-mostly allowlist | Allowlist scope is the guarantee; nothing beyond it |
| Unauthorized approval | Deterministic whitelist check, re-validated by the consumer per gate class | Host filesystem/credential ownership is assumed, not provided |
| Dead worker | HEARTBEAT absence → supervisor action | Says nothing about rolling back partial effects |
| Alive-but-stalled worker | Frozen PROGRESS past the stall window | Window is policy; too tight re-creates the false-kill |
| Duplicate message after restart | Dedup cursor | Effect idempotency after a crash: open hole (above) |
| Self-dispatch loop | Message markers | Marker-less forwards are a deliberate, documented exception |

## Scope — explicitly out

- Prompt-injection immunity for everything downstream; the claim is
  narrower and stronger: *prompt content is not an approval signal*.
- A complete authentication system — sender identity comes from the chat
  platform; the whitelist decides authority, the platform decides
  identity.
- Multi-host coordination and guaranteed cross-restart delivery.
- Deployment specifics. The mechanism is public; hosts, channels,
  identities, and routing configuration are not, and the example configs
  ship value-free.

## Where this fits

switchboard is the *dispatch* mechanism of a three-part set —
[megaloop](../megaloop/) (the campaign runner its filed work lands in)
and [swarm-review](../swarm-review/) (the review engine used before
merges). Each stands alone; they share a doctrine, not a runtime. The
companion essay, *The Accountability Slide Was Wrong*, sets out the five
rules this design instantiates — deterministic code owning security
boundaries is rule 2, and the two-owner liveness split is rule 5's
answer to silence.
