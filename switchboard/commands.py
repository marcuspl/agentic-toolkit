"""commands — in-daemon command handlers (SB-4 control + SB-6 status/nudge).

Answered *inside* the daemon, zero `claude -p` spawn, zero token cost
(PROTOCOL §3 note; invariant §10.4 — no LLM in this path). The daemon's
decide() routes any DAEMON_COMMANDS first token here; this module enforces the
per-command authz (PROTOCOL §3/§6) and does the work:

  !ping    any human            → "pong"
  !pause   approvers.default     → stop dispatching (daemon keeps acking ⏸️)
  !resume  approvers.default     → resume dispatching
  !status  any human            → render BOARD + HEARTBEAT summary (pure parse)
  !nudge   approvers.default     → fire an immediate `claude -p /megaloop resume`
                                   in the routed repo, iff conductor.lock is free

Every reply carries a ⟦sb:…⟧ marker (invariant §10.6) so it is never
re-dispatched. Unauthorized senders are dropped with no state change (§6).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import autowave
import ledger
from supervisor import lock_is_held, read_heartbeat

# BOARD status vocabulary (STATE_MODEL.md axis 2) — for the pure-parse !status.
STATUSES = [
    "TODO", "BLOCKED", "CLAIMED", "DISPATCHED", "IN-PROGRESS", "SELF-REVIEW",
    "RETURNED", "MERGED", "DONE", "GATED", "DEFERRED", "WONTFIX", "FAILED",
]

# Gate-class vocabulary (PROTOCOL §6). Longest-first so a substring scan of a
# queue line matches "deploy-gate" before a bare "gate".
GATE_CLASSES = [
    "product-question", "security-crux", "fix-and-ship", "deploy-gate",
    "push-gate", "fleet-gate", "secrets/target",
]

# What each gate class actually MEANS to the operator reading `!status` on a
# phone (T26). One line, honest about the consequence of `!approve` — a gloss
# that hid "this ships to users" would be worse than no gloss at all. Keyed by
# GATE_CLASSES; "" is the fallback for a row whose class the BOARD never stamped.
GATE_GLOSS = {
    "deploy-gate":      "approve = the daemon BUILDS + UPLOADS a TestFlight build (real users get it)",
    "fix-and-ship":     "approve = MERGES the branch AND SHIPS it (build + TestFlight upload, one step)",
    "push-gate":        "approve = commits get pushed / a PR opens — code leaves this machine",
    "fleet-gate":       "approve = live infra is mutated (running daemon/host/service) — effective at once",
    "security-crux":    "a security call (authz / secrets / crypto) only a human may make",
    "product-question": "a product/UX decision only you can make — the row is parked on your answer",
    "secrets/target":   "needs a credential or deploy target you supply — none are stored in the repo",
    "":                 "gate class NOT recorded on the board — read the row before approving",
}

# Gate classes that fire a route-configured executor on approve (phone-loop
# spec §2/§5). The command is an argv LIST in routes.yaml ({row} substituted,
# nothing shell-interpolated); the executor re-validates the on-disk approval
# itself, so this hook adds *execution*, never authority — no LLM in the
# deploy path (§10.4).
GATE_COMMAND_KEYS = {
    "deploy-gate": "ship_command",
    "fix-and-ship": "integrate_and_ship_command",
}

# A row id is a short alnum token (T45, INB-7, ML-3a). Bounds the value written
# into approvals/<id>.json — no path separators, no injection payloads.
_ROW_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _first_token(body: str) -> str:
    body = (body or "").strip()
    return body.split()[0].lower() if body else ""


class DaemonCommands:
    def __init__(self, daemon):
        self.d = daemon           # back-ref: .paused, .start_ts, .cfg, .kb, .log
        self.cfg = daemon.cfg
        self.kb = daemon.kb
        self.log = daemon.log

    # ---- entry (called from Daemon.handle on DAEMON_CMD) ------------------
    def handle(self, env) -> None:
        tok = _first_token(env.body)
        fn = {
            "!ping": self._ping,
            "!pause": self._pause,
            "!resume": self._resume,
            "!status": self._status,
            "!nudge": self._nudge,
            "!approve": self._approve,
            "!recap": self._recap,
            "!retry": self._retry,
            "!drop": self._drop,
        }.get(tok)
        if fn is None:
            self.log(f"daemon-cmd: unknown {tok!r} — ignored")
            return
        try:
            fn(env)
        except Exception as e:
            self.log(f"daemon-cmd {tok} error: {e}")

    # ---- authz (PROTOCOL §3/§6) ------------------------------------------
    def _is_human(self, env) -> bool:
        return env.sender in self.cfg.humans

    def _is_approver(self, env) -> bool:
        return env.sender in (self.cfg.approvers or {}).get("default", [])

    def _approvers_for(self, gate_class: str) -> list:
        """Whitelist for a gate class (PROTOCOL §6): an explicit per-class key
        wins, else `default`. Mirrors frontdesk fd.py:approver_list so both
        paths authz identically."""
        ap = self.cfg.approvers or {}
        if gate_class and gate_class in ap:
            return ap.get(gate_class) or []
        return ap.get("default", [])

    def _deny(self, env, cmd: str) -> None:
        # §6: dropped with no state change. Stay quiet (no reply) — don't give an
        # unauthorized sender a control surface, and don't add injection noise.
        self.log(f"daemon-cmd {cmd}: {env.sender} not authorized — dropped")

    def _reply(self, env, body: str, kind: str) -> None:
        self.kb.reply(env, body, marker=f"⟦sb:{kind} host={self.cfg.host}⟧")

    # ---- commands --------------------------------------------------------
    def _ping(self, env) -> None:
        if not self._is_human(env):
            self._deny(env, "!ping")
            return
        self._reply(env, "pong", kind="pong")

    def _pause(self, env) -> None:
        if not self._is_approver(env):
            self._deny(env, "!pause")
            return
        self.d.paused = True
        self._reply(
            env,
            "⏸️ paused — new dispatchable messages will be acked (⏸️) but not "
            "dispatched until `!resume`",
            kind="reply",
        )
        self.log(f"daemon PAUSED by {env.sender}")

    def _resume(self, env) -> None:
        if not self._is_approver(env):
            self._deny(env, "!resume")
            return
        self.d.paused = False
        self._reply(env, "▶️ resumed — dispatching again", kind="reply")
        self.log(f"daemon RESUMED by {env.sender}")

    # ---- !status (SB-6, PROTOCOL §8; pure parse, zero LLM) ----------------
    def _status(self, env) -> None:
        if not self._is_human(env):
            self._deny(env, "!status")
            return
        route = self.cfg.routes.get(env.channel, {})
        self._reply(env, self._render_status(env.channel, route), kind="status")

    def _render_status(self, channel: str, route: dict) -> str:
        repo, cd = route.get("repo"), route.get("campaignDir")
        if not (repo and cd):
            tail = "  ·  ⏸️ paused" if self.d.paused else ""
            return (f"#{channel} — routing-only channel (no local campaign)\n"
                    f"daemon: up {self._uptime()}{tail}")
        cdir = (Path(repo).expanduser() / cd).resolve()
        board = self._board_summary(cdir / "BOARD.md", channel)
        gates = self._gate_section(cdir / "BOARD.md")
        gate_block = f"\n{gates}" if gates else ""
        beat = self._beat_summary(cdir / "HEARTBEAT")
        tail = "  ·  ⏸️ paused" if self.d.paused else ""
        aw = self._autowave_summary(cdir)
        aw_line = f"\n{aw}" if aw else ""
        return (f"{board}{gate_block}\n{beat}  ·  daemon: up {self._uptime()}"
                f"{tail}{aw_line}")

    def _autowave_summary(self, cdir: Path) -> "str | None":
        """Today's auto-wave spend vs caps + kill-switch state (visibility for
        !status). None when there's been no auto-wave activity today."""
        try:
            lp = autowave.ledger_path(self.cfg.state_file)
            runs, cost = autowave.summarize_today(lp)
            ks = autowave.killswitch(cdir)
            if runs == 0 and not ks:
                return None
            c = autowave.caps(self.cfg.defaults or {})
            s = (f"auto-wave today: {runs}/{c['max_runs_day']} runs, "
                 f"${cost:.2f}/${c['daily_usd']:.0f}")
            return s + ("  ·  🛑 AUTOWAVE_OFF" if ks else "")
        except Exception:
            return None

    @staticmethod
    def _board_rows(text: str) -> list:
        """The BOARD's work rows → [{id, title, status, deps, gate_class}].

        This is the SAME single scan `!status` has always used for its counts,
        just lifted out so the gate section can reuse it (no second parser): a
        markdown row with EXACTLY ONE cell drawn from STATUSES is a real work
        row — legends, headers and the prose tables have none or many. Column
        *positions* differ between boards, so we learn them from the nearest
        `| ID | … | Status |` header and fall back to id=col0, title=col1.
        Pure parse, zero LLM (§10.4)."""
        rows: list[dict] = []
        hdr: dict[str, int] = {}
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            hits = [c for c in cells if c in STATUSES]
            if len(hits) != 1:
                low = [c.lower().strip("*` ") for c in cells]
                if "id" in low and "status" in low:   # a table header row
                    hdr = {k: low.index(k)
                           for k in ("id", "item", "deps", "kind") if k in low}
                continue

            def cell(key: str, default: str = "") -> str:
                i = hdr.get(key)
                return cells[i] if i is not None and i < len(cells) else default

            raw = cell("id", cells[0] if cells else "").strip("*` ")
            if not _ROW_ID_RE.fullmatch(raw):
                continue                              # junk / prose row
            deps = cell("deps").strip("*` ")
            rows.append({
                "id": raw,
                "title": DaemonCommands._trim(
                    cell("item", cells[1] if len(cells) > 1 else "")),
                "status": hits[0],
                "deps": "" if deps in ("", "—", "-", "–") else deps,
                "gate_class": DaemonCommands._row_gate_class(line, cell("kind")),
            })
        return rows

    @staticmethod
    def _row_gate_class(line: str, kind: str) -> str:
        """A row's gate class from its own line: the conductor stamps
        `gate_class=<class>` in Notes (ML-3); failing that, the ⛔ Kind values
        (deploy/push/fleet) name their gate. Never guessed from free text — an
        Item that merely *mentions* "deploy-gate" must not be read as one."""
        m = re.search(r"gate_class\s*=\s*`?([A-Za-z][A-Za-z/-]*)", line)
        if m:
            tok = m.group(1).lower()
            gc = next((g for g in GATE_CLASSES if tok.startswith(g)), "")
            if gc:
                return gc
        k = (kind or "").lower().strip("*` ⛔")
        return f"{k}-gate" if f"{k}-gate" in GATE_CLASSES else ""

    @staticmethod
    def _trim(s: str, n: int = 64) -> str:
        s = re.sub(r"\s+", " ", (s or "").strip().strip("*")).strip()
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"

    def _board_summary(self, board_path: Path, channel: str) -> str:
        try:
            text = board_path.read_text()
        except (FileNotFoundError, OSError):
            return f"{channel} — no BOARD.md found"
        rows = self._board_rows(text)
        if not rows:
            return f"{channel} — 0 rows"
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        parts = " · ".join(f"{counts[s]} {s}" for s in STATUSES if counts.get(s))
        return f"{channel} — {len(rows)} rows: {parts}"

    def _gate_section(self, board_path: Path) -> "str | None":
        """T26: name every open gate instead of a bare `1 GATED` count — for each
        gated row, its gate_class CODE, a one-line gloss of what approving it
        actually does, and the exact `!approve` + who may send it. BLOCKED rows
        ride along (the operator asked for "gated/blocked") but carry NO approve
        line: they wait on deps, not on a human.

        `_gate_queue()` — the same parse `!approve` authorizes against — is the
        source of truth for "approvable right now": a GATED row missing from it
        would BOUNCE an `!approve`, so we say so rather than print an
        instruction that silently fails. Empty section when nothing is gated or
        blocked: an idle `!status` stays one line. Pure parse, zero LLM."""
        try:
            text = board_path.read_text()
        except (FileNotFoundError, OSError):
            return None
        rows = self._board_rows(text)
        queue = self._gate_queue(board_path)     # {id: gateClass} = approvable now
        gated = [r for r in rows if r["status"] == "GATED"]
        blocked = [r for r in rows if r["status"] == "BLOCKED"]
        if not gated and not blocked:
            return None
        out: list[str] = []
        if gated:
            out.append(f"🚦 GATED ({len(gated)}) — waiting on you:")
        for r in gated:
            # The queue's class is the one `!approve` authorizes against, so the
            # approver list MUST come from it (never from the row's own stamp) —
            # otherwise we'd name a whitelist the command doesn't actually use.
            # The row's stamp only fills in the *display* class when the queue
            # line omitted it.
            authz = queue.get(r["id"])
            gc = authz or r["gate_class"]
            out.append(f"• {r['id']}  [{gc or 'gate?'}] — {GATE_GLOSS.get(gc, GATE_GLOSS[''])}")
            if r["title"]:
                out.append(f"   {r['title']}")
            if r["id"] in queue:
                who = ", ".join(self._approvers_for(authz or "")) or "nobody configured (!)"
                out.append(f"   → `!approve {r['id']}`  (from: {who})")
            else:
                out.append(f"   ⚠️ not in the board's gate queue yet — `!approve "
                           f"{r['id']}` will bounce until a conductor posts it "
                           f"(`!nudge`)")
        if blocked:
            out.append(f"⛔ BLOCKED ({len(blocked)}) — waiting on deps, not on you "
                       f"(no `!approve`):")
            for r in blocked:
                dep = f" ← needs {r['deps']}" if r["deps"] else ""
                out.append(f"• {r['id']}{dep}  {r['title']}")
        return "\n".join(out)

    def _beat_summary(self, hb_path: Path) -> str:
        hb = read_heartbeat(hb_path)
        if hb is None:
            return "conductor: no heartbeat (idle / never run)"
        ago = self._ago(int(hb.get("lastBeat", 0)))
        prog = ""
        if hb.get("progressAt"):
            prog = f", progress {self._ago(int(hb['progressAt']))}"
        return (f"conductor: last beat {ago} "
                f"(phase={hb.get('phase', '?')}, row={hb.get('currentRow')}{prog})")

    # ---- !nudge (SB-6) ---------------------------------------------------
    def _nudge(self, env) -> None:
        if not self._is_approver(env):
            self._deny(env, "!nudge")
            return
        route = self.cfg.routes.get(env.channel, {})
        repo, cd = route.get("repo"), route.get("campaignDir")
        if not (repo and cd):
            self._reply(env, "can't `!nudge` — this channel has no local campaign",
                        kind="reply")
            return
        cdir = (Path(repo).expanduser() / cd).resolve()
        if lock_is_held(cdir / "conductor.lock"):
            self._reply(
                env,
                f"conductor already running — {self._beat_summary(cdir / 'HEARTBEAT')}",
                kind="reply",
            )
            return
        self._fire_conductor(env)

    def _fire_conductor(self, env, autowave: bool = False) -> None:
        # Fire through conductor-run.py — the ONE launcher. A bare `claude -p
        # /megaloop resume` would run with no tool allowlist (denies every tool
        # headless), no conductor.lock, no SWITCHBOARD_* env, and no liveness
        # beats. The launcher takes the lock, sets the env, passes the allowlist,
        # and owns HEARTBEAT — exactly like the cron path.
        launcher = str(Path(__file__).resolve().parent / "conductor-run.py")
        logdir = Path(self.cfg.state_file).expanduser().parent / "conductor-logs"
        logdir.mkdir(parents=True, exist_ok=True)
        logpath = logdir / f"nudge-{env.channel}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        argv = ["--channel", env.channel] + (["--auto-wave"] if autowave else [])
        try:
            lf = open(logpath, "w")
            # sys.executable, NOT bare "python3" (same fix as trigger.py): under
            # launchd, PATH's python3 is Homebrew 3.12 without pyyaml — every
            # !nudge-fired conductor on the mac died on `import yaml` before this.
            subprocess.Popen(
                [sys.executable, launcher] + argv,
                stdout=lf, stderr=subprocess.STDOUT,
                env=os.environ.copy(), start_new_session=True,
            )
        except OSError as e:
            self._reply(env, f"failed to fire conductor: {e}", kind="reply")
            return
        self._reply(
            env,
            "🔔 fired a conductor incarnation (`conductor-run.py`, promotion-only) "
            "— watch for a debrief",
            kind="reply",
        )
        self.log(f"!nudge by {env.sender} → conductor-run.py {env.channel} ({logpath})")

    # ---- !retry / !drop (phone-loop spec §3; deterministic, zero LLM) -------
    def _retry(self, env) -> None:
        self._control(env, "!retry", "retry",
                      "🔁 retry filed for `{rid}` — firing a conductor to pick "
                      "it up", fire=True)

    def _drop(self, env) -> None:
        self._control(env, "!drop", "drop",
                      "🗑️ drop filed for `{rid}` — the conductor will mark it "
                      "dropped on its next incarnation", fire=False)

    def _control(self, env, cmd: str, action: str, ok_msg: str,
                 fire: bool) -> None:
        """File a control record the conductor consumes (SKILL step 3b). The
        daemon NEVER edits the BOARD (single-writer): like approvals/, a
        sender-stamped file in <campaign>/control/ carries the operator's
        intent and the conductor applies it. Authz: any listed human (§3) —
        retry/drop schedule work, they don't flip gates."""
        if not self._is_human(env):
            self._deny(env, cmd)
            return
        route = self.cfg.routes.get(env.channel, {})
        repo, cd = route.get("repo"), route.get("campaignDir")
        if not (repo and cd):
            self._reply(env, f"can't `{cmd}` — this channel has no local "
                        "campaign", kind="reply")
            return
        rid = self._approve_target(env.body)   # same "<cmd> <row-id>" shape
        if not rid:
            self._reply(env, f"usage: `{cmd} <row-id>` (e.g. `{cmd} T45`)",
                        kind="reply")
            return
        cdir = (Path(repo).expanduser() / cd).resolve()
        rec = {"id": rid, "action": action, "by": env.sender,
               "channel": env.channel, "msgId": int(env.msg_id),
               "sentAt": int(getattr(env, "sent_at", 0) or 0)}
        ctl = cdir / "control"
        ctl.mkdir(parents=True, exist_ok=True)
        tmp = ctl / f".{action}-{rid}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False))
        tmp.replace(ctl / f"{action}-{rid}.json")   # atomic
        ledger.append(cdir, rid, f"{action}-filed", "daemon",
                      {"by": env.sender})
        self._reply(env, ok_msg.format(rid=rid), kind=action)
        self.log(f"daemon-cmd {cmd} {rid} by {env.sender} → "
                 f"control/{action}-{rid}.json")
        if fire and not lock_is_held(cdir / "conductor.lock"):
            self._fire_conductor(env, autowave=True)

    # ---- !approve (SB-6 / PROTOCOL §6; deterministic, zero LLM) -----------
    def _approve(self, env) -> None:
        """Release a gated row by filing a sender-stamped approval record — the
        deterministic authz + atomic file-write that MUST NOT run under an LLM
        (invariant §10.4). We: parse the row id, read the row's gate class from
        the BOARD's operator-gate queue (the canonical "what's approvable now"),
        authz the sender against that class's whitelist, and write
        approvals/<id>.json with the exact schema fd.py uses. The conductor
        re-validates + consumes it once (ML-3). We NEVER edit the BOARD.

        Unauthorized / off-target approves cause NO state change. A corrective
        reply goes only to a known human (typo help); everyone else gets silence
        — no control surface, no injection echo (§6)."""
        rid = self._approve_target(env.body)
        route = self.cfg.routes.get(env.channel, {})
        repo, cd = route.get("repo"), route.get("campaignDir")
        cdir = (Path(repo).expanduser() / cd).resolve() if (repo and cd) else None
        queue = self._gate_queue(cdir / "BOARD.md") if cdir else {}
        # queue maps id -> gateClass (""=in queue, class unknown). `.get` returns
        # None only when the id is absent — i.e. not a currently-gated row.
        gate_class = queue.get(rid) if rid else None
        authorized = (
            rid is not None and gate_class is not None
            and env.sender in self._approvers_for(gate_class)
        )
        if not authorized:
            if self._is_human(env):
                if not rid:
                    self._reply(env, "usage: `!approve <row-id>` — see the gate "
                                "queue in `!status`", kind="reply")
                elif cdir is None:
                    self._reply(env, "can't `!approve` here — this channel has no "
                                "local campaign", kind="reply")
                elif gate_class is None:
                    self._reply(env, f"`{rid}` isn't in the gate queue — nothing "
                                "to approve. `!status` shows open gates.",
                                kind="reply")
                # human but not an approver for this class → stay quiet (§6);
                # the queue line already names who may approve.
            self.log(f"daemon-cmd !approve {rid or '<none>'}: unauthorized/"
                     f"off-target from {env.sender} — dropped")
            return
        rec = {
            "id": rid,
            "approvedBy": env.sender,
            "channel": env.channel,
            "msgId": int(env.msg_id),
            "sentAt": int(getattr(env, "sent_at", 0) or 0),
            "gateClass": gate_class or "",
        }
        try:
            self._write_approval(cdir, rid, rec)
        except OSError as e:
            self._reply(env, f"failed to file approval for `{rid}`: {e}",
                        kind="reply")
            return
        ledger.append(cdir, rid, "approved", "daemon",
                      {"approvedBy": env.sender, "msgId": int(env.msg_id),
                       "gateClass": gate_class or ""})
        self._reply(
            env,
            f"✅ approval filed for `{rid}` ({gate_class or 'gate'}) by "
            f"{env.sender} — the conductor releases it on its next incarnation. "
            f"`!nudge` to run one now.",
            kind="approve",
        )
        self.log(f"daemon-cmd !approve {rid} by {env.sender} "
                 f"({gate_class or 'default'}) → approvals/{rid}.json")
        self._on_approve_command(env, route, cdir, rid, gate_class)

    def _on_approve_command(self, env, route: dict, cdir: Path, rid: str,
                            gate_class: str) -> None:
        """Fire the route's executor for this gate class, detached — exactly
        the `!nudge`→conductor-run spawn pattern (spec §2). One message →
        build+upload, zero manual steps, zero LLM in the path."""
        key = GATE_COMMAND_KEYS.get(gate_class or "")
        template = route.get(key) if key else None
        if not template:
            return
        if (not isinstance(template, list)
                or not all(isinstance(a, str) for a in template)):
            self.log(f"{key} for {env.channel} must be an argv list of strings "
                     "— not firing")
            return
        argv = [a.replace("{row}", rid) for a in template]
        repo = str(Path(route["repo"]).expanduser())
        logdir = Path(self.cfg.state_file).expanduser().parent / "ship-logs"
        logdir.mkdir(parents=True, exist_ok=True)
        logpath = logdir / f"{gate_class}-{rid}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        try:
            lf = open(logpath, "w")
            self._popen(argv, cwd=repo, stdout=lf)
        except OSError as e:
            self._reply(env, f"⚠️ approval for `{rid}` is filed, but firing "
                        f"`{key}` failed: {e}", kind="reply")
            return
        ledger.append(cdir, rid, "ship-fired", "daemon",
                      {"cmd": key, "by": env.sender})
        self._reply(env, f"🚀 `{key}` fired for `{rid}` — its interlock "
                    "re-validates the approval from disk; watch this thread "
                    "for progress.", kind="ship")
        self.log(f"{key} fired for {rid}: {argv} → {logpath}")

    @staticmethod
    def _popen(argv: list, cwd: str, stdout) -> None:
        """Detached spawn; separate method so the selftest can record instead
        of executing."""
        subprocess.Popen(argv, cwd=cwd, stdout=stdout,
                         stderr=subprocess.STDOUT, env=os.environ.copy(),
                         start_new_session=True)

    # ---- !recap (phone-loop spec §6; pure parse, zero LLM) ------------------
    def _recap(self, env) -> None:
        """Render the campaign work ledger — 'what happened while I was out'
        as one phone-sized message. `!recap` = last 24h, `!recap 6` = last 6h,
        `!recap all` = everything."""
        if not self._is_human(env):
            self._deny(env, "!recap")
            return
        route = self.cfg.routes.get(env.channel, {})
        repo, cd = route.get("repo"), route.get("campaignDir")
        if not (repo and cd):
            self._reply(env, "can't `!recap` — this channel has no local "
                        "campaign", kind="reply")
            return
        cdir = (Path(repo).expanduser() / cd).resolve()
        parts = (env.body or "").strip().split()
        hours: "float | None" = 24.0
        if len(parts) >= 2:
            arg = parts[1].strip().lower()
            if arg == "all":
                hours = None
            else:
                try:
                    hours = max(0.1, float(arg))
                except ValueError:
                    pass   # keep the 24h default on junk input
        self._reply(env, ledger.render(cdir, since_hours=hours), kind="recap")

    @staticmethod
    def _approve_target(body: str) -> "str | None":
        """The row id in `!approve <id>`, sanitized. None if missing/malformed."""
        parts = (body or "").strip().split()
        if len(parts) < 2:
            return None
        raw = parts[1].strip().strip("`").strip(",.")
        m = _ROW_ID_RE.fullmatch(raw)
        return m.group(0) if m else None

    def _gate_queue(self, board_path: Path) -> dict:
        """Parse the BOARD's operator-gate queue → {rowId: gateClass}. The
        conductor writes one line per still-GATED row carrying an explicit
        `!approve <id>` and its gate class (ML-3), e.g.
        `• T45  push-gate → !approve T45  (operator)`. Presence in this
        section IS the "row is GATED" check — the conductor only lists rows that
        are gated after the wave/merge."""
        try:
            text = board_path.read_text()
        except (FileNotFoundError, OSError):
            return {}
        out: dict[str, str] = {}
        in_q = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("## "):
                in_q = s.lower().startswith("## operator-gate queue")
                continue
            if not in_q or not s:
                continue
            m = re.search(r"!approve\s+`?([A-Za-z0-9][A-Za-z0-9._-]{0,63})`?", s)
            if not m:
                continue
            rid = m.group(1)
            gc = next((g for g in GATE_CLASSES if g in s), "")
            out[rid] = gc
        return out

    @staticmethod
    def _write_approval(cdir: Path, rid: str, rec: dict) -> Path:
        """Atomic write of approvals/<id>.json (temp+rename). Same schema +
        location fd.py uses; the daemon alone creates this file for its host."""
        adir = cdir / "approvals"
        adir.mkdir(parents=True, exist_ok=True)
        dest = adir / f"{rid}.json"
        tmp = adir / f".{rid}.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False))
        tmp.replace(dest)  # atomic
        return dest

    # ---- formatting ------------------------------------------------------
    def _uptime(self) -> str:
        return self._dur(time.time() - self.d.start_ts)

    def _ago(self, ms: int) -> str:
        if not ms:
            return "never"
        return self._dur(max(0.0, time.time() - ms / 1000.0)) + " ago"

    @staticmethod
    def _dur(secs: float) -> str:
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        m, s = divmod(secs, 60)
        if m < 60:
            return f"{m}m"
        h, m = divmod(m, 60)
        if h < 24:
            return f"{h}h{m:02d}m"
        d, h = divmod(h, 24)
        return f"{d}d{h:02d}h"


# ── selftest (no Keybase, no network) — exercises the !approve gate path ───────
def selftest() -> int:
    """Adversarial coverage of _approve: authz by gate class, silent drops for
    off-target/unauthorized/bot, path-injection rejection, atomic record shape."""
    import tempfile
    from types import SimpleNamespace

    BOARD = (
        "# BOARD — test\n\n"
        "| ID | Item | Kind | Deps | Status | Notes |\n|--|--|--|--|--|--|\n"
        "| T45 | Push the release branch to origin | push | — | GATED | gate_class=push-gate |\n"
        "| T46 | Nav-bar or menu for the export button? | design | — | GATED | gate_class=product-question |\n"
        "| T47 | Cut a TestFlight build (0.15.0→0.16.0) | deploy | — | GATED | gate_class=deploy-gate |\n"
        "| T48 | Land the export hotfix and get it out | code | — | GATED | gate_class=fix-and-ship |\n"
        "| T99 | Rotate the relay token on the live host | fleet | — | GATED | (class unstamped) |\n"
        "| T60 | Depends on the gated rows | code | T45,T47 | BLOCKED | — |\n"
        "| T61 | Already landed | code | — | MERGED | — |\n"
        "| T62 | Gated but the conductor never queued it | deploy | — | GATED | gate_class=deploy-gate |\n\n"
        "## Operator-gate queue (nothing here runs autonomously)\n"
        "• T45  push-gate → `!approve T45`  (operator)\n"
        "• T46  product-question → `!approve T46`  (operator / teammate)\n"
        "• T47  deploy-gate → `!approve T47`  (operator)\n"
        "• T48  fix-and-ship → `!approve T48` — MERGES + SHIPS  (operator)\n"
        "• T99  → `!approve T99`  (unknown gate class)\n"
        "\n## Something else\n• T50  push-gate → `!approve T50` (should NOT parse — wrong section)\n"
    )

    tmp = Path(tempfile.mkdtemp(prefix="sb-approve-"))
    (tmp / "campaign").mkdir()
    (tmp / "campaign" / "BOARD.md").write_text(BOARD)

    cfg = SimpleNamespace(
        host="workstation",
        humans=["operator", "teammate"],
        approvers={"default": ["operator"],
                   "product-question": ["operator", "teammate"]},
        routes={"app": {"channel": "app", "repo": str(tmp),
                               "campaignDir": "campaign"},
                "product-chat": {"channel": "product-chat", "mode": "route"}},
        defaults={}, state_file=str(tmp / "state.json"),
    )
    replies: list = []
    kb = SimpleNamespace(reply=lambda env, body, marker=None:
                         replies.append((body, marker)))
    daemon = SimpleNamespace(cfg=cfg, kb=kb, log=lambda *a: None,
                             paused=False, start_ts=time.time())
    cmds = DaemonCommands(daemon)

    def env(sender="operator", body="!approve T45", channel="app",
            mid=812):
        return SimpleNamespace(sender=sender, channel=channel, msg_id=mid,
                               body=body, sent_at=1783480343575)

    def approval(rid):
        p = tmp / "campaign" / "approvals" / f"{rid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    cases = []

    def run(name, e, *, expect_file=None, expect_reply=None, expect_marker=None):
        replies.clear()
        cmds._approve(e)
        ok = True
        rec = approval(expect_file) if expect_file else None
        if expect_file:
            ok = ok and rec is not None
        if expect_reply is True:
            ok = ok and len(replies) == 1
        elif expect_reply is False:
            ok = ok and len(replies) == 0
        if expect_marker and replies:
            ok = ok and expect_marker in (replies[0][1] or "")
        cases.append((name, ok, rec))
        return rec

    # 1. authorized approver, push-gate → filed, correct schema, approve marker
    rec = run("marcus approves push-gate T45 → filed",
              env(), expect_file="T45", expect_reply=True, expect_marker="approve")
    schema_ok = rec == {"id": "T45", "approvedBy": "operator",
                        "channel": "app", "msgId": 812,
                        "sentAt": 1783480343575, "gateClass": "push-gate"}
    cases.append(("T45 record matches fd.py schema exactly", schema_ok, rec))

    # 2. magnus (product-question approver) on a PUSH gate → denied, silent, no file
    run("magnus on push-gate T45 → denied silently",
        env(sender="teammate", body="!approve T45", mid=813),
        expect_reply=False)
    cases.append(("magnus did NOT write a push-gate approval",
                  approval("T45")["approvedBy"] == "operator", None))  # unchanged

    # 3. magnus on product-question T46 → filed (per-class whitelist)
    run("magnus approves product-question T46 → filed",
        env(sender="teammate", body="!approve T46", mid=814),
        expect_file="T46", expect_reply=True)

    # 4. bot (non-human, non-approver) → silent drop, no file, no reply
    run("bot !approve T45 → silent drop",
        env(sender="artilect_ios", body="!approve T45", mid=815),
        expect_reply=False)
    cases.append(("bot wrote no new approval file",
                  approval("T99") is None, None))

    # 5. unknown-gate-class row T99 → default whitelist: marcus yes, magnus no
    run("marcus approves unknown-class T99 → filed (default whitelist)",
        env(body="!approve T99", mid=816), expect_file="T99", expect_reply=True)
    run("magnus on unknown-class T99 → denied silently",
        env(sender="teammate", body="!approve T99", mid=817),
        expect_reply=False)

    # 6. not-in-queue id (human) → corrective reply, no file
    run("human !approve T404 (not gated) → corrective reply, no file",
        env(body="!approve T404", mid=818), expect_reply=True)
    cases.append(("no approval file for non-gated T404",
                  approval("T404") is None, None))

    # 7. malformed (no id) from human → usage reply, no file
    run("human bare !approve → usage reply",
        env(body="!approve", mid=819), expect_reply=True)

    # 8. path-injection id → rejected (parsed as malformed), no traversal write
    run("path-injection !approve ../../etc/passwd → rejected",
        env(body="!approve ../../etc/passwd", mid=820), expect_reply=True)
    traversal = (tmp.parent / "etc" / "passwd.json")
    cases.append(("no path-traversal file created", not traversal.exists(), None))

    # 9. wrong-section line (T50) must NOT be treated as gated
    run("T50 (outside gate-queue section) → not approvable",
        env(body="!approve T50", mid=821), expect_reply=True)
    cases.append(("T50 outside queue section not filed",
                  approval("T50") is None, None))

    # 10. idempotent re-approve of T45 by marcus → still valid, overwrites cleanly
    run("marcus re-approves T45 → idempotent",
        env(mid=822), expect_file="T45", expect_reply=True)

    # 11. ledger + !recap (phone-loop spec §6)
    lg = tmp / "campaign" / "ledger.jsonl"
    cases.append(("_approve appended an `approved` ledger event",
                  lg.exists() and '"event": "approved"' in lg.read_text(), None))
    replies.clear()
    cmds._recap(env(body="!recap 24", mid=823))
    cases.append(("!recap replies with the row timeline",
                  len(replies) == 1 and "T45" in replies[0][0], None))
    replies.clear()
    cmds._recap(env(sender="artilect_ios", body="!recap", mid=824))
    cases.append(("bot !recap → silent drop", len(replies) == 0, None))
    replies.clear()
    cmds._recap(env(channel="product-chat", body="!recap", mid=825))
    cases.append(("!recap on routing-only channel → friendly no-campaign reply",
                  len(replies) == 1 and "no local campaign" in replies[0][0],
                  None))

    # 12. !retry / !drop (spec §3) — control files, never BOARD edits
    fires: list = []
    cmds._fire_conductor = lambda e, autowave=False: fires.append(autowave)

    def control(action, rid):
        p = tmp / "campaign" / "control" / f"{action}-{rid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    replies.clear()
    cmds._retry(env(body="!retry T45", mid=826))
    rec = control("retry", "T45")
    cases.append(("human !retry → control/retry-T45.json + reply + autowave fire",
                  rec is not None and rec["action"] == "retry"
                  and rec["by"] == "operator" and len(replies) == 1
                  and fires == [True], rec))
    replies.clear()
    cmds._drop(env(body="!drop T46", mid=827))
    rec = control("drop", "T46")
    cases.append(("human !drop → control/drop-T46.json + reply, NO fire",
                  rec is not None and rec["action"] == "drop"
                  and len(replies) == 1 and fires == [True], rec))
    replies.clear()
    cmds._retry(env(sender="artilect_ios", body="!retry T45", mid=828))
    cases.append(("bot !retry → silent drop, no control file change",
                  len(replies) == 0, None))
    replies.clear()
    cmds._retry(env(body="!retry ../../etc/passwd", mid=829))
    cases.append(("path-injection !retry → usage reply, no traversal",
                  len(replies) == 1 and control("retry", "passwd") is None,
                  None))
    board_after = (tmp / "campaign" / "BOARD.md").read_text()
    cases.append(("retry/drop made NO BOARD edits (single-writer intact)",
                  board_after == BOARD, None))

    # 13. gate-class executor hook (spec §2/§5) — ship_command on deploy-gate
    spawns: list = []
    cmds._popen = lambda argv, cwd, stdout: spawns.append((argv, cwd))
    route = cfg.routes["app"]
    route["ship_command"] = ["bash", "ship.sh", "--row", "{row}"]
    route["integrate_and_ship_command"] = ["bash", "ias.sh", "--row", "{row}"]

    replies.clear()
    cmds._approve(env(body="!approve T47", mid=830))
    cases.append(("deploy-gate approve fires ship_command w/ {row} substituted",
                  spawns == [(["bash", "ship.sh", "--row", "T47"], str(tmp))]
                  and len(replies) == 2, spawns))
    cases.append(("ship-fired ledger event recorded",
                  '"event": "ship-fired"'
                  in (tmp / "campaign" / "ledger.jsonl").read_text(), None))

    spawns.clear()
    replies.clear()
    cmds._approve(env(body="!approve T48", mid=831))
    cases.append(("fix-and-ship approve fires integrate_and_ship_command",
                  spawns == [(["bash", "ias.sh", "--row", "T48"], str(tmp))],
                  spawns))

    spawns.clear()
    replies.clear()
    cmds._approve(env(body="!approve T45", mid=832))
    cases.append(("push-gate approve fires NOTHING (no command for class)",
                  spawns == [] and len(replies) == 1, None))

    spawns.clear()
    route["ship_command"] = "bash ship.sh --row {row}"   # string, not argv list
    replies.clear()
    cmds._approve(env(body="!approve T47", mid=833))
    cases.append(("non-list ship_command refused (no shell interpolation path)",
                  spawns == [] and len(replies) == 1, None))

    spawns.clear()
    del route["ship_command"]
    replies.clear()
    cmds._approve(env(body="!approve T47", mid=834))
    cases.append(("deploy-gate w/o ship_command → file-only (today's behavior)",
                  spawns == [] and len(replies) == 1, None))

    spawns.clear()
    cmds._approve(env(sender="teammate", body="!approve T48", mid=835))
    cases.append(("unauthorized fix-and-ship approve → no file update, NO fire",
                  spawns == [], None))

    # 14. !status gate section (T26) — pure parse: codes + glosses + who approves
    board_md = tmp / "campaign" / "BOARD.md"
    sec = cmds._gate_section(board_md) or ""
    L = sec.splitlines()

    def has(needle):
        return any(needle in ln for ln in L)

    cases.append(("gate section names every GATED row with its class code",
                  all(f"• {r}  [" in sec for r in
                      ("T45", "T46", "T47", "T48", "T99", "T62")), None))
    cases.append(("codes are the PROTOCOL §6 vocabulary, not an opaque marker",
                  has("[push-gate]") and has("[product-question]")
                  and has("[deploy-gate]") and has("[fix-and-ship]"), None))
    cases.append(("deploy-gate gloss says it BUILDS + UPLOADS a TestFlight build",
                  "BUILDS + UPLOADS" in sec, None))
    cases.append(("fix-and-ship gloss says it MERGES *and* SHIPS",
                  "MERGES the branch AND SHIPS" in sec, None))
    cases.append(("queued row prints its exact !approve + default whitelist",
                  has("→ `!approve T45`  (from: operator)"), None))
    cases.append(("product-question names its per-class approvers (magnus too)",
                  has("→ `!approve T46`  (from: operator, teammate)"),
                  None))
    cases.append(("unstamped queue line still glossed from the row's Kind (fleet)",
                  has("• T99  [fleet-gate]"), None))
    cases.append(("GATED-but-unqueued row: no !approve line, warns it'd bounce",
                  not has("!approve T62`  (from:") and has("will bounce"), None))
    cases.append(("BLOCKED row listed with its deps and NO !approve",
                  has("• T60 ← needs T45,T47") and not has("!approve T60"), None))
    cases.append(("BLOCKED is labelled as waiting on deps, not on the operator",
                  has("⛔ BLOCKED (1)") and has("not on you"), None))
    cases.append(("MERGED rows stay out of the gate section",
                  not has("T61"), None))
    cases.append(("titles carried through, trimmed",
                  has("Cut a TestFlight build (0.15.0→0.16.0)"), None))
    cases.append(("counts still parse from the same scan (8 rows, 6 GATED)",
                  cmds._board_summary(board_md, "app")
                  == "app — 8 rows: 1 BLOCKED · 1 MERGED · 6 GATED", None))

    status = cmds._render_status("app", cfg.routes["app"])
    cases.append(("!status puts the gate section right under the counts line",
                  status.splitlines()[0].startswith("app — 8 rows")
                  and status.splitlines()[1].startswith("🚦 GATED (6)")
                  and "conductor:" in status, None))

    idle = tmp / "idle"
    (idle / "campaign").mkdir(parents=True)
    (idle / "campaign" / "BOARD.md").write_text(
        "# BOARD\n\n| ID | Item | Kind | Status |\n|--|--|--|--|\n"
        "| T1 | done thing | code | MERGED |\n")
    cases.append(("idle board (nothing gated/blocked) → NO gate section, no noise",
                  cmds._gate_section(idle / "campaign" / "BOARD.md") is None, None))
    cases.append(("missing BOARD.md → no gate section, no crash",
                  cmds._gate_section(tmp / "nope" / "BOARD.md") is None, None))

    ok_all = True
    for name, ok, _ in cases:
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}")
        ok_all = ok_all and ok
    print("PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(selftest() if (len(sys.argv) > 1 and sys.argv[1] == "selftest")
                     else selftest())
