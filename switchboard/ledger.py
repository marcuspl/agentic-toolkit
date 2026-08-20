"""ledger — append-only per-campaign work ledger + phone-sized recap.

The "what happened while I was AFK" record (phone-loop spec §6). One JSON line
per stage transition in `<campaign>/ledger.jsonl`:

    {"ts": "2026-07-10T03:18:08Z", "row": "T3", "event": "approved",
     "actor": "daemon", "detail": {...}}

Written by DUMB CODE ONLY (§10.4): the daemon (approved), the dispatcher
(filed), and per-repo release scripts (shipped/pushed/live — e.g. app's
ios/App/scripts/ledger.sh, whose schema this matches exactly). LLM lanes
produce receipts elsewhere; they never write the ledger directly.

Event vocabulary (convention, not enforced): filed promoted wave-start
wave-fail verify-pass merged approved ship-start ship-fail shipped pushed
push-fail live live-fail live-timeout retried dropped note

`render()` is the `!recap` body: per-row timelines with durations, newest
first, hard-capped for chat.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

MAX_ROWS = 8           # recap is a phone surface, not a report
MAX_EVENTS_PER_ROW = 6

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def append(cdir: Path, row: str, event: str, actor: str,
           detail: "dict | None" = None) -> None:
    """One-line O_APPEND write; short lines are atomic enough for our few
    concurrent writers. Never raises — the ledger is telemetry, and telemetry
    must not break the action it records."""
    try:
        rec = {"ts": time.strftime(TS_FMT, time.gmtime()),
               "row": row, "event": event, "actor": actor}
        if detail:
            rec["detail"] = detail
        cdir.mkdir(parents=True, exist_ok=True)
        with open(cdir / "ledger.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load(cdir: Path, since_hours: "float | None") -> "tuple[list, dict]":
    cutoff = ""
    if since_hours is not None:
        cutoff = time.strftime(TS_FMT, time.gmtime(time.time()
                                                   - since_hours * 3600))
    order: list = []
    rows: dict = {}
    try:
        lines = (cdir / "ledger.jsonl").read_text().splitlines()
    except (FileNotFoundError, OSError):
        return order, rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec = _normalize(rec)
        if cutoff and rec.get("ts", "") < cutoff:
            continue
        rid = rec.get("row", "?")
        if rid not in rows:
            rows[rid] = []
            order.append(rid)
        rows[rid].append(rec)
    return order, rows


def _normalize(rec: dict) -> dict:
    """Tolerate writer drift (LLM appenders WILL wander from the schema):
    epoch-second/ms timestamps become the canonical string, non-str rows are
    coerced, non-dict details are wrapped. Read-side tolerance keeps the
    append-only file honest without rewriting history."""
    ts = rec.get("ts")
    if isinstance(ts, (int, float)):
        secs = ts / 1000.0 if ts > 1e11 else float(ts)
        rec["ts"] = time.strftime(TS_FMT, time.gmtime(secs))
    elif not isinstance(ts, str):
        rec["ts"] = ""
    if not isinstance(rec.get("row"), str):
        rec["row"] = str(rec.get("row", "?"))
    if rec.get("detail") is not None and not isinstance(rec["detail"], dict):
        rec["detail"] = {"value": str(rec["detail"])}
    return rec


def _dur(a: str, b: str) -> str:
    try:
        m = int((datetime.strptime(b, TS_FMT)
                 - datetime.strptime(a, TS_FMT)).total_seconds() // 60)
    except ValueError:
        return "?"
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def render(cdir: Path, since_hours: "float | None" = 24.0) -> str:
    """Phone-sized recap: newest rows first, durations, ⚠️ on failures."""
    order, rows = _load(cdir, since_hours)
    span = f"last {since_hours:g}h" if since_hours is not None else "all time"
    if not order:
        return f"recap ({span}): nothing in the ledger."
    shipped = sum(1 for r in order
                  if any(e["event"] == "shipped" for e in rows[r]))
    failures = sum(1 for r in order
                   if any(e["event"].endswith(("-fail", "-timeout"))
                          for e in rows[r]))
    out = [f"recap ({span}): {len(order)} row(s) · {shipped} shipped · "
           f"{failures} with ⚠️"]
    for rid in reversed(order[-MAX_ROWS:]):
        evs = sorted(rows[rid], key=lambda e: e.get("ts", ""))
        head = f"• {rid}"
        if len(evs) > 1:
            head += (f" — {_dur(evs[0]['ts'], evs[-1]['ts'])} "
                     f"({evs[0]['event']} → {evs[-1]['event']})")
        out.append(head)
        for e in evs[-MAX_EVENTS_PER_ROW:]:
            d = e.get("detail") or {}
            dtxt = " ".join(f"{k}={v}" for k, v in list(d.items())[:3])
            flag = " ⚠️" if e["event"].endswith(("-fail", "-timeout")) else ""
            out.append(f"   {e['ts'][11:16]} {e['event']}{flag}"
                       + (f"  {dtxt}" if dtxt else ""))
    if len(order) > MAX_ROWS:
        out.append(f"(+{len(order) - MAX_ROWS} older row(s) — see ledger.jsonl)")
    return "\n".join(out)


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="sb-ledger-"))
    results = []

    def check(name, ok):
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}")
        results.append(ok)

    append(tmp, "T1", "filed", "frontdesk", {"msgId": 7})
    append(tmp, "T1", "approved", "daemon", {"approvedBy": "marcus"})
    append(tmp, "T1", "shipped", "ship-from-chat", {"build": "13"})
    append(tmp, "T2", "wave-fail", "conductor", {"cause": "tests red"})
    lines = (tmp / "ledger.jsonl").read_text().splitlines()
    check("append writes one jsonl line per event", len(lines) == 4)
    rec = json.loads(lines[0])
    check("schema matches ios ledger.sh exactly",
          set(rec) == {"ts", "row", "event", "actor", "detail"}
          and rec["row"] == "T1" and rec["actor"] == "frontdesk")

    r = render(tmp, since_hours=1.0)
    check("recap counts rows/shipped/failures",
          "2 row(s)" in r and "1 shipped" in r and "1 with ⚠️" in r)
    check("recap shows duration arc", "filed → shipped" in r)
    check("recap flags failures", "wave-fail ⚠️" in r)
    check("recap newest-first", r.index("• T2") < r.index("• T1"))

    with open(tmp / "ledger.jsonl", "a") as f:
        f.write(json.dumps({"ts": "2020-01-01T00:00:00Z", "row": "OLD",
                            "event": "note", "actor": "test"}) + "\n")
    r_recent = render(tmp, since_hours=1.0)
    check("since filter excludes old events",
          "OLD" not in r_recent and "T1" in r_recent)
    check("empty campaign renders gracefully",
          "nothing" in render(tmp / "nope", since_hours=24.0))

    # cap: 12 rows → only MAX_ROWS shown + an overflow note
    tmp2 = Path(tempfile.mkdtemp(prefix="sb-ledger2-"))
    for i in range(12):
        append(tmp2, f"T{i}", "note", "test")
    r2 = render(tmp2, since_hours=1.0)
    check("row cap + overflow note",
          r2.count("• T") == MAX_ROWS and "+4 older" in r2)

    # corrupted line is skipped, not fatal
    with open(tmp / "ledger.jsonl", "a") as f:
        f.write("not json\n")
    check("corrupt line skipped", "2 row(s)" in render(tmp, since_hours=1.0))

    # writer drift tolerated: epoch-ms ts + non-dict detail render fine
    with open(tmp / "ledger.jsonl", "a") as f:
        f.write(json.dumps({"row": "T3", "event": "promoted",
                            "actor": "conductor",
                            "ts": int(time.time() * 1000),
                            "detail": "inc=abc123"}) + "\n")
    r3 = render(tmp, since_hours=1.0)
    check("epoch-ms ts + string detail normalized",
          "3 row(s)" in r3 and "promoted" in r3 and "inc=abc123" in r3)

    ok = all(results)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
