# RECEIPTS — no claim without an executable receipt

Seeded from megaloop `templates/RECEIPTS.template.md`; edit per campaign.

The swarm's real product was never findings — it was **distrust, properly
allocated**: N independent skeptics make it hard for a plausible-but-wrong
claim to survive. Profile `receipts` removes the swarm from the per-row path,
so the distrust must be relocated, not deleted. This is where it lives:

> **No agent claim counts unless it carries an executable receipt.**

## Receipt kinds, in rough order of strength

1. **Mutation** — break the behaviour, name the test that went red, revert,
   show clean.
2. **Deletion test** — remove the entry/decorator/guard, show the suite red
   (or — damningly — show it stays green, byte-identical).
3. **Live execution** — HTTP status, row count before/after, DDL applied twice
   against a throwaway database, topics-enqueued list.
4. **Screenshot bound to an assertion** — a screenshot alone is a photograph of
   whatever happened, including nothing (see CAPTURE-RULES.md).

## Rules

- A **verify PASS must include mutation receipts** — at least one per new
  requirement id. A pass without receipts is an opinion.
- A claim without a receipt is filed as a **hypothesis**, in the report,
  labelled as one. (Pilot anecdote: a build agent asserted an unguarded
  registration surface with no receipt; the verify agent refuted it by deleting
  an entry and watching the gate go red in 0.58s. That asymmetry is this file
  in one sentence.)
- The **conductor reviews receipts, not code**, and re-runs **one receipt per
  report**. Receipts can be gamed by weak mutations; the spot-check is what
  keeps them from decaying into theatre. Drop it and they will.
- Verify is prompted to *refute*, not to confirm: mutation-test at least three
  of the builder's claims, sweep for the row's named defect class, confirm each
  new requirement id has a test that would fail without it.

## File shape

`<campaignDir>/receipts/<id>.json` — written by the conductor at verify PASS.
Backward-compatible with the fix-and-ship shape (`row`/`sha`/`suites`/`ts`);
profile `receipts` adds the `receipts` array:

```json
{
  "row": "T41",
  "sha": "<verified branch head>",
  "suites": {"unit": 145, "integration": 12},
  "receipts": [
    {"kind": "mutation", "claim": "REQ-203 guard rejects cross-member writes",
     "how": "inverted the authority check", "result": "test_authority_matrix went red; reverted, clean"},
    {"kind": "live", "claim": "amend with cleared figure still emits",
     "how": "drove the real handler against a SET-honouring connection", "result": "topic enqueued, 1 row updated"}
  ],
  "ts": 1783480343575
}
```
