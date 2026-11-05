#!/usr/bin/env python3
"""conductor-run — launch a megaloop conductor incarnation for a switchboard
campaign (ML-4 lock + env contract). Used by hand and by cron.

It resolves the campaign from routes.yaml (by channel), sets the SWITCHBOARD_*
env the conductor reads, takes the non-blocking conductor.lock (skip if held —
protects the serial-merge invariant against cron/!nudge overlap), and runs
`claude -p "/megaloop resume"` in the repo with a scoped tool allowlist.

Pilot-safe default: PROMOTION-ONLY. Without --auto-wave, the conductor promotes
INBOX→BOARD + consumes approvals + debriefs, but does NOT run code waves (they
stay operator-run via `/megaloop wave`). Pass --auto-wave to let it run waves
(a conscious autonomy grant — the conductor then spawns code agents on the repo).

Usage:
  conductor-run.py --channel app [--auto-wave] [--config PATH] [--timeout SEC]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

import autowave

# Promotion-only conductor: read + edit campaign files + post to keybase. No
# arbitrary destructive shell. (--auto-wave needs a broader profile / the
# operator's own settings; keep the unattended default tight.)
CONDUCTOR_TOOLS = (
    "Read,Edit,Write,Grep,Glob,"
    "Bash(python3:*),Bash(keybase:*),Bash(git:*),Bash(flock:*),"
    "Bash(printenv:*),Bash(env:*),"
    "Bash(ls:*),Bash(cat:*),Bash(mkdir:*),Bash(head:*),Bash(tail:*),"
    "Bash(wc:*),Bash(rg:*),Bash(grep:*),Bash(find:*)"
)

# The launcher holds conductor.lock (below); state that up-front IN THE PROMPT so
# the incarnation never has to check the env/lock and never stands down on seeing
# its own process. This is the reliable signal (an allowlist can deny printenv).
RESUME_PROMPT = (
    "/megaloop resume\n\n"
    "IMPORTANT — launch context (read before acting):\n"
    "1. You were launched by conductor-run.py, which already holds conductor.lock "
    "for your entire lifetime AND owns liveness (it writes HEARTBEAT every ~60s on "
    "your behalf). You ARE the one authorized incarnation. Do NOT run pgrep, do NOT "
    "read or flock conductor.lock, do NOT write HEARTBEAT, do NOT look for 'another "
    "incarnation', do NOT stand down — any running `claude -p /megaloop resume` is "
    "yourself. Proceed immediately.\n"
    "2. WRITES: this environment blocks the Write/Edit tools and Bash '>' redirects "
    "for any path under `.claude/` (a sensitive-dir guard). The campaign state files "
    "(PROGRESS, BOARD.md, INBOX.md) live there, so you MUST write them with `python3` "
    "instead (python file I/O is allowed and is exactly how the frontdesk `fd.py` "
    "writes INBOX.md). For each campaign-dir write — PROGRESS (atomic temp+rename), "
    "the BOARD.md promotion edit, and the INBOX.md rewrite — run a `python3 - "
    "<<'PY' … PY` snippet. Do NOT use Write/Edit on `.claude/` paths.\n"
    "3. PROGRESS (your one liveness duty): at each phase/row transition, atomically "
    "write `$SWITCHBOARD_CAMPAIGN_DIR/PROGRESS` = "
    '{"incarnationId": $SWITCHBOARD_INCARNATION, "phase": <promote|wave|merge|'
    'debrief|idle>, "currentRow": <id-or-null>, "at": <epoch-ms>}. That is how the '
    "supervisor sees forward progress during a long wave — advance it whenever you "
    "start a new row/sub-agent. You do NOT manage pid/lastBeat; the launcher does.\n"
    "4. Then: drain INBOX.md → BOARD and debrief per the skill (promotion-only "
    "unless SWITCHBOARD_AUTO_WAVE is set)."
)
DEFAULT_TIMEOUT = 1800        # promotion-only incarnation cap (30 min)
AUTO_WAVE_TIMEOUT = 7200      # auto-wave: waves legitimately run longer (2 h)
BEAT_INTERVAL_SEC = 60        # launcher liveness beat cadence


# An auto-wave incarnation may advance these ONLY via the tested+reviewed
# auto-merge path (operator ruling 2026-07-28): the lane's full suite ran green
# ON THE MERGE RESULT (not the row branch pre-merge) AND a self-swarm-review
# pass is recorded on the board row — both attested as a JSON line carrying the
# merge-result SHA in <campaignDir>/.merge-approvals.jsonl BEFORE the ref moves
# to that SHA. The campaign branch stays the default merge target; push and
# prod deploy remain separate operator gates. The launcher captures the SHAs
# before/after: a move whose after-SHA matches a fresh attestation is ledgered
# as an advisory `merge-to-master`; anything else is still a
# `merge-target-violation` (the deterministic backstop for the merge-target gap
# seen live on the mac). The `.autowave-active` sentinel (below) lets the
# repo's reference-transaction hook enforce the same SHA-match as a hard block
# at ref-update time.
PROTECTED_REFS = ("refs/heads/master", "refs/heads/main")
MERGE_APPROVAL_FRESH_SEC = 10800   # attestation freshness — matches the hook's 3 h


def _ref_shas(repo: str) -> dict:
    """Current SHA of each protected ref (missing ref → None). No LLM, no trust."""
    out = {}
    for r in PROTECTED_REFS:
        try:
            p = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "-q", r],
                capture_output=True, text=True, timeout=15)
            out[r] = p.stdout.strip() if p.returncode == 0 else None
        except Exception:
            out[r] = None
    return out


def _merge_approved_shas(repo: str) -> set:
    """SHAs attested for the tested+reviewed auto-merge path. The conductor
    appends one JSON line per approved master merge to
    <campaignDir>/.merge-approvals.jsonl —
    {"row","sha","tested","reviewed","at"} — where sha is the merge-RESULT
    commit the suite ran green on and reviewed names the row's recorded review
    pass. Deterministic read, fresh lines only; malformed or stale lines are
    ignored (fail toward violation)."""
    shas = set()
    cutoff_ms = (time.time() - MERGE_APPROVAL_FRESH_SEC) * 1000
    for p in Path(repo).glob(".claude/*/.merge-approvals.jsonl"):
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if (isinstance(rec, dict) and rec.get("sha") and rec.get("tested")
                    and rec.get("reviewed") and rec.get("at", 0) >= cutoff_ms):
                shas.add(rec["sha"])
    return shas


_ROW_ID = re.compile(r"^[A-Za-z]+\d+$")


def board_dispatched(board_path: Path) -> dict:
    """{rowId: trimmed title} for rows currently at status DISPATCHED. Deterministic
    read for the wave-start announce: the conductor writes DISPATCHED claims to the
    BOARD *before* it spawns the row agents (megaloop wave step 2), so polling this
    tells the channel what's being worked the moment work starts — reliably, without
    depending on the LLM conductor to remember to post. Board row shape (§ template):
    `| Tn | title | kind | deps | files | STATUS | branch | verdict | notes |`."""
    out = {}
    try:
        for ln in board_path.read_text().splitlines():
            if not ln.startswith("|"):
                continue
            cells = ln.split("|")
            if len(cells) < 8:
                continue
            rid = cells[1].strip()
            if not _ROW_ID.match(rid) or cells[6].strip() != "DISPATCHED":
                continue
            title = cells[2].strip()
            out[rid] = (title[:57].rstrip() + "…") if len(title) > 60 else title
    except OSError:
        pass
    return out


def _post_channel(raw: dict, channel: str, body: str, marker: str) -> None:
    """Post a marker'd line to the channel as the bot (self-skip + marker keep the
    daemon from dispatching it). Used for auto-wave cost/suppression visibility."""
    req = {"method": "send", "params": {"options": {
        "channel": {"name": raw["team"], "members_type": "team",
                    "topic_name": channel},
        "message": {"body": f"{body}  {marker}"}}}}
    try:
        subprocess.run(
            ["keybase", "-H", os.path.expanduser(raw["bot_home"]), "chat", "api"],
            input=json.dumps(req), capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def _read_progress(campaign_dir: Path, inc: str):
    """The conductor's PROGRESS file (phase/currentRow/at), but only if it belongs
    to THIS incarnation — a stale PROGRESS from a prior run must not make us think
    the current one is progressing (or, worse, look old and trip the stall)."""
    try:
        d = json.loads((campaign_dir / "PROGRESS").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if d.get("incarnationId") != inc:
        return None
    return d


def write_heartbeat(campaign_dir: Path, campaign: str, inc: str, pid: int,
                    launcher_pid: int, phase: str, start_ms: int,
                    transcript: "Path | None" = None, final: bool = False) -> None:
    """Atomically publish the LAUNCHER-owned `<campaignDir>/HEARTBEAT` (§11).

    This is the *liveness* tier (design D): the launcher writes it every ~60s while
    the claude child is alive, so a healthy incarnation of any duration stays fresh
    and the supervisor never false-kills a busy wave. It also folds in the
    *progress* tier so the supervisor can tell busy-but-progressing from stuck:

      progressAt = max(this incarnation's PROGRESS.at, transcript mtime, start)

    — i.e. the last time the incarnation did anything observable (advanced a row,
    or emitted output). currentRow/phase are copied from PROGRESS when present."""
    now = int(time.time() * 1000)
    prog = _read_progress(campaign_dir, inc)
    progress_at = start_ms
    current_row = None
    hb_phase = phase
    # `final` = the launcher's terminal idle beat on exit: force phase/row rather
    # than let the conductor's last PROGRESS (e.g. phase=wave) override it, so the
    # exited incarnation doesn't read as still-in-a-wave.
    if prog and not final:
        progress_at = max(progress_at, int(prog.get("at", 0)))
        current_row = prog.get("currentRow")
        hb_phase = prog.get("phase") or phase
    if transcript is not None:
        try:
            progress_at = max(progress_at, int(transcript.stat().st_mtime * 1000))
        except OSError:
            pass
    hb = {"incarnationId": inc, "pid": int(pid), "launcherPid": int(launcher_pid),
          "campaign": campaign, "phase": hb_phase, "currentRow": current_row,
          "lastBeat": now, "progressAt": progress_at}
    p = campaign_dir / "HEARTBEAT"
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(hb))
    tmp.replace(p)


def load_route(config: str, channel: str):
    raw = yaml.safe_load(Path(config).expanduser().read_text())
    routes = {r["channel"]: r for r in raw.get("routes", [])}
    route = routes.get(channel)
    if route is None:
        sys.exit(f"conductor-run: no route for channel '{channel}' in {config}")
    repo = route.get("repo")
    cd = route.get("campaignDir")
    if not (repo and cd):
        sys.exit(f"conductor-run: channel '{channel}' has no repo+campaignDir "
                 f"(mode={route.get('mode')}); conductor needs a local campaign")
    repo = Path(repo).expanduser()
    campaign_dir = (repo / cd).resolve()
    return raw, route, str(repo), campaign_dir


def main() -> int:
    p = argparse.ArgumentParser(prog="conductor-run")
    p.add_argument("--channel", required=True)
    p.add_argument("--config",
                   default=os.path.expanduser("~/.config/switchboard/routes.yaml"))
    p.add_argument("--auto-wave", action="store_true",
                   help="let the conductor run code waves (autonomy grant)")
    p.add_argument("--timeout", type=int, default=None,
                   help="hard wall-clock cap (default 1800s, or 7200s with --auto-wave)")
    args = p.parse_args()

    raw, route, repo, campaign_dir = load_route(args.config, args.channel)
    if not (campaign_dir / "INBOX.md").exists():
        # No INBOX ⇒ not switchboard-enabled ⇒ nothing chat-driven to do.
        print(f"conductor-run: {campaign_dir}/INBOX.md absent — not switchboard-"
              f"enabled; nothing to do")
        return 0

    # ML-4: non-blocking conductor.lock — skip if another incarnation holds it.
    lock_path = campaign_dir / "conductor.lock"
    lock_fd = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"conductor-run: {lock_path} held by a running incarnation — skip")
        return 0
    lock_fd.seek(0); lock_fd.truncate()
    lock_fd.write(f"{os.getpid()}\n"); lock_fd.flush()

    # --- auto-wave safety harness (autowave.py) --------------------------------
    # An auto-wave run must pass the guards (kill-switch, daily budget, daily run
    # cap) or it downgrades to promotion-only. Promotion-only runs skip all of it.
    defaults = raw.get("defaults", {})
    ledger = autowave.ledger_path(raw.get("state_file",
                                          "~/.config/switchboard/state.json"))
    auto_wave = args.auto_wave
    if args.auto_wave:
        pf = autowave.preflight(defaults, campaign_dir, ledger)
        if not pf["allowed"]:
            auto_wave = False   # downgrade
            print(f"conductor-run: AUTO-WAVE SUPPRESSED — {pf['reason']}; "
                  f"running promotion-only")
            # tell the channel once per reason per day (not every cron cycle)
            if pf["reason_key"] not in autowave.suppressed_keys_today(ledger):
                _post_channel(raw, args.channel,
                    f"⏸️ auto-wave paused — {pf['reason']}. Running promotion-only. "
                    f"(kill-switch: rm the AUTOWAVE_OFF file; budget/run-cap reset "
                    f"at midnight local.)",
                    f"⟦sb:autowave host={raw['host']} kind=suppressed⟧")
                autowave.record(ledger, {"event": "suppressed",
                    "reason_key": pf["reason_key"], "reason": pf["reason"],
                    "channel": args.channel})

    timeout = args.timeout if args.timeout is not None else (
        AUTO_WAVE_TIMEOUT if auto_wave else DEFAULT_TIMEOUT)

    env = dict(os.environ,
        SWITCHBOARD_BOT_HOME=os.path.expanduser(raw["bot_home"]),
        SWITCHBOARD_HOST=raw["host"],
        SWITCHBOARD_APPROVERS=json.dumps(raw.get("approvers", {})),
        SWITCHBOARD_HUMANS=json.dumps(raw.get("humans", [])),
        SWITCHBOARD_CAMPAIGN_DIR=str(campaign_dir),
        SWITCHBOARD_CHANNEL=args.channel,
        # The launcher owns the incarnationId (both HEARTBEAT and the conductor's
        # PROGRESS stamp it, so the supervisor can correlate the two files).
        SWITCHBOARD_INCARNATION=uuid.uuid4().hex[:6],
        # This launcher already holds conductor.lock (flock, above) for the
        # incarnation's lifetime — an LLM can't hold an fd across reasoning
        # steps. Tell the conductor NOT to re-acquire it (else it sees its own
        # launcher's lock and stands down). ML-4 step 0 is the launcher's job.
        SWITCHBOARD_LOCK_HELD=str(os.getpid()))
    # Never let an inherited SWITCHBOARD_AUTO_WAVE (e.g. from a parent/cron env)
    # sneak past the harness downgrade — the launcher is the sole authority.
    env.pop("SWITCHBOARD_AUTO_WAVE", None)
    if auto_wave:
        env["SWITCHBOARD_AUTO_WAVE"] = "1"
        env["SWITCHBOARD_MAX_ROWS_PER_RUN"] = str(
            autowave.caps(defaults)["max_rows_per_run"])

    campaign = campaign_dir.name
    inc = env["SWITCHBOARD_INCARNATION"]
    launcher_pid = os.getpid()
    start_ms = int(time.time() * 1000)
    tdir = campaign_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    transcript = tdir / f"conductor-{stamp}.log"
    mode = ("AUTO-WAVE" if auto_wave else
            "promotion-only (auto-wave suppressed)" if args.auto_wave else
            "promotion-only")
    print(f"conductor-run: {args.channel} ({mode}) inc={inc} → {transcript}")

    def beat(pid: int, phase: str, final: bool = False) -> None:
        write_heartbeat(campaign_dir, campaign, inc, pid, launcher_pid, phase,
                        start_ms, transcript, final=final)

    # Close the supervisor startup race BEFORE the (slow) claude spawn: we already
    # hold conductor.lock, so publish a fresh liveness beat now (pid=us for the
    # microseconds until claude exists), else the supervisor sees lock-held + the
    # previous run's stale beat and false-kills us.
    beat(launcher_pid, "starting")

    rc = 1
    stop_beating = threading.Event()
    # Runaway watchdog (auto-wave only): the beater meters the growing transcript
    # and kills a stuck-looping / wedged incarnation. Backstops are sized so a
    # normal wave never trips them (autowave.watchdog_caps). Distinct from the
    # daily/preflight $ guards — this bounds ONE incarnation's blast radius.
    watchdog = {"tripped": None}
    wdcaps = autowave.watchdog_caps(defaults) if auto_wave else None
    refs_before = _ref_shas(repo) if auto_wave else {}
    # Wave-start announce (operator ask): post to the channel the moment the conductor
    # claims rows (DISPATCHED), so we see what's being worked at start, not just at the
    # end-of-run debrief. Snapshot rows already DISPATCHED (stale claims from a prior
    # budget-killed incarnation) so we announce only THIS run's fresh picks. The
    # launcher owns this post (like HEARTBEAT + the cost line); the conductor skips its
    # own wavestart when SWITCHBOARD_LOCK_HELD is set (SKILL) to avoid a double-post.
    board_path = campaign_dir / "BOARD.md"
    wavestart_seen = set(board_dispatched(board_path)) if auto_wave else set()
    sentinel = campaign_dir / ".autowave-active"
    if auto_wave:
        try:
            sentinel.write_text(json.dumps(
                {"inc": inc, "launcherPid": launcher_pid, "at": start_ms}))
        except OSError:
            pass
    with open(transcript, "w") as tf:
        tf.write(f"# conductor incarnation channel={args.channel} mode={mode} "
                 f"inc={inc} cwd={repo} at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        tf.flush()
        cmd = ["claude", "-p", RESUME_PROMPT, "--allowedTools", CONDUCTOR_TOOLS]
        if auto_wave:
            # stream-json gives us a final result event with total_cost_usd for
            # the ledger, and (bonus) streams output so the transcript mtime keeps
            # progressAt fresh through a long wave — which is also what the
            # runaway watchdog meters (assistant-event count + bytes + mtime age).
            cmd += ["--output-format", "stream-json", "--verbose"]
        proc = subprocess.Popen(
            cmd, cwd=repo, env=env, stdout=tf, stderr=subprocess.STDOUT)
        beat(proc.pid, "running")

        # Liveness thread: refresh HEARTBEAT every BEAT_INTERVAL_SEC while claude
        # is alive. This is design D's liveness tier — a healthy wave of any
        # duration stays fresh; the supervisor distinguishes stuck from busy via
        # progressAt (folded in by write_heartbeat), not by this beat. On an
        # auto-wave run the same tick also runs the runaway watchdog.
        def _beater() -> None:
            while not stop_beating.wait(BEAT_INTERVAL_SEC):
                beat(proc.pid, "running")
                if not auto_wave:
                    continue
                # wave-start announce: any row newly at DISPATCHED since we last looked
                fresh = {r: t for r, t in board_dispatched(board_path).items()
                         if r not in wavestart_seen}
                if fresh:
                    wavestart_seen.update(fresh)
                    picks = ", ".join(f"{r} ({t})" for r, t in fresh.items())
                    _post_channel(raw, args.channel,
                        f"🔨 wave starting — working on: {picks}",
                        f"⟦sb:wavestart host={raw['host']} "
                        f"rows={','.join(fresh)}⟧")
                if watchdog["tripped"] is not None:
                    continue
                metrics = autowave.meter_transcript(transcript)
                try:
                    age = time.time() - transcript.stat().st_mtime
                except OSError:
                    age = 0.0
                dec = autowave.runaway_check(metrics, wdcaps, age)
                if dec["kill"]:
                    watchdog["tripped"] = {**dec, **metrics}
                    print(f"conductor-run: RUNAWAY WATCHDOG — {dec['reason']}; "
                          f"killing inc={inc} pid={proc.pid}")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
        bt = threading.Thread(target=_beater, name="launcher-beat", daemon=True)
        bt.start()

        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            print(f"conductor-run: TIMEOUT after {timeout}s")
            rc = 124
        finally:
            stop_beating.set()
            bt.join(timeout=2)

    # Drop the auto-wave sentinel (best-effort; the hook also ignores stale ones).
    if auto_wave:
        try:
            sentinel.unlink()
        except OSError:
            pass

    # Leave a fresh idle beat with pid=launcher (now exiting → dead), so the
    # supervisor reads lock-free + dead-pid = clean exit, and the lock-free
    # cron-broken detector still has a recent beat to measure the gap from.
    beat(launcher_pid, "idle", final=True)

    # --- auto-wave accounting + visibility -------------------------------------
    if auto_wave:
        dur_sec = round((int(time.time() * 1000) - start_ms) / 1000)
        cost = autowave.parse_cost(transcript)
        cusd = cost.get("cost_usd")
        _, cost_before = autowave.summarize_today(ledger)
        wd = watchdog["tripped"]
        autowave.record(ledger, {"event": "run", "channel": args.channel,
            "inc": inc, "cost_usd": cusd, "num_turns": cost.get("num_turns"),
            "dur_sec": dur_sec, "rc": rc, "subtype": cost.get("subtype"),
            "capped": (wd or {}).get("reason_key")})
        cap = autowave.caps(defaults)["daily_usd"]
        print(f"conductor-run: auto-wave cost=${cusd if cusd is not None else '?'} "
              f"turns={cost.get('num_turns')} today=${cost_before + (cusd or 0):.2f}/${cap:.0f}")
        # Post the cost line for non-trivial runs; idle resume runs (which still
        # cost a few cents) stay quiet, honoring idle-quiet.
        if cusd is not None and cusd >= 0.05:
            _post_channel(raw, args.channel,
                f"🌊 auto-wave inc={inc}: ${cusd:.2f}, {cost.get('num_turns','?')} turns, "
                f"{dur_sec}s — today ${cost_before + cusd:.2f}/${cap:.0f}",
                f"⟦sb:autowave host={raw['host']} kind=cost⟧")
        # Runaway watchdog fired → say so (a kill mid-stream usually has no cost
        # line, so this is the only signal). Commit-first means durable RETURNED
        # branches survive the kill; the loop re-verifies + merges them next time.
        if wd:
            print(f"conductor-run: runaway kill — {wd['reason']}")
            _post_channel(raw, args.channel,
                f"⛔ auto-wave inc={inc} STOPPED by the runaway watchdog — "
                f"{wd['reason']} ({wd.get('assistant_events','?')} turns, "
                f"{wd.get('bytes', 0) / 1048576:.1f} MB, {dur_sec}s). Any committed "
                f"row branches are preserved; investigate the loop before re-running.",
                f"⟦sb:autowave host={raw['host']} kind=runaway⟧")
        # Merge-target backstop: master/main may move ONLY via the approved
        # tested+reviewed auto-merge path (suite green on the merge result +
        # review pass on the row, attested with the merge-result SHA in
        # .merge-approvals.jsonl — operator ruling 2026-07-28). An attested
        # move is advisory; anything else is still a violation.
        refs_after = _ref_shas(repo)
        moved = [r for r, s in refs_after.items()
                 if refs_before.get(r) is not None and s != refs_before.get(r)]
        if moved:
            ok_shas = _merge_approved_shas(repo)
            approved = [r for r in moved if refs_after.get(r) in ok_shas]
            violated = [r for r in moved if r not in approved]
        if moved and approved:
            names = ", ".join(r.split("/")[-1] for r in approved)
            autowave.record(ledger, {"event": "merge-to-master",
                "channel": args.channel, "inc": inc, "refs": approved,
                "before": {r: refs_before.get(r) for r in approved},
                "after": {r: refs_after.get(r) for r in approved}})
            print(f"conductor-run: ℹ️ tested+reviewed auto-merge — {names} advanced "
                  f"during auto-wave inc={inc} (attested)")
            _post_channel(raw, args.channel,
                f"ℹ️ auto-merge — auto-wave inc={inc} advanced {names} via the "
                f"tested+reviewed path (suite green on the merge result, review "
                f"recorded on the row). Push + prod deploy remain operator gates.",
                f"⟦sb:autowave host={raw['host']} kind=merge-approved⟧")
        if moved and violated:
            names = ", ".join(r.split("/")[-1] for r in violated)
            autowave.record(ledger, {"event": "merge-target-violation",
                "channel": args.channel, "inc": inc, "refs": violated,
                "before": {r: refs_before.get(r) for r in violated},
                "after": {r: refs_after.get(r) for r in violated}})
            print(f"conductor-run: ⚠️ MERGE-TARGET VIOLATION — {names} moved during "
                  f"auto-wave inc={inc}")
            _post_channel(raw, args.channel,
                f"⚠️ merge-target VIOLATION — auto-wave inc={inc} advanced {names} "
                f"(protected) with NO tested+reviewed attestation. Master merges "
                f"require suite-green-on-merge-result + a review pass on the row "
                f"(.merge-approvals.jsonl); otherwise merge to the campaign branch. "
                f"Push is a separate operator gate. Review + reset {names} before "
                f"any push.",
                f"⟦sb:autowave host={raw['host']} kind=merge-violation⟧")

    print(f"conductor-run: exit {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
