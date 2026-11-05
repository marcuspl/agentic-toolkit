"""autowave — rudimentary safety harness for `--auto-wave` (token-burn guardrails).

`--auto-wave` lets a conductor incarnation autonomously spawn code sub-agents,
which burn tokens without a human in the loop. This module is the "don't
crazy-burn without knowing" harness — four independent guards, cheapest to
strongest, wired into `conductor-run.py`:

  1. **kill-switch file** (instant, hard) — `AUTOWAVE_OFF` in the campaign dir or
     `~/.config/switchboard/`. Present → force promotion-only. `touch` to stop the
     world, `rm` to resume; no cron/config/systemd edit.
  2. **daily $ budget** (soft) — today's recorded auto-wave cost ≥ the cap →
     downgrade this run to promotion-only.
  3. **daily run-count cap** (hard) — refuse the Nth auto-wave incarnation/day.
  4. **cost ledger + report** (visibility) — every auto-wave run records
     `{date,host,channel,inc,cost_usd,num_turns,rows,dur_sec,rc}` and the launcher
     posts a one-line cost summary + running daily total to the channel.

Enforced elsewhere: a per-run wall-clock timeout (`conductor-run.py --timeout`)
and a `SWITCHBOARD_MAX_ROWS_PER_RUN` fan-out cap handed to the conductor.

Caveat on accounting: `cost_usd` is parsed from the conductor incarnation's own
`--output-format stream-json` result. If the megaloop engine dispatches waves as
*separate* `claude` processes, their cost is NOT in that number — so treat the $
budget as best-effort visibility, and lean on the run-count + rows + wall-clock
+ kill-switch as the hard ceilings.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_DAILY_USD = 15.0
DEFAULT_MAX_RUNS_DAY = 20
DEFAULT_MAX_ROWS_PER_RUN = 3
GLOBAL_KILLSWITCH = Path("~/.config/switchboard/AUTOWAVE_OFF").expanduser()

# --- runaway watchdog backstops (per-incarnation, live) --------------------
# These are NOT daily-spend limiters — they are anti-runaway BACKSTOPS for the
# one failure the daily/preflight guards can't see: a single auto-wave
# incarnation stuck in a loop, burning tokens without making progress. They are
# sized MANY-times the largest legitimate run so a normal day (even one that
# waves almost continuously) NEVER trips them; only a genuine runaway does.
# Reference point — the biggest real run to date (T6, a full-stack merge):
# ~309 assistant events, ~1.2 MB transcript, 53 min, $22. The defaults below are
# ~5-40x that. Tune per host in routes.yaml `defaults:` if a run legitimately
# approaches one (raise it — the operator's rule: never trip on real work).
DEFAULT_MAX_TURNS_PER_RUN = 1500        # assistant events (T6 ~309 → ~5x)
DEFAULT_MAX_TRANSCRIPT_MB = 50          # stream-json bytes (T6 ~1.2 MB → ~40x)
DEFAULT_STALL_SECS = 1800               # zero transcript output this long = wedged


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def ledger_path(state_file: str) -> Path:
    return Path(state_file).expanduser().parent / "autowave-ledger.jsonl"


def caps(cfg_defaults: dict) -> dict:
    d = cfg_defaults or {}
    return {
        "daily_usd": float(d.get("autowave_daily_usd", DEFAULT_DAILY_USD)),
        "max_runs_day": int(d.get("autowave_max_runs_day", DEFAULT_MAX_RUNS_DAY)),
        "max_rows_per_run": int(d.get("autowave_max_rows_per_run",
                                      DEFAULT_MAX_ROWS_PER_RUN)),
    }


def _today_entries(lp: Path) -> list[dict]:
    today = _today()
    out = []
    try:
        for line in lp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("date") == today:
                out.append(e)
    except (FileNotFoundError, OSError):
        pass
    return out


def summarize_today(lp: Path) -> tuple[int, float]:
    """(auto-wave runs today, $ spent today). Suppression notices don't count."""
    es = [e for e in _today_entries(lp) if e.get("event", "run") == "run"]
    cost = sum(float(e.get("cost_usd") or 0.0) for e in es)
    return len(es), round(cost, 4)


def suppressed_keys_today(lp: Path) -> set:
    """reason_keys already announced as suppressed today (so a downgrade posts
    once per reason per day, not every cron cycle)."""
    return {e.get("reason_key") for e in _today_entries(lp)
            if e.get("event") == "suppressed"}


def killswitch(campaign_dir: Path) -> "Path | None":
    for f in (Path(campaign_dir) / "AUTOWAVE_OFF", GLOBAL_KILLSWITCH):
        if f.exists():
            return f
    return None


def preflight(cfg_defaults: dict, campaign_dir: Path, lp: Path) -> dict:
    """Decide whether an auto-wave run may proceed. Returns a dict:
    {allowed: bool, reason: str, runs_today, cost_today, caps}."""
    c = caps(cfg_defaults)
    runs, cost = summarize_today(lp)
    base = {"runs_today": runs, "cost_today": cost, "caps": c}
    ks = killswitch(campaign_dir)
    if ks:
        return {**base, "allowed": False, "reason_key": "killswitch",
                "reason": f"kill-switch present ({ks})"}
    if runs >= c["max_runs_day"]:
        return {**base, "allowed": False, "reason_key": "run-cap",
                "reason": f"daily run cap reached ({runs}/{c['max_runs_day']})"}
    if cost >= c["daily_usd"]:
        return {**base, "allowed": False, "reason_key": "budget",
                "reason": f"daily budget reached (${cost:.2f}/${c['daily_usd']:.2f})"}
    return {**base, "allowed": True, "reason_key": "ok", "reason": "ok"}


def record(lp: Path, entry: dict) -> None:
    """Append one auto-wave run to the ledger (stamps date + ts)."""
    entry = dict(entry, date=_today(), ts=int(time.time() * 1000))
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "a") as f:
        f.write(json.dumps(entry) + "\n")


def parse_cost(transcript: Path) -> dict:
    """Pull `total_cost_usd`/`num_turns`/`usage` from a stream-json transcript's
    final `result` event. Best-effort — returns {} if not parseable."""
    result = {}
    try:
        for line in transcript.read_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "result":
                result = e  # keep the last result event
    except (FileNotFoundError, OSError):
        return {}
    if not result:
        return {}
    return {
        "cost_usd": result.get("total_cost_usd"),
        "num_turns": result.get("num_turns"),
        "usage": result.get("usage"),
        "subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
    }


# --- runaway watchdog (live, per-incarnation) ------------------------------
def watchdog_caps(cfg_defaults: dict) -> dict:
    """Anti-runaway backstops (per-incarnation). Distinct from `caps()` (the
    daily/fan-out preflight limits) — these bound ONE stuck incarnation."""
    d = cfg_defaults or {}
    return {
        "max_turns": int(d.get("autowave_max_turns_per_run",
                               DEFAULT_MAX_TURNS_PER_RUN)),
        "max_bytes": int(float(d.get("autowave_max_transcript_mb",
                                     DEFAULT_MAX_TRANSCRIPT_MB)) * 1024 * 1024),
        "stall_secs": float(d.get("autowave_stall_secs", DEFAULT_STALL_SECS)),
    }


def meter_transcript(transcript: Path) -> dict:
    """Live volume of a (still-growing) stream-json transcript. Returns
    {assistant_events, bytes, output_tokens}. `assistant_events` (a count) and
    `bytes` are reliable + monotonic — a looping incarnation grows both without
    bound. `output_tokens` is best-effort only (stream events carry partial
    per-message usage that does NOT sum to the final aggregate), so it is
    surfaced for visibility but the watchdog does NOT gate on it. Cheap: a
    substring scan, no JSON parse of the whole file."""
    evts = 0
    out = 0
    try:
        size = transcript.stat().st_size
    except OSError:
        return {"assistant_events": 0, "bytes": 0, "output_tokens": 0}
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type":"assistant"' not in line:
                    continue
                evts += 1
                # best-effort token pluck (visibility only)
                k = line.find('"output_tokens":')
                if k != -1:
                    j = k + len('"output_tokens":')
                    num = ""
                    while j < len(line) and (line[j].isdigit()):
                        num += line[j]
                        j += 1
                    if num:
                        out += int(num)
    except OSError:
        pass
    return {"assistant_events": evts, "bytes": size, "output_tokens": out}


def runaway_check(metrics: dict, wcaps: dict, mtime_age_sec: float) -> dict:
    """Pure kill-decision for the launcher watchdog. Trips ONLY on a genuine
    runaway (a loop growing turns/bytes past the backstop) or a wedged process
    (no transcript output for stall_secs) — never on a normal wave. Returns
    {kill: bool, reason_key, reason}."""
    ev = int(metrics.get("assistant_events", 0))
    by = int(metrics.get("bytes", 0))
    if ev >= wcaps["max_turns"]:
        return {"kill": True, "reason_key": "turn-backstop",
                "reason": f"{ev} assistant turns ≥ per-run backstop "
                          f"{wcaps['max_turns']} — runaway loop"}
    if by >= wcaps["max_bytes"]:
        return {"kill": True, "reason_key": "size-backstop",
                "reason": f"transcript {by / 1048576:.1f} MB ≥ backstop "
                          f"{wcaps['max_bytes'] / 1048576:.0f} MB — runaway loop"}
    if mtime_age_sec >= wcaps["stall_secs"]:
        return {"kill": True, "reason_key": "no-output-stall",
                "reason": f"no transcript output for {int(mtime_age_sec)}s ≥ "
                          f"{int(wcaps['stall_secs'])}s — wedged"}
    return {"kill": False, "reason_key": "ok", "reason": "ok"}


# ==========================================================================
def selftest() -> bool:
    import tempfile
    ok = []

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        lp = d / "autowave-ledger.jsonl"
        cdir = d / "campaign"
        cdir.mkdir()
        # high run cap so the budget tests isolate the $ guard (preflight checks
        # run-cap before budget); the run-cap test below uses its own low cap.
        defaults = {"autowave_daily_usd": 15.0, "autowave_max_runs_day": 50}
        defaults_lowrun = {"autowave_daily_usd": 15.0, "autowave_max_runs_day": 3}

        # empty ledger → allowed
        pf = preflight(defaults, cdir, lp)
        ok.append(("empty→allowed", pf["allowed"] is True and pf["cost_today"] == 0.0))

        # record two runs totalling $4 → still allowed, sums correct
        record(lp, {"channel": "app", "inc": "a", "cost_usd": 1.5})
        record(lp, {"channel": "app", "inc": "b", "cost_usd": 2.5})
        runs, cost = summarize_today(lp)
        ok.append(("sum-two", runs == 2 and abs(cost - 4.0) < 1e-9))
        ok.append(("under-budget", preflight(defaults, cdir, lp)["allowed"] is True))

        # push over the $ budget → blocked with budget reason
        record(lp, {"channel": "app", "inc": "c", "cost_usd": 12.0})
        pf = preflight(defaults, cdir, lp)
        ok.append(("over-budget", pf["allowed"] is False and "budget" in pf["reason"]))

        # run-count cap: fresh ledger, 3 cheap runs (cap=3) → 3rd preflight blocks
        lp2 = d / "l2.jsonl"
        for i in range(3):
            record(lp2, {"inc": str(i), "cost_usd": 0.01})
        pf = preflight(defaults_lowrun, cdir, lp2)
        ok.append(("run-cap", pf["allowed"] is False and "run cap" in pf["reason"]))

        # kill-switch beats everything (even an empty ledger)
        (cdir / "AUTOWAVE_OFF").write_text("")
        pf = preflight(defaults, cdir, d / "empty.jsonl")
        ok.append(("killswitch", pf["allowed"] is False and "kill-switch" in pf["reason"]))
        (cdir / "AUTOWAVE_OFF").unlink()

        # parse_cost from a stream-json transcript
        t = d / "t.log"
        t.write_text(
            '# header line (ignored)\n'
            '{"type":"assistant","message":{}}\n'
            '{"type":"result","subtype":"success","is_error":false,'
            '"num_turns":6,"total_cost_usd":0.4213,"usage":{"output_tokens":1200}}\n'
        )
        c = parse_cost(t)
        ok.append(("parse-cost", abs((c.get("cost_usd") or 0) - 0.4213) < 1e-9
                   and c.get("num_turns") == 6))
        ok.append(("parse-cost-missing", parse_cost(d / "nope.log") == {}))

        # --- runaway watchdog ------------------------------------------------
        wcaps = watchdog_caps({"autowave_max_turns_per_run": 5,
                               "autowave_max_transcript_mb": 1,
                               "autowave_stall_secs": 600})
        ok.append(("wcaps", wcaps["max_turns"] == 5
                   and wcaps["max_bytes"] == 1024 * 1024
                   and wcaps["stall_secs"] == 600))

        # meter a synthetic stream-json transcript: 3 assistant events, tokens
        tw = d / "meter.log"
        tw.write_text(
            '# header\n'
            '{"type":"system","subtype":"init"}\n'
            '{"type":"assistant","message":{"usage":{"output_tokens":10}}}\n'
            '{"type":"user","message":{}}\n'
            '{"type":"assistant","message":{"usage":{"output_tokens":20}}}\n'
            '{"type":"assistant","message":{"usage":{"output_tokens":5}}}\n'
        )
        m = meter_transcript(tw)
        ok.append(("meter-events", m["assistant_events"] == 3))
        ok.append(("meter-tokens", m["output_tokens"] == 35))
        ok.append(("meter-bytes", m["bytes"] == tw.stat().st_size and m["bytes"] > 0))
        ok.append(("meter-missing",
                   meter_transcript(d / "nope.log")["assistant_events"] == 0))

        # under every backstop, fresh output → no kill
        ok.append(("wd-ok", runaway_check(
            {"assistant_events": 3, "bytes": 1000}, wcaps, 5.0)["kill"] is False))
        # turns past backstop → kill
        r = runaway_check({"assistant_events": 5, "bytes": 1000}, wcaps, 1.0)
        ok.append(("wd-turns", r["kill"] is True and r["reason_key"] == "turn-backstop"))
        # bytes past backstop → kill
        r = runaway_check({"assistant_events": 2, "bytes": 2 * 1024 * 1024}, wcaps, 1.0)
        ok.append(("wd-size", r["kill"] is True and r["reason_key"] == "size-backstop"))
        # no output for longer than stall_secs → kill (wedged)
        r = runaway_check({"assistant_events": 2, "bytes": 1000}, wcaps, 601.0)
        ok.append(("wd-stall", r["kill"] is True and r["reason_key"] == "no-output-stall"))
        # a big-but-legit run stays under generous DEFAULT backstops
        dcaps = watchdog_caps({})
        ok.append(("wd-defaults-safe", runaway_check(
            {"assistant_events": 309, "bytes": 1_260_000}, dcaps, 30.0)["kill"] is False))

    allok = all(v for _, v in ok)
    for name, v in ok:
        print(f"  [{'ok ' if v else 'FAIL'}] {name}")
    print("PASS" if allok else "FAIL")
    return allok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
