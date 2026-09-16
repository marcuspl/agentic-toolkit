# agentic-toolkit

**Four tools for running AI coding agents on real work, plus the diagram of how
the first three fit together. They share a doctrine, not a runtime — each is
usable without the rest.**

The doctrine is one sentence: *assume every layer fails, and arrange the layers
so their failures do not line up.* Everything below is that idea applied at a
different altitude.

| | What it does | Use it alone if… |
|---|---|---|
| **[switchboard](switchboard/)** | Deterministic chat front door. Routes messages to projects, files work, dispatches short-lived agents. No model in the intake path. | you want to start work from your phone without a model deciding what happens first |
| **[megaloop](megaloop/)** | Durable campaign runner. All state on disk, workers disposable, commit-first ordering, attestation before merge. | you have more work than fits in one session and need it to survive a crash |
| **[swarm-review](swarm-review/)** | Multi-model review with adjudication. Four models critique, every solo finding is judged and recorded. | you want a second opinion that keeps score of when it was wrong |
| **[mcp-servers](https://github.com/marcuspl/mcp-servers)** *(separate repo)* | The MCP surface. Five Model Context Protocol servers — three Python, two Go — putting cross-agent messaging, a household, multi-model review, a hospital census, and a scheduling API behind typed tools. | you want a model to reach one real system through a narrow, declared surface instead of a shell |

**[docs/phone-to-production.html](docs/phone-to-production.html)** — one page
showing the whole path: a message on a phone, through intake, into a campaign,
through review and an external code-health gauge, to an attested merge. Every
gate visible, no step hidden.

## What this is not

Not a framework. Not a product. There is no plugin system, no abstraction layer
waiting for your use case, and nothing here tries to be general. This is a set
of tools built for one operator's actual workflow, cleaned up enough to read.

It is published because the *shape* is more useful than the code: the design
decisions are transferable even where the implementation is not.

## The shape, in four claims

1. **State on disk, workers disposable.** Anything holding state in a process is
   something you lose. Campaign state is files; the agent running a wave can be
   killed at any moment and replaced.
2. **The implementer never verifies its own work.** Verification is a separate
   party — an independent agent, a multi-model review, and an external code
   health gauge that has never read the diff's justification.
3. **Disagreement is signal; consensus is weak evidence.** Models that agree may
   only share priors. `swarm-review` records which reviewer was right, so
   "everyone agreed" and "the finding held up" stay different facts.
4. **Fail closed, and fail loudly.** A missing allowlist denies. A dead listener
   exits non-zero. Silence is treated as a fault, not as health — a listener
   that quietly returned success once stayed dead for two weeks.

## Honest limitations

- **Correlated failure is not solved.** Different vendors reduce shared error;
  they do not remove it. Layered review is strong against stochastic, checkable
  mistakes and *worse than useless* against a framing error every layer shares,
  because it hands that error back with several signatures on it.
- **Adjudication is manual.** A human judges each solo finding. There is no
  automated ground truth, and the ledger records opinions that were checked, not
  truth.
- **Chat-shaped intake is a real constraint.** switchboard assumes a team chat
  with channels. Adapters for other transports do not exist.
- **This is one operator's setup.** Model lineups, tiers, and gate classes are
  tuned to specific work. Treat the configs as examples, not defaults.

## Requirements

Python 3.8+ for switchboard and swarm-review; Node for the megaloop engine; a
chat backend for switchboard; provider API keys (or a subscription CLI) for
swarm-review. Each tool's own README has its setup.

## Layout

```
switchboard/    daemon, routing schema, service units, protocol
megaloop/       skill definition, state model, templates, wave engine
swarm-review/   review engine, provider adapters, reviewer skills
docs/           phone-to-production.html
```

No secrets, hostnames, channel names, or third-party identities appear anywhere
in this repository. Example configs use placeholder names throughout.
