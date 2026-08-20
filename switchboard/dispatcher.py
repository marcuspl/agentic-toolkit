"""dispatcher — spawn the frontdesk headless dispatcher (SB-3) + audit (HD-1).

Dumb code, no LLM in *this* path (PROTOCOL §10.4). The daemon hands each
dispatchable envelope here; we spawn `claude -p` running the /frontdesk skill in
the routed repo, cap global concurrency, serialize per channel (FIFO), capture a
transcript + exit code per msgId, and append an audit line to the campaign's
`dispatch-log.jsonl`. On a crash/timeout we post a ⚠️ threaded alert (PROTOCOL §8).

DAEMON⇄FRONTDESK spawn contract (must match the frontdesk lane):
  claude -p "<PROMPT>"  cwd = route.repo, with env:
    SWITCHBOARD_ENVELOPE   envelope JSON (PROTOCOL §1 shape, Envelope.to_protocol_dict)
    SWITCHBOARD_ROUTE      the route dict JSON
    SWITCHBOARD_CAMPAIGN_DIR  abs campaign dir ("" for routing-only channels)
    SWITCHBOARD_BOT_HOME   the bot's KEYBASE_HOME (keybase -H)
    SWITCHBOARD_HOST       host label
    SWITCHBOARD_APPROVERS  approvers map JSON
    SWITCHBOARD_HUMANS     humans list JSON

Concurrency model (SB-3):
  - one worker thread + queue per channel  → strict FIFO within a channel
  - a global Semaphore(defaults.concurrency, default 2) around each spawn
    → different channels run concurrently, capped fleet-wide per host
  - the daemon listen loop never blocks: submit() only enqueues.
"""
from __future__ import annotations

import fcntl
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

FRONTDESK_PROMPT = (
    "Run the /frontdesk skill on the switchboard message in $SWITCHBOARD_ENVELOPE."
)
DEFAULT_CONCURRENCY = 2
DEFAULT_TIMEOUT_SEC = 900  # 15 min per frontdesk dispatcher

# Headless `claude -p` denies any tool not pre-approved (no human to prompt), so
# the dispatcher must pass an allowlist. The frontdesk is read-mostly by design
# (FD-3): it explores, runs `fd.py` (INBOX/approvals under flock), and posts via
# `keybase` — it never needs to mutate the routed repo. This scoped list keeps the
# injection defense intact: even a prompt-injected frontdesk can't `rm`, `curl`,
# etc. Override with defaults.dispatcher_allowed_tools (comma-separated) in
# routes.yaml. (No commas/spaces inside a pattern → comma-split is safe.)
READ_MOSTLY_TOOLS = (
    "Read,Grep,Glob,"
    "Bash(python3:*),Bash(keybase:*),"
    "Bash(git:*),Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),"
    "Bash(wc:*),Bash(rg:*),Bash(grep:*),Bash(find:*),Bash(sed:*),Bash(jq:*)"
)


def campaign_dir(route: dict) -> "Path | None":
    """Absolute campaign dir for a route (campaignDir is relative to repo), or
    None for routing-only channels (mode=route has no local repo)."""
    repo, cd = route.get("repo"), route.get("campaignDir")
    if repo and cd:
        return (Path(repo).expanduser() / cd).resolve()
    return None


class Dispatcher:
    def __init__(self, cfg, adapter, log=print, trigger=None):
        self.cfg = cfg
        self.kb = adapter
        self.log = log
        self.trigger = trigger   # ConductorTrigger (spec §1); None = timer-only
        cap = int((cfg.defaults or {}).get("concurrency") or DEFAULT_CONCURRENCY)
        self.cap = max(1, cap)
        self.sem = threading.Semaphore(self.cap)
        self.timeout = int(
            (cfg.defaults or {}).get("dispatcher_timeout_sec") or DEFAULT_TIMEOUT_SEC
        )
        self.allowed_tools = (
            (cfg.defaults or {}).get("dispatcher_allowed_tools") or READ_MOSTLY_TOOLS
        )
        self._channels: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    # ---- submit (called from the listen loop; must not block) -------------
    def submit(self, env, route: dict) -> None:
        self._channel_queue(env.channel).put((env, route))

    def _channel_queue(self, channel: str) -> queue.Queue:
        with self._lock:
            q = self._channels.get(channel)
            if q is None:
                q = queue.Queue()
                self._channels[channel] = q
                t = threading.Thread(
                    target=self._worker, args=(channel, q),
                    name=f"fd-{channel}", daemon=True,
                )
                t.start()
            return q

    # ---- per-channel FIFO worker -----------------------------------------
    def _worker(self, channel: str, q: queue.Queue) -> None:
        while True:
            env, route = q.get()
            try:
                self._run_one(env, route)
            except Exception as e:  # never let one job kill the channel worker
                self.log(f"dispatcher ERROR {channel}#{env.msg_id}: {e}")
            finally:
                q.task_done()

    def _run_one(self, env, route: dict) -> None:
        adir = self._audit_dir(route, env.channel)
        transcript = adir / "transcripts" / f"frontdesk-{env.msg_id}.log"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        environ = self._child_env(env, route)
        cwd = self._cwd(route)
        # Filing signal for the event-driven conductor trigger (spec §1): the
        # frontdesk is the INBOX's only writer, so a byte-signature delta across
        # its run IS "a task was filed" — no LLM output parsing.
        pre_sig = None
        if self.trigger is not None:
            from trigger import inbox_sig
            pre_sig = inbox_sig(campaign_dir(route))
        outcome, exit_code = "dispatch", None
        self.sem.acquire()  # global concurrency cap (SB-3)
        t0 = time.time()
        try:
            with open(transcript, "w") as tf:
                tf.write(
                    f"# frontdesk transcript msgId={env.msg_id} "
                    f"channel={env.channel} sender={env.sender} "
                    f"cwd={cwd} at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                tf.flush()
                try:
                    proc = subprocess.run(
                        # prompt BEFORE --allowedTools: the flag is variadic
                        # (<tools...>) and would otherwise swallow the prompt.
                        ["claude", "-p", FRONTDESK_PROMPT,
                         "--allowedTools", self.allowed_tools],
                        cwd=cwd, env=environ,
                        stdout=tf, stderr=subprocess.STDOUT,
                        timeout=self.timeout,
                    )
                    exit_code = proc.returncode
                    if exit_code != 0:
                        outcome = "crash"
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
                except FileNotFoundError as e:  # `claude` not on PATH
                    outcome = "crash"
                    tf.write(f"\n[dispatcher] spawn failed: {e}\n")
        finally:
            self.sem.release()
        dt = time.time() - t0
        self.log(
            f"frontdesk {env.channel}#{env.msg_id} → {outcome} "
            f"exit={exit_code} {dt:.0f}s → {transcript}"
        )
        self._audit(adir, env, outcome, exit_code, transcript)
        if outcome in ("crash", "timeout"):
            self._alert(env, outcome, exit_code, transcript)
        if pre_sig is not None:
            from trigger import inbox_sig
            cd = campaign_dir(route)
            if inbox_sig(cd) != pre_sig:
                self._ledger_filed(cd, env)
                self.trigger.notify_filing(env.channel, route)

    @staticmethod
    def _ledger_filed(cd: Path, env) -> None:
        """Ledger the filing (spec §6) under the new INBOX line's inbId, so the
        recap's row timeline starts at capture time."""
        import ledger
        row = "INB-?"
        try:
            for ln in reversed(cd.joinpath("INBOX.md").read_text().splitlines()):
                ln = ln.strip()
                if ln.startswith("{") and '"inbId"' in ln:
                    row = json.loads(ln).get("inbId", row)
                    break
        except (OSError, json.JSONDecodeError):
            pass
        ledger.append(cd, row, "filed", "frontdesk",
                      {"msgId": env.msg_id, "sender": env.sender})

    # ---- spawn wiring ----------------------------------------------------
    def _cwd(self, route: dict) -> str:
        repo = route.get("repo")
        return str(Path(repo).expanduser()) if repo else os.path.expanduser("~")

    def _child_env(self, env, route: dict) -> dict:
        e = os.environ.copy()
        cd = campaign_dir(route)
        e.update({
            "SWITCHBOARD_ENVELOPE": json.dumps(env.to_protocol_dict()),
            "SWITCHBOARD_ROUTE": json.dumps(route),
            "SWITCHBOARD_CAMPAIGN_DIR": str(cd) if cd else "",
            "SWITCHBOARD_BOT_HOME": self.cfg.bot_home,
            "SWITCHBOARD_HOST": self.cfg.host,
            "SWITCHBOARD_APPROVERS": json.dumps(self.cfg.approvers),
            "SWITCHBOARD_HUMANS": json.dumps(self.cfg.humans),
        })
        return e

    # ---- audit + failure surfacing (HD-1) --------------------------------
    def _audit_dir(self, route: dict, channel: str) -> Path:
        """Where dispatch-log.jsonl + transcripts live. The campaign dir when
        one exists (PROTOCOL §11); for routing-only channels a per-host fallback
        under the state dir so HD-1 auditing still holds fleet-wide."""
        cd = campaign_dir(route)
        if cd:
            return cd
        return Path(self.cfg.state_file).expanduser().parent / "campaigns" / channel

    def _audit(self, adir: Path, env, outcome: str, exit_code, transcript: Path) -> None:
        adir.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(time.time() * 1000),
            "convId": env.conv_id,
            "msgId": env.msg_id,
            "sender": env.sender,
            "outcome": outcome,
            "transcript": str(transcript),
            "exit": exit_code,
        }
        line = json.dumps(rec) + "\n"
        path = adir / "dispatch-log.jsonl"
        try:
            with open(path, "a") as f:  # append-only under flock (PROTOCOL §11)
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(line)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError as e:
            self.log(f"dispatch-log append failed ({path}): {e}")

    def _alert(self, env, outcome: str, exit_code, transcript: Path) -> None:
        """⚠️ reaction + threaded alert (PROTOCOL §8) — silence is the failure
        mode to kill (HD-1)."""
        detail = "timed out" if outcome == "timeout" else f"crashed (exit {exit_code})"
        body = (
            f"⚠️ frontdesk dispatcher for msg {env.msg_id} {detail} — "
            f"see transcript `{transcript.name}`"
        )
        marker = (
            f"⟦sb:alert host={self.cfg.host} "
            f"kind=dispatcher-{outcome} msg={env.msg_id}⟧"
        )
        try:
            self.kb.react(env, ":warning:")
        except Exception as e:
            self.log(f"alert react failed: {e}")
        try:
            self.kb.post(
                env.channel_obj, body, marker,
                reply_to=(env.thread_root or env.msg_id),
            )
        except Exception as e:
            self.log(f"alert post failed: {e}")
