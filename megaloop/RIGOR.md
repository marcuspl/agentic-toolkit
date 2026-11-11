# Rigor profiles: how much testing and review a campaign buys

**Status: proposed (2026-11-11).** Not yet read by the engine. Until it is, a conductor resolves the profile by hand at `gather` and records it in the BOARD front matter. The two review profiles in SKILL.md (`swarm`, `receipts`) become one knob of this.

## Why

Rigor so far was set per campaign, by hand, and usually at the heaviest level. Measured on Human Touch over four days (2026-11-06 to 10):

- ht-ledger made 165 commits on main for 33 merges, and 98 of them mention verify, bounce or FAIL.
- ht-foundation: 86 commits, 53 of them review churn, 18 merges.
- 74k lines of Rust test code against 83k lines of source.

Most of the effort went into proving rows nobody would miss if they broke, in a project with no users that rewrites its formats freely. The fix that worked (2026-11-10) was to scale rigor to risk:

- full refute-verify only for the security core;
- single-pass review everywhere else;
- bigger, whole-feature rows;
- business-rule and scene tests instead of unit tests that restate the implementation.

This file makes that a setting instead of a rediscovery.

## The model

Rigor for a row comes from **three questions about the work**. Pacing comes from **one question about the machines**:

```
row rigor  = f( risk tier of the row , kind of build-out , project stage )
pacing     = f( machine capacity )
```

- **Risk tier sets the floor.** A tier-1 row gets at least the tier-1 minimum, whatever the stage or kind.
- **Stage sets the default** for everything above that floor.
- **Kind shifts which tests pay**, not how many: rule tests for domain logic, scenes for UI, vectors for formats.
- **Capacity only changes when and where proofs run**, never which proofs are required. A small machine defers heavy gates to the milestone and sends them to a gate runner; it doesn't drop them.

The conductor resolves this once per row at dispatch and writes the result in the row's Notes (`rigor: t3/early/ui → single-pass, scenes, light gate`). The builder and the verifier read their brief from that line.

## The knobs (what gets resolved)

| Knob | Values, light → heavy |
|---|---|
| **row_size** | `slice` (one function or fix) · `feature` (a whole user-visible feature, tested at its edge) · `epic` (several features, one design) |
| **review** | `none` · `self` (the builder's own checklist) · `single-pass` (builder receipts plus a conductor spot-check of one receipt) · `verify` (a fresh agent that didn't write it, prompted to refute) · `verify+mutants` (a verify PASS needs a mutation receipt per requirement) · `swarm-on-design` (multi-model review of the design doc before code, operator present) |
| **tests** | any of `edge` (public API or CLI round trip) · `rules` (business-rule tables) · `scenes` (scripted user flows) · `vectors` (byte-pinned format fixtures) · `props` (property tests) · `unit` (only for subtle logic) · `mutants` (mutation testing) |
| **gate_cadence** | `light-per-row` (the row's own crate or package) · `full-per-wave` · `full-per-milestone` · `full-per-row` |
| **bounces** | how many verify FAILs a row may take before it parks for a ruling: `0` · `1` · `2` |
| **asks** | `lean-provisional` (apply the lean, log it PROVISIONAL, carry on) · `default-at` (apply the lean after N hours) · `stop` (wait for the operator) |
| **uat** | `none` · `per-campaign` · `per-wave` |
| **code_health** | `off` · `no-drop` (the score must not fall) · `no-drop + refactor-whole-file` |

These never vary, whatever the profile:

- **Hard gates.** Push and deploy are never autonomous.
- **Formats change only with an ADR and vectors.**
- **No LLM in the gate path.**
- **Never fabricate a result.**
- **Receipts are honest.** A claim without one is a hypothesis.

## Input 1: risk tier (per row)

| Tier | What | Floor |
|---|---|---|
| **1: core** | Identity and keys, signed formats and wire schemas, policy and permission decisions, money math, published identifiers (URLs, topic names, event schemas), tenancy and security boundaries, ordering guarantees | review `verify+mutants`; tests include `vectors` or `props` where they apply; bounces 2; asks `stop`; `swarm-on-design` for a new design |
| **2: contract** | Shared data model, cross-campaign APIs, persistence layout, anything other rows build on | review `single-pass`, with `verify` if it touches a tier-1 file; tests `edge` plus `rules` |
| **3: feature** | Everything user-facing or internal that a later row could rewrite cheaply | review `single-pass`; tests at the edge |
| **4: spike** | Throwaway exploration, prototypes, measurements | review `none`; a receipt of what was learned |

The tier is a property of the files and the contract, not of how hard the row feels. `gather` tags it from a per-project path list: `tier1_paths:` in the rigor block, for example `crates/ht-identity/**`, `spec/vectors/**`. A row touching any tier-1 path is tier 1.

## Input 2: kind of build-out (per campaign, a row may override)

The kind decides which tests pay off. It barely changes how much review there is.

| Kind | Tests that pay | Tests that don't | Notes |
|---|---|---|---|
| **greenfield product** | `edge`, a few `scenes` | unit tests per function; mutants | Rows are `feature` sized. Expect rewrites; tests that lock in internals cost twice. |
| **domain / business logic** | `rules` (tables of input → decision), `scenes` | plumbing unit tests | Rule tests are the specification. Keep them when the code is rewritten. |
| **UI** | `scenes` (Playwright), a visual check per wave, `smoke` | snapshot tests of markup | UI is the heaviest to gate. Run it per wave or per milestone, on a gate runner. |
| **platform / tooling / scripts** | `edge` (run the script against a temp repo), one real run | unit tests of shell | Single-pass is enough. Receipts are a real run's output. |
| **library / format / protocol** | `vectors`, `props`, `edge` | — | Usually tier 1 by path. |
| **brownfield / enterprise** (existing users, other teams' code) | characterization tests around what you touch, `edge`, regression `scenes` | rewriting the existing suite | Bounces matter more than coverage. Never break the existing suite's meaning. |
| **data migration** | before/after counts, a dry run on a copy, rollback rehearsal | — | Tier 1 if the data can't be regenerated. |
| **research / spike** | none, just a written finding | everything | Tier 4. |

## Input 3: project stage (per project, re-asked at each stage change)

| Stage | Signals | Default above the tier floor |
|---|---|---|
| **explore** | No users; the idea isn't settled | `row_size: epic`, review `self`, tests `edge`, gate `full-per-milestone`, asks `lean-provisional`, uat `none` |
| **early dev** | No users; formats rewritten freely, no migrations | `feature`, `single-pass`, `edge` plus the kind's tests, `light-per-row` plus `full-per-milestone`, `lean-provisional`, `per-campaign` |
| **dogfood / pilot** | The operator or a pilot team relies on it daily | `feature`, `single-pass` (`verify` for tier 2), the kind's tests plus regression `scenes`, `full-per-wave`, `default-at` 2 h, `per-wave` |
| **production** | Real users and data; a break costs someone | `slice` or `feature`, `verify` for tier 2 and up, `full-per-row`, `default-at`, `per-wave`, code health `no-drop` |
| **maintained / enterprise** | Many contributors, compliance, long-lived | `slice`, `verify` by default, change records, `full-per-row`, `stop` for anything non-trivial |

Moving up a stage is a moment to re-ask, not to turn everything up at once. Raise tier 2 first, then the gate cadence.

## Input 4: machine capacity (per host, pacing only)

| Setting | Effect |
|---|---|
| `heavy_slots` (concurrent cargo, npm or Playwright jobs) and `jobs_per_slot` | How many builders can build at once. On Human Touch: Linux 2 × 4 threads across all campaigns; Mac 2 × 4. |
| `max_row_agents` | How many builders are dispatched at once (Linux 2 per campaign, Mac 6). |
| `gate_runner` | Where full gates run. With a remote runner, `full-per-wave` costs the home box nothing, so a capacity-starved home box can still afford it. |
| `ui_gate_host` | Where Playwright runs. UI is the single heaviest gate. |
| `quiet_hours` | When long runs may start without the operator present. |

Rules:

- **Capacity never removes a proof.** When the box is starved, the proof moves to a later cadence (per row → per wave → per milestone) or to the gate runner.
- **A starved box narrows parallelism before it narrows gates.** Fewer builders at once, the same rigor each.
- **Measure, don't guess:** take load and memory during a full gate before setting the slots. On the M5 Mac a full UI gate ran at load about 3 on 10 cores with 71% memory free, so it could take a second campaign.

## Precedence

1. The "never vary" list.
2. The tier floor: the row's tier minimum.
3. A per-row override written by the operator (`rigor:` in the row's Notes).
4. The kind's test choice.
5. The stage default.
6. Capacity, for pacing only.

When the inputs disagree, the heavier review wins and the cheaper cadence wins. Example: a tier-1 row in an explore-stage project still gets `verify+mutants`, but its full gate can wait for the milestone, unless it merges to main.

## Config shape (BOARD front matter)

```yaml
rigor:
  stage: early-dev            # explore | early-dev | dogfood | production | maintained
  kind: greenfield            # default kind for rows; a row may set kind: in its Notes
  tier1_paths: [crates/ht-identity/**, crates/ht-record/**, crates/ht-policy/**, spec/vectors/**]
  tier2_paths: [crates/ht-store/**, web/src/lib/api/**]
  overrides: {}               # knob: value, campaign-wide, operator-set
capacity:
  hosts:
    pop-os: {heavy_slots: 2, jobs_per_slot: 4, max_row_agents: 2}
    mac:    {heavy_slots: 2, jobs_per_slot: 4, max_row_agents: 6, gate_runner: true, ui_gate_host: true}
  quiet_hours: "22:00-07:00 America/Chicago"
```

A campaign with no `rigor:` block uses `stage: early-dev`, `kind: greenfield` and an empty tier-1 list, which means everything is tier 3. `gather` must ask for the tier-1 paths before dispatching anything.

## Asking a campaign (at `gather`, or when the operator says "re-tune")

The conductor asks these as one numbered list, with leans, and records the answers in the rigor block:

1. **Stage?** Lean: what the repo shows. No users and no migrations means early dev.
2. **Kind of most rows?** Lean: from the board's file sets (mostly `web/` → UI; `domain/` → business logic).
3. **Tier-1 paths?** Lean: formats, identity, policy, money, published identifiers found in the repo. Always ask; never assume an empty list.
4. **Row size?** Lean: `feature` unless the stage is production or later.
5. **Machines?** Lean: measured slots for this box; is there a gate runner?
6. **Anything you want heavier or lighter than the default?** These become `overrides`.

**Re-tune signals.** Ask again when any of these shows up:

- more than half the commits on main are review churn;
- a tier-3 row bounces twice;
- a defect escapes into main that a cheaper test would have caught;
- the project changes stage: first user, first pilot, first external contributor.

## Worked example: Human Touch, 2026-11-11

```yaml
rigor:
  stage: early-dev
  kind: greenfield            # ht-ledger: domain; ht-ui: ui; ht-hosts: tooling
  tier1_paths: [identity, record formats, policy, emitter ordering, spec/vectors]
capacity: {pop-os: 2×4, 2 agents/campaign; mac: 2×4, 6 agents, gate runner, UI host pending ledger-macos-answers}
```

It resolves to:

| Rows | Settings |
|---|---|
| Tier-1 rows (ht-foundation's H-rows) | verify+mutants, props, 2 bounces, asks stop |
| ht-ledger | single-pass; business-rule tests plus scenes are its gate |
| ht-ui | single-pass; scenes per wave; UI gates on Linux until the Mac can record answers |
| ht-hosts | tooling; single-pass; a real run as the receipt |

All campaigns: light gate per row, full gate per milestone on the Mac, asks lean-provisional overnight, and no GitHub pushes unattended.
