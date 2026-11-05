"""supervisor — the daemon's supervision tick (SB-5, D7, PROTOCOL §11).

Dumb code, no LLM (PROTOCOL §10.4). Once a minute, per routed campaign, it reads
`conductor.lock` + `HEARTBEAT` and decides whether a conductor incarnation is
healthy, stuck, or orphaned.

**Two-tier liveness (design D).** A heartbeat has to tell "healthy but busy" apart
from "hung", and those need opposite thresholds: a `promote` step that goes quiet
for 10 min is hung, but a `wave` step legitimately hands off to sub-agents for far
longer. So we track two orthogonal signals, written by two different owners, each
to its own single-writer file:

  - **liveness** — `HEARTBEAT.lastBeat`, written by the *launcher*
    (`conductor-run.py`) every ~60s for as long as the `claude` child is alive.
    A live incarnation of any duration keeps this fresh.
  - **progress** — `HEARTBEAT.progressAt`, the launcher's fold of (a) the
    conductor's own `PROGRESS` file, rewritten per phase/row, and (b) the
    transcript file's mtime (the LLM emitting output). "Last time the incarnation
    did anything observable."

The supervisor kills only when a signal genuinely says so (see `check`): a stuck
LLM (fresh liveness, frozen progress), a wedged launcher (holds the lock, stopped
beating), or an orphan (incarnation alive but its launcher died and the lock is
free). It **monitors, never links**: own thread, never blocks dispatch, and it no
longer unlinks the lock — it kills, and the launcher releases its own flock on
exit (unlinking a lock a live launcher still holds would break mutual exclusion).
Recovery is kill + next-cron `resume` (all state is on disk). Alerts are de-duped.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

# --- thresholds (overridable via cfg.defaults) --------------------------------
LIVENESS_STALE_SEC = 180       # lock held but no launcher beat in 3m = wedged
PROGRESS_STALL_MIN = 30        # active phase, no observable progress this long = stuck
DEFAULT_CADENCE_MIN = 30       # cron cadence (ML-5); 2× = cron-broken threshold
DEFAULT_INTERVAL_SEC = 60      # supervision tick: 1/min (D7)

# phases in which "no progress" is a meaningful stuck signal. `idle`/`starting`
# are transient/terminal and never count as stuck; a missing phase is treated as
# active (fail toward noticing, not toward ignoring).
_INACTIVE_PHASES = {"idle", "starting"}
_HAS_PROC = Path("/proc").is_dir()   # Linux → we can verify a pid's cmdline


def lock_is_held(path) -> bool:
    """True iff a live process holds an flock on `path` (ML-4). A leftover lock
    *file* with no live flock (crashed conductor) reads as free — flock is
    released when the holder dies. Acquire-test-release is instantaneous; we
    never keep the lock (monitor, never link)."""
    import fcntl
    p = Path(path)
    if not p.exists():
        return False
    try:
        f = open(p, "r")
    except OSError:
        return False
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False   # we acquired it → nobody holds it
    except BlockingIOError:
        return True    # held by a live incarnation
    except OSError:
        return False
    finally:
        f.close()


def read_heartbeat(path) -> "dict | None":
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours
    except OSError:
        return False


def looks_like_conductor(pid) -> bool:
    """Guard against PID reuse before we SIGTERM. On Linux, require the pid's
    cmdline to look like a conductor/claude process; on non-Linux (mac, no
    `/proc`) we can't verify cheaply, so trust the pid."""
    if not _HAS_PROC:
        return True
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return False   # pid already gone
    except OSError:
        return True    # unreadable (perms) → trust rather than miss a real kill
    s = raw.replace(b"\0", b" ").decode("utf-8", "replace").lower()
    return any(k in s for k in ("claude", "node", "conductor-run"))


class Supervisor:
    def __init__(self, cfg, adapter, log=print, interval: int = DEFAULT_INTERVAL_SEC):
        self.cfg = cfg
        self.kb = adapter
        self.log = log
        self.interval = interval
        d = cfg.defaults or {}
        self.cadence_min = int(d.get("cron_cadence_min") or DEFAULT_CADENCE_MIN)
        self.liveness_stale_ms = int(d.get("liveness_stale_sec")
                                     or LIVENESS_STALE_SEC) * 1000
        self.progress_stall_ms = int(d.get("progress_stall_min")
                                     or PROGRESS_STALL_MIN) * 60 * 1000
        self._stop = threading.Event()
        self._alerted: dict[str, str] = {}   # channel -> last alert signature (dedup)
        self._campaigns = self._resolve_campaigns()

    def _resolve_campaigns(self) -> list[tuple[str, Path]]:
        out = []
        for ch, route in self.cfg.routes.items():
            repo, cd = route.get("repo"), route.get("campaignDir")
            if repo and cd:
                out.append((ch, (Path(repo).expanduser() / cd).resolve()))
        return out

    # ---- thread lifecycle ------------------------------------------------
    def start(self) -> "threading.Thread | None":
        if not self._campaigns:
            self.log("supervisor: no local campaigns to monitor — not starting")
            return None
        t = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        t.start()
        self.log(
            "supervisor: monitoring "
            f"{[c for c, _ in self._campaigns]} every {self.interval}s "
            f"(liveness>{self.liveness_stale_ms // 1000}s, "
            f"progress>{self.progress_stall_ms // 60000}m)"
        )
        return t

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for ch, cdir in self._campaigns:
                try:
                    self.check(ch, cdir)
                except Exception as e:  # a monitor error must never take the daemon down
                    self.log(f"supervisor error {ch}: {e}")
            self._stop.wait(self.interval)

    # ---- the tick (design D kill matrix) ---------------------------------
    # thin instance wrappers over the module fns so the selftest can inject
    # lock/pid state without monkeypatching module globals.
    def _is_held(self, path) -> bool:
        return lock_is_held(path)

    def _alive(self, pid) -> bool:
        return pid_alive(pid)

    def check(self, channel: str, cdir: Path) -> None:
        hb = read_heartbeat(cdir / "HEARTBEAT")
        held = self._is_held(cdir / "conductor.lock")
        now = int(time.time() * 1000)

        if held:
            if hb is None:
                return  # launcher just took the lock, first beat imminent — wait
            last_beat = int(hb.get("lastBeat", 0))
            if now - last_beat > self.liveness_stale_ms:
                # Launcher holds the lock but stopped beating: it's wedged. Kill
                # the incarnation AND its launcher so the flock is released.
                self._act(channel, "launcher-wedged", hb,
                          [hb.get("pid"), hb.get("launcherPid")],
                          f"launcher stopped beating "
                          f"({(now - last_beat) // 1000}s)")
                return
            phase = hb.get("phase")
            progress_at = int(hb.get("progressAt", last_beat))
            if phase not in _INACTIVE_PHASES and now - progress_at > self.progress_stall_ms:
                # Alive and beating, but no observable forward progress: stuck LLM.
                # Kill only the incarnation; the launcher will notice, write its
                # idle beat, and release the lock on its own.
                self._act(channel, "stuck-no-progress", hb, [hb.get("pid")],
                          f"no progress for {(now - progress_at) // 60000}m")
                return
            self._clear(channel)   # healthy: alive + progressing
        else:
            if hb is None:
                return  # never ran here — can't distinguish idle from broken
            pid, lpid = hb.get("pid"), hb.get("launcherPid")
            if self._alive(pid) and not self._alive(lpid):
                # Incarnation alive, its launcher gone, and nobody holds the lock:
                # an orphan running unsupervised. Reap it.
                self._act(channel, "orphan", hb, [pid],
                          "launcher died, incarnation left running unsupervised")
                return
            gap_min = (now - int(hb.get("lastBeat", 0))) / 60000.0
            if gap_min > 2 * self.cadence_min:
                self._alert_cron_broken(channel, gap_min)
            else:
                self._clear(channel)   # ran recently and exited clean; cron alive

    # ---- actions ---------------------------------------------------------
    def _act(self, channel: str, kind: str, hb: dict, pids, detail: str) -> None:
        inc = hb.get("incarnationId", "?")
        sig = f"{kind}:{inc}"
        if self._alerted.get(channel) == sig:
            return  # already handled this incarnation for this reason
        killed = [p for p in pids if self._kill(p)]
        phase, row = hb.get("phase", "?"), hb.get("currentRow")
        state = f"killed pid {killed[0]}" if killed else "pid already gone"
        body = (
            f"⚠️ conductor incarnation {inc} — {kind} at phase={phase} "
            f"row={row} ({detail}) — {state}, next cron `resume` will recover"
        )
        marker = (f"⟦sb:alert host={self.cfg.host} kind={kind} "
                  f"row={row}⟧")
        self._post(channel, body, marker)
        self._alerted[channel] = sig
        self.log(f"supervisor: {kind} inc {inc} on {channel} "
                 f"(pids={pids}, {detail}) — {state}")

    def _alert_cron_broken(self, channel: str, gap_min: float) -> None:
        sig = "cron-broken"
        if self._alerted.get(channel) == sig:
            return
        body = (
            f"⚠️ no conductor incarnation on #{channel} for {gap_min:.0f}m "
            f"(>2× the {self.cadence_min}m cadence) — cron/timer may be broken"
        )
        marker = f"⟦sb:alert host={self.cfg.host} kind=cron-broken⟧"
        self._post(channel, body, marker)
        self._alerted[channel] = sig

    def _clear(self, channel: str) -> None:
        self._alerted.pop(channel, None)

    # ---- helpers ---------------------------------------------------------
    def _kill(self, pid) -> bool:
        """SIGTERM a pid iff it's alive AND still looks like a conductor (PID-reuse
        guard). Returns True only when we actually signalled a live process."""
        if not pid_alive(pid):
            return False
        if not looks_like_conductor(pid):
            self.log(f"supervisor: pid {pid} alive but not a conductor "
                     f"(reuse?) — not killing")
            return False
        try:
            os.kill(int(pid), signal.SIGTERM)
            return True
        except (OSError, ValueError) as e:
            self.log(f"supervisor: kill {pid} failed: {e}")
            return False

    def _post(self, channel: str, body: str, marker: str) -> None:
        chobj = {"name": self.cfg.team, "members_type": "team", "topic_name": channel}
        try:
            self.kb.post(chobj, body, marker)
        except Exception as e:
            self.log(f"supervisor post failed ({channel}): {e}")


# ==========================================================================
# selftest — drive the kill matrix with crafted HEARTBEAT/lock/pid states.
# ==========================================================================
def selftest() -> bool:
    import tempfile

    class FakeCfg:
        host = "test-host"
        team = "acme_agents"
        defaults = {"cron_cadence_min": 30}
        routes = {}

    class FakeAdapter:
        def __init__(self):
            self.posts = []
        def post(self, chobj, body, marker):
            self.posts.append((body, marker))

    now = lambda: int(time.time() * 1000)
    MIN = 60 * 1000
    results = []

    def scenario(name, hb, held, alive_pids, expect_kill, expect_post_kind):
        adapter = FakeAdapter()
        sup = Supervisor(FakeCfg(), adapter, log=lambda *a: None)
        killed = []
        # inject lock/pid state + capture kills on the instance (no module patching)
        sup._is_held = lambda p: held
        sup._alive = lambda pid: pid in alive_pids
        sup._kill = lambda pid: (killed.append(pid) or True) if pid in alive_pids else False
        with tempfile.TemporaryDirectory() as d:
            cdir = Path(d)
            if hb is not None:
                (cdir / "HEARTBEAT").write_text(json.dumps(hb))
            (cdir / "conductor.lock").write_text("")
            sup.check("app", cdir)
        post_kind = None
        if adapter.posts:
            mk = adapter.posts[0][1]
            for tok in mk.split():
                if tok.startswith("kind="):
                    post_kind = tok.split("=", 1)[1].rstrip("⟧")
        ok = (bool(killed) == expect_kill) and (post_kind == expect_post_kind)
        results.append((name, ok, f"killed={killed} post_kind={post_kind}"))
        return ok

    n = now()
    # 1. healthy running: fresh beat, fresh progress, active phase → nothing
    scenario("healthy-running",
             {"incarnationId": "aaa", "pid": 100, "launcherPid": 99,
              "lastBeat": n, "progressAt": n, "phase": "wave", "currentRow": "T1"},
             held=True, alive_pids={100, 99},
             expect_kill=False, expect_post_kind=None)
    # 2. healthy long wave: beat fresh, progress 20m old (< 30m), active → nothing
    scenario("healthy-long-wave",
             {"incarnationId": "bbb", "pid": 100, "launcherPid": 99,
              "lastBeat": n, "progressAt": n - 20 * MIN, "phase": "wave", "currentRow": "T1"},
             held=True, alive_pids={100, 99},
             expect_kill=False, expect_post_kind=None)
    # 3. launcher wedged: holds lock, no beat 4m → kill, alert
    scenario("launcher-wedged",
             {"incarnationId": "ccc", "pid": 100, "launcherPid": 99,
              "lastBeat": n - 4 * MIN, "progressAt": n - 4 * MIN, "phase": "wave"},
             held=True, alive_pids={100, 99},
             expect_kill=True, expect_post_kind="launcher-wedged")
    # 4. stuck LLM: beat fresh, progress 31m old, active phase → kill, alert
    scenario("stuck-no-progress",
             {"incarnationId": "ddd", "pid": 100, "launcherPid": 99,
              "lastBeat": n, "progressAt": n - 31 * MIN, "phase": "wave", "currentRow": "T1"},
             held=True, alive_pids={100, 99},
             expect_kill=True, expect_post_kind="stuck-no-progress")
    # 5. idle phase never counts as stuck even with old progress
    scenario("idle-not-stuck",
             {"incarnationId": "eee", "pid": 100, "launcherPid": 99,
              "lastBeat": n, "progressAt": n - 60 * MIN, "phase": "idle"},
             held=True, alive_pids={100, 99},
             expect_kill=False, expect_post_kind=None)
    # 6. orphan: lock free, incarnation alive, launcher dead → kill, alert
    scenario("orphan",
             {"incarnationId": "fff", "pid": 100, "launcherPid": 99,
              "lastBeat": n, "progressAt": n, "phase": "wave"},
             held=False, alive_pids={100},   # launcher 99 dead
             expect_kill=True, expect_post_kind="orphan")
    # 7. clean exit: lock free, pids dead, recent beat → nothing
    scenario("clean-exit",
             {"incarnationId": "ggg", "pid": 99, "launcherPid": 99,
              "lastBeat": n - 2 * MIN, "progressAt": n - 2 * MIN, "phase": "idle"},
             held=False, alive_pids=set(),
             expect_kill=False, expect_post_kind=None)
    # 8. cron-broken: lock free, pids dead, beat 70m old (>2×30m) → alert, no kill
    scenario("cron-broken",
             {"incarnationId": "hhh", "pid": 99, "launcherPid": 99,
              "lastBeat": n - 70 * MIN, "progressAt": n - 70 * MIN, "phase": "idle"},
             held=False, alive_pids=set(),
             expect_kill=False, expect_post_kind="cron-broken")
    # 9. startup window: lock held, no HEARTBEAT yet → nothing
    scenario("startup-window", None, held=True, alive_pids=set(),
             expect_kill=False, expect_post_kind=None)

    ok_all = all(ok for _, ok, _ in results)
    for name, ok, detail in results:
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {detail}")
    print("PASS" if ok_all else "FAIL")
    return ok_all


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
