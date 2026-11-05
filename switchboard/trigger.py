"""trigger — debounced event-driven conductor trigger (phone-loop spec §1).

Dumb code, no LLM in this path (PROTOCOL §10.4). The dispatcher calls
notify_filing() whenever a frontdesk run grew the campaign INBOX (detected by
byte-signature delta — the frontdesk is the INBOX's only writer, so no LLM
output parsing is needed). We debounce so a burst of filings becomes ONE
conductor run, then fire `conductor-run.py --auto-wave` through the one
launcher (lock, env, allowlist, HEARTBEAT — identical to the scheduled tick),
and keep watching until the INBOX actually drains: a filing that lands while a
conductor is mid-wave would be missed by that incarnation (it promotes only at
start), so we re-fire after the lock frees.

The 30-min scheduled tick stays as a mop-up heartbeat. This trigger exists so
a phone-filed idea starts working in ~2 minutes, not ~30 (the single biggest
latency tax in the idea→ship pipeline). Cost discipline is inherited:
conductor-run.py --auto-wave enforces the auto-wave daily run/cost caps and the
kill switch itself, so the trigger cannot spend past what the tick could.

Selftest: `python3 trigger.py selftest` (no Keybase, no subprocesses).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from supervisor import lock_is_held

DEBOUNCE_SEC = 120     # a burst of filings coalesces into one run
MAX_WAIT_SEC = 300     # …but the first filing never waits longer than this
POLL_SEC = 60          # drain-watcher cadence
MAX_POLLS = 30         # then hand off to the scheduled mop-up tick


def campaign_dir(route: dict) -> "Path | None":
    """Absolute campaign dir for a route (same rule as dispatcher.campaign_dir;
    duplicated here to keep the import graph acyclic)."""
    repo, cd = route.get("repo"), route.get("campaignDir")
    if repo and cd:
        return (Path(repo).expanduser() / cd).resolve()
    return None


def inbox_sig(cdir: "Path | None") -> tuple:
    """Byte signature of the campaign INBOX — the deterministic 'a task was
    filed' primitive: snapshot before a frontdesk spawn, compare after."""
    if cdir is None:
        return ("no-campaign",)
    p = cdir / "INBOX.md"
    try:
        data = p.read_bytes()
    except (FileNotFoundError, OSError):
        return ("absent",)
    return (len(data), hashlib.sha256(data).hexdigest())


def inbox_pending(cdir: "Path | None") -> int:
    """Count of undrained INBOX entries (jsonl lines carrying an inbId)."""
    if cdir is None:
        return 0
    try:
        text = (cdir / "INBOX.md").read_text()
    except (FileNotFoundError, OSError):
        return 0
    return sum(1 for ln in text.splitlines()
               if ln.strip().startswith("{") and '"inbId"' in ln)


class ConductorTrigger:
    """One instance per daemon; per-channel debounce + drain-watcher threads."""

    def __init__(self, cfg, log=print, debounce_sec: float = DEBOUNCE_SEC,
                 max_wait_sec: float = MAX_WAIT_SEC, poll_sec: float = POLL_SEC,
                 max_polls: int = MAX_POLLS, spawn=None, lock_probe=None):
        self.cfg = cfg
        self.log = log
        self.debounce_sec = debounce_sec
        self.max_wait_sec = max_wait_sec
        self.poll_sec = poll_sec
        self.max_polls = max_polls
        # injection seams for the selftest; production uses the real ones
        self._spawn = spawn or self._spawn_autowave
        self._lock_probe = lock_probe or (
            lambda cdir: lock_is_held(cdir / "conductor.lock"))
        self._mu = threading.Lock()
        self._state: dict = {}       # channel -> {"timer": Timer|None, "first_at": float|None}
        self._watchers: set = set()  # channels with a live drain-watcher

    # ---- entry points ------------------------------------------------------
    def notify_filing(self, channel: str, route: dict) -> None:
        """A frontdesk grew this channel's INBOX. Debounce, then fire."""
        if campaign_dir(route) is None:
            return
        fire_now = False
        with self._mu:
            st = self._state.setdefault(channel,
                                        {"timer": None, "first_at": None})
            now = time.monotonic()
            if st["first_at"] is None:
                st["first_at"] = now
            if st["timer"] is not None:
                st["timer"].cancel()
                st["timer"] = None
            if now - st["first_at"] >= self.max_wait_sec:
                fire_now = True   # anti-starvation: a steady stream still runs
            else:
                t = threading.Timer(self.debounce_sec, self._fire,
                                    args=(channel, route))
                t.daemon = True
                st["timer"] = t
                t.start()
        if fire_now:
            self.log(f"trigger: {channel} burst exceeded {self.max_wait_sec:.0f}s"
                     " — firing now")
            self._fire(channel, route)
        else:
            self.log(f"trigger: filing on {channel} — conductor in "
                     f"{self.debounce_sec:.0f}s unless more arrive")

    def catch_up(self, routes: dict) -> None:
        """Daemon-start sweep: anything filed while the daemon was down (or
        dropped by a crashed trigger) still gets a conductor without waiting
        for the mop-up tick."""
        for channel, route in routes.items():
            cdir = campaign_dir(route)
            n = inbox_pending(cdir)
            if n > 0:
                self.log(f"trigger: catch-up — {n} pending on {channel}")
                self.notify_filing(channel, route)

    # ---- internals -----------------------------------------------------------
    def _fire(self, channel: str, route: dict) -> None:
        with self._mu:
            st = self._state.get(channel)
            if st is not None:
                st["timer"] = None
                st["first_at"] = None
            if channel in self._watchers:
                return            # a watcher is already ensuring the drain
            self._watchers.add(channel)
        t = threading.Thread(target=self._ensure_drained, args=(channel, route),
                             name=f"trigger-{channel}", daemon=True)
        t.start()

    def _ensure_drained(self, channel: str, route: dict) -> None:
        """Fire when the lock is free; re-check until the INBOX drains. Handles
        filings that arrive mid-wave (wait for lock, re-fire) and spawn hiccups
        (retry next poll). conductor-run.py's own flock makes double-fires
        no-ops, so erring on the side of firing is safe."""
        cdir = campaign_dir(route)
        try:
            for _ in range(self.max_polls):
                n = inbox_pending(cdir)
                if n == 0:
                    self.log(f"trigger: {channel} INBOX drained")
                    return
                if not self._lock_probe(cdir):
                    self.log(f"trigger: firing conductor for {channel} "
                             f"({n} pending)")
                    self._spawn(channel)
                time.sleep(self.poll_sec)
            self.log(f"trigger: {channel} not drained after "
                     f"{self.max_polls} polls — leaving to the scheduled tick")
        finally:
            with self._mu:
                self._watchers.discard(channel)

    def _spawn_autowave(self, channel: str) -> None:
        """Fire conductor-run.py --auto-wave detached — the one launcher, same
        invocation as the scheduled tick (lock + env + allowlist + HEARTBEAT +
        auto-wave caps). Mirrors commands._fire_conductor, plus --auto-wave:
        an event-triggered run must promote AND wave, or a phone-filed idea
        still waits 30 min for code to start."""
        launcher = str(Path(__file__).resolve().parent / "conductor-run.py")
        logdir = Path(self.cfg.state_file).expanduser().parent / "conductor-logs"
        logdir.mkdir(parents=True, exist_ok=True)
        logpath = logdir / f"trigger-{channel}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        try:
            lf = open(logpath, "w")
            # sys.executable, NOT bare "python3": under launchd, PATH resolves
            # python3 to Homebrew 3.12 (no pyyaml) and the launcher dies on
            # import. The daemon runs on the pinned interpreter — propagate it.
            subprocess.Popen(
                [sys.executable, launcher, "--channel", channel, "--auto-wave"],
                stdout=lf, stderr=subprocess.STDOUT,
                env=os.environ.copy(), start_new_session=True,
            )
            self.log(f"trigger: conductor-run.py --auto-wave {channel} "
                     f"({logpath})")
        except OSError as e:
            self.log(f"trigger: spawn failed for {channel}: {e}")


# ── selftest (no Keybase, no subprocesses, sub-second) ──────────────────────
def selftest() -> int:
    import tempfile
    from types import SimpleNamespace

    tmp = Path(tempfile.mkdtemp(prefix="sb-trigger-"))

    def mkcamp(name: str, pending: int) -> dict:
        d = tmp / name / "campaign"
        d.mkdir(parents=True)
        lines = "".join(f'{{"inbId": "INB-{i}", "body": "x"}}\n'
                        for i in range(pending))
        (d / "INBOX.md").write_text("# INBOX\n" + lines)
        return {"repo": str(tmp / name), "campaignDir": "campaign"}

    cfg = SimpleNamespace(state_file=str(tmp / "state.json"))
    results = []

    def check(name, ok):
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}")
        results.append(ok)

    # 1. signature primitives
    r = mkcamp("sig", 1)
    cd = campaign_dir(r)
    s1 = inbox_sig(cd)
    (cd / "INBOX.md").open("a").write('{"inbId": "INB-9", "body": "y"}\n')
    check("inbox_sig detects a filing", inbox_sig(cd) != s1)
    check("inbox_pending counts jsonl lines", inbox_pending(cd) == 2)
    check("no campaign → sig sentinel, 0 pending",
          inbox_sig(None) == ("no-campaign",) and inbox_pending(None) == 0)

    def trig(route, *, locked=None, drain_after=1, **kw):
        """Trigger with fake spawn that drains the INBOX after N spawns."""
        spawns = []
        cdir = campaign_dir(route)
        lock_state = {"locked": False} if locked is None else locked

        def spawn(channel):
            spawns.append(channel)
            if len(spawns) >= drain_after:
                (cdir / "INBOX.md").write_text("# INBOX\n")

        t = ConductorTrigger(cfg, log=lambda *a: None, spawn=spawn,
                             lock_probe=lambda c: lock_state["locked"],
                             debounce_sec=kw.get("debounce_sec", 0.1),
                             max_wait_sec=kw.get("max_wait_sec", 5.0),
                             poll_sec=0.05, max_polls=kw.get("max_polls", 8))
        return t, spawns, lock_state

    # 2. single filing → exactly one spawn after the debounce
    r = mkcamp("single", 1)
    t, spawns, _ = trig(r)
    t.notify_filing("ch-single", r)
    time.sleep(0.5)
    check("single filing → one spawn", spawns == ["ch-single"])

    # 3. burst of 3 filings → still one spawn
    r = mkcamp("burst", 3)
    t, spawns, _ = trig(r)
    for _ in range(3):
        t.notify_filing("ch-burst", r)
        time.sleep(0.02)
    time.sleep(0.5)
    check("burst coalesces to one spawn", spawns == ["ch-burst"])

    # 4. conductor lock held → waits, fires after release
    r = mkcamp("locked", 1)
    t, spawns, lock_state = trig(r, locked={"locked": True})
    t.notify_filing("ch-locked", r)
    time.sleep(0.3)
    held_no_spawn = spawns == []
    lock_state["locked"] = False
    time.sleep(0.3)
    check("lock held → no spawn until it frees",
          held_no_spawn and spawns == ["ch-locked"])

    # 5. INBOX already drained when the debounce fires → no spawn
    r = mkcamp("drained", 1)
    t, spawns, _ = trig(r)
    t.notify_filing("ch-drained", r)
    campaign_dir(r).joinpath("INBOX.md").write_text("# INBOX\n")
    time.sleep(0.4)
    check("pre-drained INBOX → zero spawns", spawns == [])

    # 6. anti-starvation: continuous filings past max_wait fire anyway
    r = mkcamp("stream", 2)
    t, spawns, _ = trig(r, debounce_sec=5.0, max_wait_sec=0.15)
    t.notify_filing("ch-stream", r)
    time.sleep(0.2)
    t.notify_filing("ch-stream", r)   # debounce would wait 5s; max_wait fires
    time.sleep(0.4)
    check("max_wait fires a steady stream", spawns == ["ch-stream"])

    # 7. undrainable INBOX → gives up after max_polls (mop-up tick's job)
    r = mkcamp("stuck", 1)
    t, spawns, _ = trig(r, drain_after=99, max_polls=3)
    t.notify_filing("ch-stuck", r)
    time.sleep(0.6)
    watcher_exited = "ch-stuck" not in t._watchers
    check("undrained → bounded retries then hand-off",
          len(spawns) == 3 and watcher_exited)

    # 8. catch_up fires for pre-existing backlog, skips empty/routing-only
    r_full = mkcamp("catchup", 2)
    r_empty = mkcamp("catchup-empty", 0)
    t, spawns, _ = trig(r_full)
    t.notify_filing = lambda ch, rt: spawns.append("notified:" + ch)  # spy
    t.catch_up({"a": r_full, "b": r_empty, "c": {"mode": "route"}})
    check("catch_up notifies only backlogged campaigns",
          spawns == ["notified:a"])

    ok = all(results)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
