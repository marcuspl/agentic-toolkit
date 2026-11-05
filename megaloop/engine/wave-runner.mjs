export const meta = {
  name: 'megaloop-wave',
  description: 'Run one megaloop wave: dispatch unblocked rows to isolated worktrees, self-swarm-review, return branches + gate queue for the conductor to merge',
  phases: [
    { title: 'Implement', detail: 'one agent per row; worktree-isolated for code' },
    { title: 'Verify', detail: 'independent verify per returned branch' },
  ],
}

// ── Contract ────────────────────────────────────────────────────────────────
// args = {
//   campaignDir: string,                    // ".claude/<slug>"
//   config: { baseBranch, swarmReviewCmd, testCmd, worktreeSetup?, codeReviewFocus? },
//     worktreeSetup: a fast command to warm a fresh isolation worktree (e.g. link
//     the main checkout's node_modules) so tests don't pay a cold reinstall.
//   rows: [ { id, kind, title, deps, files, docRef, prompt } ],
// }
// The CONDUCTOR pre-filters to the dispatchable set (TODO, deps met, disjoint
// files, kind in code|investigate|design) and records claims in BOARD.md BEFORE
// calling this. This engine is stateless: it never reads/writes BOARD.md.
// Returns { returned, gates, failed } for the conductor to merge + reflect.

// Defensive: the Workflow tool may deliver args as a JSON string rather than
// a parsed object — without this parse the engine silently sees zero rows.
const a = typeof args === 'string' ? JSON.parse(args) : (args || {})
const cfg = a.config || {}
const campaignDir = a.campaignDir || '.claude/campaign'
const rows = (a.rows || []).filter(r =>
  ['code', 'investigate', 'design'].includes(r.kind))

if (!rows.length) {
  log('megaloop-wave: no dispatchable rows')
  return { returned: [], gates: [], failed: [] }
}
log(`megaloop-wave: ${rows.length} rows — ${rows.map(r => r.id).join(', ')}`)

// ── Schemas (validated at the tool layer; the Go port unmarshals these) ───────
const GATE_CLASSES = ['deploy-gate', 'push-gate', 'fleet-gate', 'product-question', 'security-crux', 'secrets/target', '']

const ITEM_RESULT = {
  type: 'object',
  required: ['status'],
  additionalProperties: false,
  properties: {
    status: { enum: ['RETURNED', 'GATED', 'FAILED'] },
    branch: { type: 'string' },                       // megaloop/<id> (code only)
    files: { type: 'array', items: { type: 'string' } },
    testResult: { type: 'string' },
    selfReview: { type: 'string' },                   // distilled swarm verdict + resolution
    reviewVerdict: { enum: ['pending', 'passed', 'findings-fixed', 'findings-rejected', 'codescene-flagged'] },
    rejectedRationale: { type: 'string' },            // required iff findings-rejected
    report: { type: 'string' },                       // investigate/design deliverable
    gateClass: { enum: GATE_CLASSES },                // set iff status=GATED
    deferred: { type: 'array', items: { type: 'string' } }, // spotted-not-now → TECH_DEBT.md
    notes: { type: 'string' },
  },
}

const VERIFY_RESULT = {
  type: 'object',
  required: ['verdict'],
  additionalProperties: false,
  properties: {
    verdict: { enum: ['passed', 'findings-fixed', 'findings-rejected', 'codescene-flagged', 'failed'] },
    summary: { type: 'string' },
    rejectedRationale: { type: 'string' },
  },
}

// ── Prompt builders (the Go port emits the same text to `claude -p`) ──────────
function implPrompt(row) {
  const isCode = row.kind === 'code'
  return [
    `You are a megaloop sub-agent doing exactly ONE board row: **${row.id}** — ${row.title}.`,
    ``,
    `Contract (${campaignDir}/PROTOCOL.md):`,
    row.docRef ? `1. Read this first: ${row.docRef}.` : `1. (No doc-ref; work from the row description.)`,
    isCode
      ? [
        cfg.worktreeSetup
          ? `2. Prepare this fresh worktree FIRST: \`${cfg.worktreeSetup}\` — links/reuses deps so tests don't pay a full from-scratch reinstall (a cold \`npm install\` can exhaust the run budget before you commit).`
          : `2. This is a fresh isolated worktree (no deps installed). If your tests need deps, use the project's FAST setup (link/reuse the main checkout's deps), not a from-scratch reinstall — a cold install can exhaust the run budget.`,
        `3. Create the row branch up front: \`git checkout -b megaloop/${row.id}\`.`,
        `4. Implement ONLY this row. Minimal, idiomatic diff. Touch only: ${(row.files || []).join(', ') || '(scope tightly)'}.`,
        `5. COMMIT NOW — \`git commit <pathspecs>\` (never \`git add -A\`): a DURABLE checkpoint on \`megaloop/${row.id}\` *before* the slow test/review steps. If the run is later interrupted (budget/timeout), this leaves a committed branch the next incarnation can verify+merge — not lost uncommitted work. This ordering is the point: durability before verification.`,
        `6. Add/extend tests; run affected packages green: \`${cfg.testCmd || 'the affected test command'}\`. Fold the tests + any fixes into the branch (\`git commit --amend <pathspecs>\` or a follow-up commit).`,
        `7. Self-swarm-review your diff: \`${cfg.swarmReviewCmd || 'swarm-review --preset code --git-diff'}\`${cfg.codeReviewFocus ? ` --focus "${cfg.codeReviewFocus}"` : ''}. Fix real findings (amend/commit); note any you consciously reject (→ reviewVerdict "findings-rejected" + rejectedRationale).`,
        `8. Leave NOTHING staged or uncommitted on \`megaloop/${row.id}\`. Return the branch name + status RETURNED.`,
        `9. Do NOT push / open PRs / deploy / touch live infra. If this row REQUIRES any of those, STOP → return status "GATED" with the gateClass.`,
      ].join('\n')
      : [
        `2. This is a ${row.kind} row: ${row.kind === 'investigate' ? 'read-only research, no code changes.' : 'produce the plan/spec doc only, no code.'}`,
        `3. Return a structured, path-cited deliverable in \`report\`.`,
      ].join('\n'),
    `Finally: if you spot an obvious-but-not-now issue, add it to \`deferred\` (it goes to TECH_DEBT.md).`,
    ``,
    `Return the distilled result ONLY (no diffs/transcripts) per the schema.`,
  ].filter(Boolean).join('\n')
}

function verifyPrompt(row, res) {
  return [
    `Independently verify megaloop row **${row.id}** on branch \`${res.branch}\` — do NOT trust the implementer's self-review.`,
    `1. Confirm the branch builds and the touched tests pass: \`${cfg.testCmd || 'the affected test command'}\`.`,
    `2. Re-review the diff adversarially (\`${cfg.swarmReviewCmd || 'swarm-review --preset code --git-diff'}\`); optionally run codescene analyze_change_set.`,
    `3. Return a verdict. Use "failed" if the branch is red or a real bug survives; "findings-rejected" only with a rationale.`,
  ].join('\n')
}

// ── Pipeline: implement → verify, per row, no barrier ─────────────────────────
const results = await pipeline(
  rows,
  row => agent(implPrompt(row), {
    label: `impl:${row.id}`,
    phase: 'Implement',
    isolation: row.kind === 'code' ? 'worktree' : undefined,
    schema: ITEM_RESULT,
  }).then(r => (r ? { ...r, id: row.id, kind: row.kind, title: row.title } : { id: row.id, kind: row.kind, status: 'FAILED', notes: 'impl agent returned null' })),

  (res, row) => {
    if (!res || res.status !== 'RETURNED') return res            // gated/failed → surface as-is
    if (res.kind !== 'code' || !res.branch) return res           // investigate/design: nothing to verify
    return agent(verifyPrompt(row, res), {
      label: `verify:${row.id}`,
      phase: 'Verify',
      isolation: 'worktree',
      schema: VERIFY_RESULT,
    }).then(v => {
      if (!v) return { ...res, reviewVerdict: res.reviewVerdict || 'pending', notes: `${res.notes || ''} (independent verify returned null)`.trim() }
      if (v.verdict === 'failed') return { ...res, status: 'FAILED', reviewVerdict: 'pending', notes: `independent verify FAILED: ${v.summary || ''}` }
      return { ...res, reviewVerdict: v.verdict, rejectedRationale: v.rejectedRationale || res.rejectedRationale, notes: `${res.notes || ''} verify: ${v.summary || v.verdict}`.trim() }
    })
  },
)

const done = results.filter(Boolean)
const summary = {
  returned: done.filter(r => r.status === 'RETURNED'),
  gates: done.filter(r => r.status === 'GATED'),
  failed: done.filter(r => r.status === 'FAILED'),
}
log(`megaloop-wave done: ${summary.returned.length} returned, ${summary.gates.length} gated, ${summary.failed.length} failed`)
return summary
