# swarm-review

**swarm-review is a single-file multi-model review engine that runs a
fixed four-seat roster, records each model's findings, and adjudicates
every solo finding as validated, refuted, or taste. It makes reviewer
disagreement and solo-finding precision measurable; it does not turn
model consensus into proof.**

> **Boundary:** the ledger records adjudicated evidence about reviewer
> performance. It does not determine objective truth — a human (or a
> downstream agent with source access) performs the adjudication step,
> and a finding is only a question worth adjudicating until then.

## What it guarantees — and what it does not

Guaranteed by construction: four independent reviews of the same content;
per-seat attribution of every finding; a synthesis contract that keeps
divided opinions divided; a persistent, append-only record of how each
seat's solo findings fared. Not guaranteed: that consensus is correct
(shared training priors fail together), that every defect is found, or
that a validated finding stays valid when its source changes.

## The review contract

One engine, three postures, selected explicitly — never auto-detected:

| Preset | Posture | Roster character |
|---|---|---|
| `--preset code` | defensive — critique a diff/patch | coding-specialized seats |
| `--preset prose` | defensive — critique a document | generalist seats + a deliberate different-prior seat |
| `--mode generate` | offensive — original analysis from a brief | same roster, inverted stance |

Content is wrapped as untrusted input. The synthesis step (performed by
the caller, per the accompanying skill files) must name which model saw
what, verify factual disagreements against the source where possible, and
present consensus as a signal — not a verdict.

## Seats, fallbacks, and attribution

Rosters are fixed per preset. The default path runs through a
subscription CLI at no marginal API cost; `--backend api|auto` falls back
to direct APIs where every substitute is an exact family peer of the seat
it replaces, so specialization is preserved across the fallback.

**Attribution rule:** the engine's JSON output records, per seat, the
*actual* model id and backend that served the run. The adjudication
ledger keys findings by seat name; the run record is the provenance for
which concrete model spoke. A fallback is never silently credited to the
configured model — the output names what ran.

## The `--health` sweep

Vendor lineups churn. A model once vanished from the free path's lineup
and every "auto" run silently pivoted to the paid API path for two months
before a sweep caught it (2026-06→08; the incident is documented in the
engine's roster notes). `--health` probes every seat on both backends
with a one-word canary and prints availability, latency, and the
expected-vs-actual route per seat — the routing *contract* is monitored,
not just availability.

## Adjudication and the ledger

The interface is deliberately minimal and file-based. After a synthesis,
each **solo finding** (a claim only one model raised) and each
**resolved disagreement** is judged against the source material and
appended as one TSV line:

```
<date>\t<preset>\t<seat/model>\t<claim gist, one clause>\t<verdict>
```

Verdicts: `validated` (held up), `refuted` (confidently wrong), `taste`
(unresolvable judgment call). Only actually-adjudicated claims are
logged — never guessed verdicts. Seats keep or lose their place on
measured solo-find precision over time; the roster comments document the
standing challenger for the different-prior seat and the swap rule.

Worked example of why the pipeline exists: a review of a slide deck built
from this stack found that its roster-accountability slide carried a
superseded score (a single-domain 2.35 where the full tournament grid said
1.53) and a mis-attributed one (a 4.40 that belonged to a different model
entirely). Of fourteen adjudicated findings in that review, six were
refuted — two of them because the review bundle's source excerpts were
truncated, which the ledger records with the same weight as the validated
finds. A follow-up review of the essay written *about* that incident then
caught the correction itself: it had fixed the numbers and inverted the
timeline. Both directions are the product.

## Scope — explicitly out

- Consensus as correctness, or model families as statistically
  independent (different vendors reduce correlation; they do not zero it).
- Complete defect detection; universal model rankings; stable provider
  pricing or lineups.
- Automatic fixing or patching — the engine reviews; the caller acts.

## Quick start

```bash
./install.sh                 # symlinks the engine onto PATH, registers the skills
swarm-review --health        # verify every seat on both backends
swarm-review --preset code --git-diff
swarm-review --preset prose mydoc.md --focus "attack claims against cited sources"
```

Requires Python 3.8+. The free path needs the subscription CLI logged in;
the API path needs per-provider keys in a `.env` (see the engine header —
never commit keys).

## Where this fits

swarm-review is the *evaluation* mechanism of a three-part set —
[megaloop](../megaloop/) (durable campaign execution; uses this engine for
per-row self-review and independent verification) and
[switchboard](../switchboard/) (a deterministic dispatch front door). Each
stands alone; they share a doctrine, not a runtime. The companion essay,
*The Accountability Slide Was Wrong*, tells the adjudication story in
full.
