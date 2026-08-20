# <CAMPAIGN> Megaloop — Sub-agent protocol

How the conductor and sub-agents cooperate without stepping on each other.

## Roles
- **Conductor** (main session): owns BOARD.md (single writer). Records claims
  BEFORE dispatch, runs the engine, merges RETURNED branches, updates the board,
  and STOPS at operator gates. Never lets a sub-agent touch live infra or push.
- **Sub-agent**: does exactly one board row in an ISOLATED git worktree,
  self-reviews, commits to a named branch, and returns a distilled report.

## Collision safety
- Each `code` sub-agent runs worktree-isolated → its own working copy off the base
  branch. No two agents share a working tree, so the git index never collides.
- Sub-agents do NOT write BOARD.md. The conductor reflects their status.
- Agents get DISJOINT file sets; two rows touching a shared file are serialized
  across waves, not parallelized.

## Sub-agent contract (every `code` agent)
1. Read the doc/plan section named in your prompt (`doc-ref`) first.
2. Implement ONLY your row. Keep the diff minimal and idiomatic to surrounding code.
3. Add/extend tests; run the affected packages green (scoped to what you touched).
4. **Self-swarm-review**: run the campaign's swarm-review command on your diff,
   read the synthesis, FIX real findings, and note any you consciously reject and
   why (this becomes `review_verdict: findings-rejected` with rationale).
5. Commit to a NEW branch `megaloop/<id>` via `git commit <pathspecs>` (never
   `git add -A` across a shared tree). Leave nothing staged.
6. **Do NOT** push, open PRs, deploy, or touch live infra / the CA / the overlay /
   registry. If your row turns out to require any of those, STOP and return it as
   a gate (`status: GATED`, with `gate_class`).
7. Return a DISTILLED report only: id, files changed, test result, swarm-review
   verdict (+ rationale for any rejected finding), branch name, and any
   gates/blockers discovered. No full diffs or transcripts.
8. If you spot an "obvious but not-now" issue, return it as a DEFERRED note (area,
   what, why-deferred) → the conductor logs it in TECH_DEBT.md.

## `investigate` / `design` agents
Read-only (investigate) or doc-only (design). No code changes, no infra mutation.
Return a structured, path-cited report the conductor can paste into the board.
A `design` output usually feeds an approval gate before its `code` rows unblock.

## Loop cadence
Wave = rows with no unmet deps and no file conflicts, dispatched in parallel by
the engine. The conductor records claims, runs the engine, then merges returned
branches (serial), updates BOARD.md, and dispatches the next now-unblocked rows.
Blocking rows run one at a time. The loop ends when every row is DONE or the only
remaining work is behind operator gates — then it surfaces the gate queue.
