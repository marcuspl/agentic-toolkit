# Porting the wave engine: `wave-runner.mjs` → `megaloop` (Go)

The JS prototype and the Go binary share one **contract** (BOARD.md / PROTOCOL.md
/ STATE_MODEL.md + the `{campaignDir, config, rows}` input and `{returned, gates,
failed}` output). Proving it in JS makes the Go build a reimplementation, not a
redesign. Construct-by-construct map:

| JS (Workflow tool)                          | Go (`megaloop` binary)                                              |
|---------------------------------------------|--------------------------------------------------------------------|
| `args = {campaignDir, config, rows}`        | `cmd/megaloop wave` reads BOARD.md itself + a `--config` file       |
| `pipeline(rows, implStage, verifyStage)`    | worker pool: `errgroup` + a buffered semaphore chan (cap = cores-2) |
| each row = one independent chain            | one goroutine per row runs impl→verify; no barrier                  |
| `agent(prompt, {isolation:'worktree'})`     | `git worktree add` a temp dir off `config.baseBranch`, defer remove |
| `agent(prompt, {schema})`                   | `exec.Command("claude","-p",prompt,"--output-format","json"...)`; unmarshal stdout into the struct |
| `ITEM_RESULT` / `VERIFY_RESULT` JSON Schema | Go structs w/ json tags + validate (enum checks)                    |
| `label` / `phase` / `log()`                 | structured logs / a TUI progress model                             |
| return `{returned, gates, failed}`          | print JSON to stdout; conductor (or `megaloop merge`) consumes it   |

## What the Go binary gains over the JS prototype
- **Headless / cron**: runs with no live Claude Code session → the unattended tail
  (grind to gates, park, sleep). Wire `claude -p --permission-mode <scoped>` so the
  worktree children run unattended without prompts.
- **A real merge queue**: `megaloop merge` can own the serial integration (the JS
  version leaves merges to the conductor).
- **Reusable across workstations**: ship it like the other Go reference tooling.

## What stays the conductor's job either way
- Writing BOARD.md (single-writer claims + status reflection).
- Deciding gates (`fleet`/`push`/`deploy`/`product-question`/`security-crux`).
- Approving the `gather` draft board before any wave runs.

## Contract invariants the port must preserve
1. Engine is **stateless** wrt BOARD.md — it takes rows in, returns results out.
   (Keeps the conductor the single writer; makes the engine trivially testable.)
2. `code` rows run **worktree-isolated**; never `git add -A`; branch `megaloop/<id>`.
3. Never dispatch `fleet`/`push`/`deploy`; a row that turns out to need one comes
   back `GATED` with a `gateClass`.
4. Verify is **independent** of the implementer's self-review (separate agent).
