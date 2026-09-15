---
name: swarm-review-prose
description: Use this skill when the user asks to review PROSE — a document, plan, essay, email, spec, or any writing — using multiple AI models. Fans out to GPT-5.5, Sonnet-4.6, Gemini-3.1-Pro, and Kimi-K3 in parallel via cursor-agent (free), with direct-API fallback. Synthesizes into a single review flagging where models agree vs diverge. Triggers on phrases like "swarm review this doc/plan/writing", "multi-model review of this essay", "get another opinion on this draft", or "/swarm-review-prose". For code/diffs/patches, use swarm-review-code instead.
version: 2.0.0
---

# Swarm Review — Prose

Fan a PROSE review out to 4 models in parallel (GPT-5.5, Sonnet-4.6,
Gemini-3.1-Pro, Kimi-K3), then synthesize. The roster is chosen for
diverse writing/reasoning priors — Sonnet for long-range coherence and
register, Kimi for a non-Western prior, GPT and Gemini as strong
generalists. Agreement across models signals high confidence; divergence
is flagged explicitly.

This skill pins the **`prose` preset** of the shared `swarm-review` engine.
There is a sibling skill, `swarm-review-code`, for diffs and source files —
the split is deliberate (explicit preset over auto-detection). Use this one
only for documents and writing.

## Step 1 — Determine what to review

Parse the skill args:

- **File paths** in args (e.g. `plan.md`, `proposal.txt`) → pass those files
- **`--stdin`** → pipe the user's pasted text (ask them to paste if needed)
- **No file and no stdin** → ask the user what to review, or accept a paste
- `--git-diff` is supported but rarely what you want for prose — only use it
  if the user is reviewing changes to a markdown/text doc under version control

Pass-through flags the user may add:
- `--backend auto|cursor|api` (default: auto — cursor first, fast pivot to API if cursor is blocked/over quota; parallel-safe)
- `--no-gpt`, `--no-sonnet`, `--no-gemini`, `--no-kimi` (drop a reviewer)
- `--focus "..."` for extra guidance (e.g. "tighten the argument", "tone for execs")
- `--gpt-model`, `--kimi-model`, etc. for custom cursor models
- `--gpt-api-model`, etc. for API-fallback overrides
- `--timeout N` (seconds per reviewer; baselines are gpt 180s, sonnet/kimi 240s, gemini 300s)

## Step 2 — Run the engine

Always pass `--preset prose` so the prose roster and prose focus are used:

```bash
swarm-review --preset prose [args from Step 1]
```

The script writes progress to stderr (visible to you), JSON to stdout.
If it exits non-zero with no output, report the error and stop — never
fabricate a review. (Exit 2 means it ran but ≥1 reviewer errored; the JSON
is still valid — synthesize from the survivors and note the gap.)

### Health check (when reviewers misbehave, or to decide on --backend)

If reviewers start erroring, or the user asks whether the API path is worth
using, run the sweep — it probes every model on BOTH backends with a
trivial canary and prints availability + latency:

```bash
swarm-review --health
```

A `FAIL` in the cursor column means that cursor model is down or your Cursor
quota/on-demand cap is exhausted (frontier calls via cursor-agent bill as
API/on-demand once the included allowance is gone — check the Cursor
dashboard, the CLI can't report quota). A working `api` column means
`--backend api` (or `auto` fallback) is a viable escape. The API fallbacks
are version-matched peers of the cursor models (Sonnet 4.6, Kimi K3), so
no quality is lost on the fallback path.

## Step 3 — Parse the output

```json
{
  "content_summary": "plan.md",
  "content_type": "prose",
  "backend": "cursor",
  "reviewers": [
    {"name": "gpt",    "model": "gpt-5.5-extra-high",      "backend": "cursor", "text": "...", "error": null},
    {"name": "sonnet", "model": "claude-4.6-sonnet-medium","backend": "cursor", "text": "...", "error": null},
    {"name": "gemini", "model": "gemini-3.1-pro",          "backend": "cursor", "text": "...", "error": null},
    {"name": "kimi",   "model": "kimi-k3-max",             "backend": "cursor", "text": "...", "error": null}
  ]
}
```

Note which reviewers succeeded (`error: null`) and which failed. If all
four errored, report the errors and stop.

## Step 4 — Synthesize

Read all successful reviews carefully, then produce one synthesis:

### Header line
One sentence: what was reviewed, how many of the 4 models weighed in, backend used.

### Consensus findings (most/all agree)
Points flagged by 3–4 reviewers are the most trustworthy signal — present
these first with high confidence. Name the specific issue (section,
paragraph, claim) and cite which models flagged it.

### Divided opinions (reviewers disagree)
Where one model flagged something the others didn't — or two reached
opposite judgments (e.g. "the structure is clear" vs "the argument is
buried") — surface the disagreement explicitly. Do not flatten it. Let the
user decide on matters of taste; on matters of fact (a wrong claim, a
broken citation) say which side is right if you can tell.

### Model-specific insights
Anything valuable only one reviewer caught — label by model. These deserve
attention precisely because they came from a different prior (Sonnet's
coherence/register sense, Kimi's non-Western framing, GPT/Gemini's
generalist read).

### What's working
Synthesize positive findings — if reviewers agree the writing is clear,
well-argued, or well-structured, say so.

### Verdict
One or two sentences: is the document ready, the single most important
revision to make first, and what can wait.

### Ledger (slot-usage accounting)
After delivering the synthesis, append one TSV line per **adjudicated solo
claim** — a finding only one model raised, or a disagreement you resolved
on a matter of fact — to `~/.local/state/swarm-review/adjudications.tsv`
(create the directory if needed):

```
YYYY-MM-DD<TAB>prose<TAB><model-name><TAB><claim gist, one clause><TAB><verdict>
```

`verdict` is one of `validated` (held up), `refuted` (confidently wrong), or
`taste` (unresolvable judgment call). Log only claims you actually
adjudicated — never guess a verdict. This ledger is how roster slots earn
or lose their place (solo-find precision and refuted-claim rate per model);
don't mention it in the synthesis itself.

---

## Synthesis tone

- Write in prose, not bullets.
- Don't summarize each reviewer's full output — extract and compare.
- Separate matters of taste (surface as divided opinion) from matters of
  fact (resolve where you can).
- Keep it tight. The user needs the consensus distilled and the
  disagreements surfaced — not a transcript of four reviews.

## On reviewer errors

- One errored → synthesize from the remaining three, note the gap.
- Two or three errored → proceed, but warn the user coverage is partial.
- All errored: for cursor errors, suggest `--backend api` (verified working
  — run `swarm-review --health` to confirm); for API errors, check that keys
  are set in the `.env` the engine loads (`~/code/new/writings/.env` or
  `~/dev/<repo>/.env`). The API fallbacks are version-matched peers
  of the cursor models, so nothing is lost on the fallback path.
