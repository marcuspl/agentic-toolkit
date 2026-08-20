# megaloop

**megaloop is a campaign-runner contract that dispatches itemized board
rows to isolated git worktrees for implementation, self-review,
independent verification, and serial merge. It keeps kind, status, gate
class, and review verdict orthogonal, preserves committed work across
worker failure, and blocks protected-branch moves unless the configured
reference gate sees a fresh attestation.**

> **Boundary:** the conductor may be disposable; campaign state is not.
> A commit is durable work, not validated work, and an attestation is a
> gate receipt — not proof of semantic correctness.

## What ships

This is a contract plus a reference implementation, not a service: a
skill specification (`SKILL.md` — the conductor's behavior), a state
model (`STATE_MODEL.md`), an engine script (`engine/wave-runner.mjs`,
with a construct-by-construct map for porting), board/protocol/tech-debt
templates, and the `reference-transaction` hook. Campaign state lives in
plain files under `.claude/<campaign>/` in the target repo.

## The campaign contract

A campaign is a big todo board whose rows are claimed, delegated, and
merged back, with the main session kept thin: all state on disk, per-row
work in ephemeral sub-agents, and one summary object returned per wave.
Golden rules, verbatim from the spec:

- **The board is single-writer** — only the conductor writes it;
  sub-agents return distilled reports.
- **Hard gates never run autonomously** — rows that push, deploy, or
  mutate live infrastructure park in an operator queue.
- **Merges are serial and conductor-owned.**
- **Never fabricate** — an engine error stops the row; it does not
  invent a result.

## Four state axes, not one enum

```
kind × status × gate_class × review_verdict
```

A row is one point in that space. `status=GATED` with
`review_verdict=findings-fixed` — *reviewed AND blocked* — is a state a
linear status collapses, and merges must see both facts independently.
`kind` decides dispatchability (code/investigate/design run autonomously;
fleet/push/deploy never do); `gate_class` says why an operator is
needed; `review_verdict` carries the review outcome, including
`findings-rejected` *with its rationale*.

## Commit-first durability — and its boundary

Sub-agents commit their branch **before** the slow test and review
steps, so a run killed mid-wave leaves durable branches the next
incarnation can verify and merge instead of lost uncommitted work.

State the boundary plainly, because the recorded incident sits exactly
on it: commit-first protects work only **after** it crosses the commit
line. The documented pre-commit failure — environment setup consumed an
entire run budget before anything landed — is owned by a different
control, pinned build recipes (`BUILD-RECIPES.md`: proven setup
commands, inherited by every later run). The two controls split the
timeline at the commit.

## The merge gate, precisely

Merging a row branch to a protected branch requires, per the spec: the
lane's full suite green **on the merge result** (green on the row branch
pre-merge does not count), and the row's review verdict recorded. The
attestation is one JSON line appended to
`<campaignDir>/.merge-approvals.jsonl`:

```json
{"row":"T30","sha":"<merge-result sha>","tested":"<suite> green on <sha>","reviewed":"passed","at":<epoch-ms>}
```

The `reference-transaction` hook then enforces it. Its exact scope —
documented so the guarantee is not overstated:

- It vetoes updates to `refs/heads/master|main` at git's *prepared*
  phase **while an autonomous-wave sentinel is live** (a sentinel file
  the launcher maintains; stale sentinels older than 3 h are ignored so
  a crashed launcher cannot wedge the branch).
- The attested `sha` must **equal the new ref value** — that equality is
  what encodes "tested on the merge result": only the tested commit
  itself may become the branch head.
- "Fresh" means the attestation's `at` is under 3 hours old, and
  `tested`/`reviewed` are non-empty.
- Outside its scope: manual merges when no autonomous wave is active,
  clones without the hook installed, and administrative paths. It is an
  interlock against unattended mistakes, not repository-wide branch
  protection.

## Recovery and replay

The conductor is disposable by design: any incarnation re-reads the
board, protocol, and latest handoff, and continues — including verifying
and merging branches a previous incarnation left behind. A recorded
recovery exists for the pre-commit case too (work salvaged from an
abandoned worktree by the next session), but that path is manual;
orphaned-worktree handling is a named gap, not a shipped policy.

## Scope — explicitly out

- Protection of uncommitted work; semantic correctness of committed
  work; automatic rollback.
- Multi-host synchronization and push — pushing is a separate operator
  gate, and merged-but-unpushed work being invisible across machines is
  a documented open hole.
- General-purpose project management. The board is a dispatch contract,
  not a tracker.

## Where this fits

megaloop is the *durable execution* mechanism of a three-part set —
[swarm-review](../swarm-review/) supplies its per-row self-review and
independent verification; [switchboard](../switchboard/) is the
deterministic chat front door that files work onto boards like these.
Each stands alone; they share a doctrine, not a runtime. The companion
essay, *The Accountability Slide Was Wrong*, covers the
control-must-own-the-failure correction that reshaped this README's
durability section.
