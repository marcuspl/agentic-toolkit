---
name: swarm-review-code
description: Use this skill when the user asks to review CODE (a diff, files, a patch, an implementation) using multiple AI models. Fans out to a coding-specialized roster — Codex-5.3-High, Grok-4.5, Gemini-3.1-Pro, and Sonnet-4.6 — in parallel via cursor-agent (free), with direct-API fallback. Synthesizes into a single review flagging where models agree vs diverge. Triggers on phrases like "swarm review this code", "multi-model code review", "review this diff/patch/PR with the swarm", or "/swarm-review-code". For prose/plans/writing, use swarm-review-prose instead.
version: 2.0.0
---

# Swarm Review — Code

Fan a CODE review out to 4 models in parallel (Codex-5.3-High, Grok-4.5,
Gemini-3.1-Pro, Sonnet-4.6), then synthesize. Each model reviews
independently — agreement across models signals high confidence;
divergence is flagged explicitly.

This skill pins the **`code` preset** of the shared `swarm-review` engine.
There is a sibling skill, `swarm-review-prose`, for documents and writing —
the split is deliberate (explicit preset over auto-detection). Use this one
only for code.

## Step 1 — Determine what to review

Parse the skill args:

- **File paths** in args (e.g. `src/main.go`, `auth.py`) → pass those files
- **`--git-diff`** or **`--staged`** in args → pass those flags through
- **No args (or only pass-through flags)** → default to `--git-diff` (review changes vs HEAD)
- **`--stdin`** → pipe the user's pasted code (ask them to paste if needed)

Pass-through flags the user may add:
- `--backend auto|cursor|api` (default: auto — cursor first, fast pivot to API if cursor is blocked/over quota; parallel-safe)
- `--no-codex`, `--no-grok`, `--no-gemini`, `--no-sonnet` (drop a reviewer)
- `--focus "..."` for extra guidance (e.g. "concurrency safety", "the auth path")
- `--codex-model`, `--sonnet-model`, etc. for custom cursor models
- `--codex-api-model`, etc. for API-fallback overrides
- `--timeout N` (seconds per reviewer; baselines are codex/grok 180s, sonnet 240s, gemini 300s)

## Step 2 — Run the engine

Always pass `--preset code` so the coding roster and code focus are used:

```bash
swarm-review --preset code [args from Step 1]
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

A `FAIL` in the cursor column for a model means that cursor model is down
or your Cursor quota/on-demand cap is exhausted (frontier calls via
cursor-agent bill as API/on-demand once the included allowance is gone —
check the Cursor dashboard, the CLI can't report quota). A working `api`
column means `--backend api` (or `auto` fallback) is a viable escape — the
codex slot maps to the real `openai/gpt-5.3-codex` on OpenRouter, so the
code specialization is preserved on the API path too.

## Step 3 — Parse the output

```json
{
  "content_summary": "src/main.go",
  "content_type": "code",
  "backend": "cursor",
  "reviewers": [
    {"name": "codex",  "model": "gpt-5.3-codex-high",     "backend": "cursor", "text": "...", "error": null},
    {"name": "grok",   "model": "cursor-grok-4.5-high",   "backend": "cursor", "text": "...", "error": null},
    {"name": "gemini", "model": "gemini-3.1-pro",         "backend": "cursor", "text": "...", "error": null},
    {"name": "sonnet", "model": "claude-4.6-sonnet-medium","backend": "cursor", "text": "...", "error": null}
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
Bugs or issues flagged by 3–4 reviewers are the most trustworthy signal —
present these first with high confidence. Name the specific problem
(file/line/function) and cite which models flagged it.

### Divided opinions (reviewers disagree)
Where one model flagged something the others didn't — or two reached
opposite conclusions (e.g. "race condition here" vs "no race") — surface
the disagreement explicitly. Do not flatten it. If you can verify against
the code yourself, do so and say which side is right; otherwise let the
user decide.

### Model-specific insights
Anything valuable only one reviewer caught — label by model. These deserve
attention precisely because they came from a different failure-mode family
(Codex's coding priors, Sonnet's reasoning, Gemini/Grok's generalist eyes).

### What's working
Synthesize positive findings — if reviewers agree something is correct or
well-structured, say so.

### Verdict
One or two sentences: is the code ready to merge, the single most important
bug/fix to address first (by severity), and what can wait.

### Ledger (slot-usage accounting)
After delivering the synthesis, append one TSV line per **adjudicated solo
claim** — a finding only one model raised, or a disagreement you resolved
against the code — to `~/.local/state/swarm-review/adjudications.tsv`
(create the directory if needed):

```
YYYY-MM-DD<TAB>code<TAB><model-name><TAB><claim gist, one clause><TAB><verdict>
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
- On a factual code disagreement, flag it; verify against the code before
  picking a side. Don't assert a bug exists just because one model said so.
- Keep it tight. The user needs the consensus distilled and the
  disagreements surfaced — not a transcript of four reviews.

## On reviewer errors

- One errored → synthesize from the remaining three, note the gap.
- Two or three errored → proceed, but warn the user coverage is partial.
- All errored: for cursor errors, suggest `--backend api` (verified working
  — run `swarm-review --health` to confirm); for API errors, check that keys
  are set in the `.env` the engine loads (`SWARM_REVIEW_ENV_FILE`, else the
  `DEFAULT_ENV_FILES` defaults). The codex slot maps to the real
  `openai/gpt-5.3-codex` on the API path, so no specialization is lost.
