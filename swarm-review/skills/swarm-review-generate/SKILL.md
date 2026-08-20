---
name: swarm-review-generate
description: Use this skill when the user wants to GENERATE original analysis from a brief or question using multiple AI models in parallel — ideation, building the case for a proposal, exploring an argument, or first-pass research for a deck/whitepaper. This is the "offensive" counterpart to swarm-review-code/prose (which critique existing content): instead of reviewing, it fans a brief out to a diverse roster (GPT-5.5, Sonnet-4.6, Gemini-3.1-Pro, Kimi-K3) via cursor-agent (free) with API fallback, then synthesizes the divergent takes into one strong position. Triggers on "swarm generate", "fan this question out to the models", "multi-model brainstorm/ideation", "build the case for X with the swarm", or "/swarm-review-generate". For critiquing existing code use swarm-review-code; for critiquing existing prose use swarm-review-prose.
version: 1.0.0
---

# Swarm Review — Generate (offensive mode)

Fan a BRIEF or question out to 4 models in parallel for **original analysis**,
then synthesize. This is the inverse of the review skills: those critique
content the user already has; this one *produces* the first draft of thinking —
the case for a proposal, the arguments and evidence, the risks, the
differentiation. Diverse priors give divergent idea sets; agreement across
models signals a robust point, divergence surfaces angles worth stealing.

This runs `--mode generate` on the shared `swarm-review` engine. It is a
lightweight ideation pass — single round, no cross-model debate, no citation
verification. For deep, verified, multi-wave research that ends in a deck or
whitepaper, the heavier `../research` pipeline is the right tool; use this for a
fast, free first-pass divergence.

## Step 1 — Determine the brief

Parse the skill args:

- **A question/brief string** in args → pass via `-q "..."`
- **`--stdin`** → pipe the user's pasted brief (ask them to paste if needed)
- **File paths** in args → passed as **reference context** the models draw on
  (NOT content to review). Combine with a `-q` brief or `--stdin`.
- **No brief at all** → ask the user what to investigate, or accept a paste.

Pass-through flags the user may add:
- `--preset prose` (default for generate — generalist ideation roster) or
  `--preset code` (the coding roster, for a technical/architecture brief)
- `--backend auto|cursor|api` (default: auto — cursor first, fast pivot to API)
- `--no-gpt`, `--no-sonnet`, `--no-gemini`, `--no-kimi` (drop a model)
- `--focus "..."` for extra steer (e.g. "tone for investors", "be skeptical")
- `--timeout N` (seconds per model)

## Step 2 — Run the engine

```bash
swarm-review --mode generate -q "THE BRIEF" [reference-files...] [flags]
# or:  cat brief.md | swarm-review --mode generate --stdin [flags]
```

The script writes progress to stderr (visible to you), JSON to stdout. If it
exits non-zero with no output, report the error and stop — never fabricate.
(Exit 2 means it ran but ≥1 model errored; the JSON is still valid — synthesize
from the survivors and note the gap.)

`swarm-review --health` probes every model on both backends if generators start
erroring.

## Step 3 — Parse the output

```json
{
  "mode": "generate",
  "content_summary": "Make the case that ...",
  "content_type": "generate",
  "backend": "cursor",
  "reviewers": [
    {"name": "gpt",    "model": "gpt-5.5-extra-high",       "text": "...", "error": null},
    {"name": "sonnet", "model": "claude-4.6-sonnet-medium", "text": "...", "error": null},
    {"name": "gemini", "model": "gemini-3.1-pro",           "text": "...", "error": null},
    {"name": "kimi",   "model": "kimi-k3-max",              "text": "...", "error": null}
  ]
}
```

Each model returns an independent take on the brief (thesis → case → objections
→ differentiation → recommendation). Note which succeeded (`error: null`). If
all four errored, report the errors and stop.

## Step 4 — Synthesize

Do NOT just concatenate the four takes. Build ONE strong position from them:

### Headline thesis
The sharpest one-or-two-sentence statement of the case, drawn from the
strongest framing across the models (name which model framed it best if one
clearly nailed it).

### Consensus pillars (the robust core)
Arguments or evidence that 3–4 models independently reached — these are the
load-bearing points to build the proposal/deck on, because independent priors
converged on them. State each and cite which models raised it.

### Divergent angles worth stealing
The valuable ideas only one model produced — label by model. These are where
the multi-model spread pays off: a non-obvious framing (often Kimi's
non-Western prior or Sonnet's structural read), a differentiator, a piece of
evidence the others missed. Pull the best of these into the synthesis.

### Contested / soft points
Where models disagreed, or where claims came tagged `[UNVERIFIED]`/`[ESTIMATE]`.
Surface these explicitly — they are the parts of the case that need a real
source before they go in a deck. Do not launder them into confident fact.

### Recommended spine
A concrete, ordered structure for the proposal/deck/argument: the thesis, the
3–5 pillars to lead with, the objections to pre-empt, and the single strongest
differentiator. This is the deliverable — what the user acts on next.

## Synthesis tone

- Write in prose, build a position — don't summarize four essays in turn.
- Separate the robust core (consensus) from the speculative edge (single-model
  or tagged claims). The user is going to put this in front of someone; mark
  what still needs verification.
- A `[UNVERIFIED]`/`[ESTIMATE]` tag from a model is a TODO, not a fact. If the
  user wants those nailed down, point them at the `../research` pipeline's
  verification waves.

## On model errors

- One errored → synthesize from the remaining three, note the thinner spread.
- Two or three errored → proceed, but warn the divergence is limited.
- All errored: for cursor errors, suggest `--backend api` (run
  `swarm-review --health` to confirm it's viable); for API errors, check keys
  in the `.env` the engine loads (or set `SWARM_REVIEW_ENV_FILE`).
