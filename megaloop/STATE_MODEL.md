# Megaloop state model — four orthogonal axes

A row is one point in `kind × status × gate_class × review_verdict`. Keep them
separate; don't collapse into one enum.

## Axis 1 — `kind` (what the work IS → how it's dispatched)

| kind          | meaning                                   | autonomous?              |
|---------------|-------------------------------------------|--------------------------|
| `investigate` | read-only research → cited report         | ✅ auto (no mutation)    |
| `design`      | produce a plan / spec / ADR, no code      | ✅ auto (output → gate)  |
| `code`        | implementation in an isolated worktree    | ✅ auto (→ branch)       |
| `chore`       | board / memory / doc housekeeping         | ✅ auto (conductor)      |
| `fleet`       | live infra mutation (CA/relay/registry)   | ⛔ **gate**              |
| `push`        | git push / PR open                        | ⛔ **gate**              |
| `deploy`      | ship a built artifact to a running host   | ⛔ **gate**              |

`fleet`/`push`/`deploy` are the hard gates — never dispatched, never done
without operator go-ahead.

## Axis 2 — `status` (lifecycle — the state machine the engine drives)

```
TODO ─▶ BLOCKED(unmet dep) ─▶ CLAIMED/DISPATCHED ─▶ IN-PROGRESS ─▶ SELF-REVIEW ─▶ RETURNED ─▶ MERGED ─▶ DONE
                                                                        │
                          off-ramps: GATED · DEFERRED(→TECH_DEBT.md) · WONTFIX · FAILED(triage)
```

- `TODO` — defined, not started.
- `BLOCKED` — an unmet dep; engine won't dispatch.
- `CLAIMED`/`DISPATCHED` — an agent owns it (claim recorded in BOARD before spawn).
- `IN-PROGRESS` — agent working.
- `SELF-REVIEW` — agent running its own swarm-review + fixes.
- `RETURNED` — branch ready + distilled report; awaiting conductor merge.
- `MERGED` — integrated into the campaign branch (or `master` via the
  tested+reviewed auto-merge path in SKILL `merge` step 2), green.
- `DONE` — terminal success (for non-code kinds too).
- `GATED` — auto-complete as far as possible; parked in the operator-gate queue.
- `DEFERRED` — consciously spotted-not-now; moved to `TECH_DEBT.md`.
- `WONTFIX` — decided against, with reason.
- `FAILED` — agent errored / couldn't complete; needs triage.

## Axis 3 — `gate_class` (only when `status = GATED` → why the operator is needed)

| gate_class        | meaning                                                        |
|-------------------|----------------------------------------------------------------|
| `deploy-gate`     | ship an artifact to a running host                             |
| `push-gate`       | git push / open a PR                                            |
| `fleet-gate`      | mutate live infra (CA/relay/registry, backfill, dereg)         |
| `product-question`| a design/UX decision only the operator can make                |
| `security-crux`   | sensitive change to hold for careful handling                  |
| `secrets/target`  | needs credentials or an external target (e.g. off-host backup) |

## Axis 4 — `review_verdict` (per RETURNED `code` row → swarm/codescene outcome)

| verdict             | meaning                                                       |
|---------------------|---------------------------------------------------------------|
| `pending`           | not yet reviewed                                              |
| `passed`            | clean                                                          |
| `findings-fixed`    | real findings surfaced and fixed                              |
| `findings-rejected` | findings consciously rejected — **carries the rationale**     |
| `codescene-flagged` | a Code Health regression to weigh before merge                |
