# CAPTURE-RULES — assert, don't photograph

Seeded from megaloop `templates/CAPTURE-RULES.template.md`; edit per campaign.
This file exists because the table below is the part an agent gets wrong every
single time, and because each gotcha at the bottom cost real time once.

**The rule: every step that claims a state change must assert the state
changed.** A capture that navigates, clicks and screenshots is a photograph of
whatever happened — including nothing happening. Three consecutive capture runs
once produced 21 clean screenshots and "zero failures" while three writes were
silently failing. The screenshots were real. The system was broken. The report
was confident.

A capture that asserts is a test suite that produces a client deliverable as a
side effect. That is the whole trick: the same run that proves the system works
is the document handed over afterwards.

## Do / do not

| Do not | Do |
|---|---|
| screenshot after clicking submit | count the rows before and after, and **fail the run** if the count did not change |
| assert "an entry dated today exists" | assert **this run's** entry exists (a prior debug run satisfies the former) |
| screenshot a dashboard | poll until the rendered numbers are **stable for N iterations**, then assert them against the read-model |
| trust `locator.click()` after a fullPage shot | click via the DOM/locator and keep screenshots viewport-sized (see gotchas) |
| skip the shot when a scene fails | photograph **every failing scene, unconditionally** — a red assertion with the moment on film is the highest-value artifact the loop produces |
| re-run a flaky scene until green | delete or quarantine the flake same-day — a re-run culture is how a broken system photographs clean |

## Capture gotchas (each verified the hard way; keep appending)

- A `fullPage` screenshot overrides device metrics, and the restore can break
  later coordinate clicks **silently** — no request fires, nothing throws.
  Click via the DOM (`locator.click()` / in-page `el.click()`), keep shots
  viewport-sized.
- Playwright `inner_text()` returns the **rendered** text — CSS
  `text-transform: uppercase` on a heading breaks equality asserts.
  `text_content()` reads the DOM.
- Drive the deterministic path (a parser/fixture engine), never a live model —
  the capture must produce the same transcript every run.
- Server-rendered pages: `wait_for_load_state()` after every form submit; a
  redirect race reads the previous page's state.

## Scenes

One scene per surface/flow, each declaring a manifest:

```yaml
# scenes/<name>.yaml (or a header block in the scene script)
covers:   [REQ-101, REQ-102]   # requirement ids this scene discharges
surfaces: [svc-api, /route]    # services/routes it exercises
```

- Selection never chooses what is *verified* — the full assert-sweep runs every
  wave, camera off. Selection chooses what is *photographed*: scenes whose
  manifest intersects the wave's diff, plus every failing scene.
- An under-declared `surfaces` list skips photography that mattered — mitigated,
  not solved, by the sweep running regardless. Declare generously.
- `covers` ties UAT to the spec trace: the walkthrough cites which shot
  discharges which requirement, and a requirement no scene covers is visible.

## Worked example

`mcp-servers/sentinel-hl7-mcp/demo/ui/uat/capture.py` — 23 assertions, refusal
proven to move nothing, replay projection asserted against live state at three
cursors, failure path exercised (it caught the `inner_text` gotcha above on its
first run).
