#!/usr/bin/env python3
"""switchboardd — the switchboard listener daemon (SB-1 + SB-2).

Dumb code, never an LLM (PROTOCOL §10.4). Per host it:
  - subscribes to `keybase chat api-listen` for its routed channels,
  - dedups via a persisted last-msgId per conversation,
  - applies the dispatch decision (PROTOCOL §2/§5/§7): skip non-text / seen /
    self / marker'd / non-dispatchable,
  - posts an instant 👀 reaction on every dispatchable message (SB-2),
  - then hands off to a frontdesk dispatcher (SB-3) — DISABLED by default here.

This build ships in a DRY-RUN posture: listen + ack + dedup, dispatch off. Turn
dispatch on only after the frontdesk skill (FD-*) exists and the bot has JOINed
its channels. See PROTOCOL §7 — do not go live before SB-0a (done) *and* a real
dispatcher.

Usage:
  switchboardd.py run [--config PATH] [--dispatch on|off] [--react on|off]
                      [--channels a,b] [--verbose]
  switchboardd.py selftest          # exercise the dispatch decision, no Keybase
  switchboardd.py channels [--config PATH]   # print resolved listen filters
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keybase_adapter import Envelope, KeybaseAdapter  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from supervisor import Supervisor  # noqa: E402
from commands import DaemonCommands  # noqa: E402
from trigger import ConductorTrigger  # noqa: E402


class ListenerDied(RuntimeError):
    """The keybase listen stream ended. Fatal: the daemon can no longer hear."""


DISPATCH_COMMANDS = {"!task", "!ask"}   # spawn a frontdesk (SB-3/FD-*)
# !approve is a DAEMON command, not a dispatch: gate release is a deterministic
# authz + atomic file-write that must never run under an LLM. Routing it to the
# `claude -p` frontdesk deadlocked deploy-gates — headless auto-mode's safety
# classifier refuses to write a deploy approval, so the one action required to
# be deterministic was delegated to an agent structurally forbidden from doing
# it. Handled in-daemon (commands._approve), honoring invariant §10.4 / §6:
# "no LLM in the gate path; only a whitelisted !approve flips a gate."
DAEMON_COMMANDS = {"!status", "!ping", "!pause", "!resume", "!nudge", "!approve",
                   "!recap", "!retry", "!drop"}  # in-daemon (SB-4/6 + spec §3/§6)

EYES = ":eyes:"          # 👀 dispatchable ack (SB-2)
PAUSE = ":pause_button:"  # ⏸️ paused-park ack (PROTOCOL §2 step 5)
INBOX = ":inbox_tray:"    # 📥 captured-to-sink ack (mode=capture)

# Dispatch-decision outcomes (PROTOCOL §2).
DISPATCH = "dispatch"
DAEMON_CMD = "daemon-cmd"
PAUSED_PARK = "paused-park"
ATTACH_DISPATCH = "attach:dispatch"   # spec §4: stage payload + dispatch caption
ATTACH_STAGE = "attach:stage"         # spec §4: stage payload only (no caption)
CAPTURE = "capture"                   # mode=capture: append to sink, spawn nothing
CAPTURE_ATTACH = "capture:attach"     # mode=capture: file the payload alongside
DROP_NONTEXT = "drop:nontext"
DROP_SEEN = "drop:seen"
DROP_SELF = "drop:self"
DROP_MARKER = "drop:marker"
DROP_NOT_DISPATCHABLE = "drop:not-dispatchable"
DROP_UNROUTED = "drop:unrouted"


@dataclass
class Config:
    host: str
    team: str
    bot: str
    bot_home: str
    humans: list[str]
    approvers: dict
    state_file: str
    routes: dict          # channel -> route dict
    defaults: dict

    @classmethod
    def load(cls, path: str) -> "Config":
        raw = yaml.safe_load(Path(path).expanduser().read_text())
        routes = {r["channel"]: r for r in raw.get("routes", [])}
        # Fail at load, not at 2am on the first message: a capture route with no
        # sink would swallow the thought it exists to preserve.
        for ch, r in routes.items():
            mode = r.get("mode", "dispatch")
            if mode == "capture" and not r.get("sink"):
                raise ValueError(f"route #{ch}: mode=capture requires `sink:`")
            if mode == "dispatch" and not (r.get("repo") and r.get("campaignDir")):
                raise ValueError(f"route #{ch}: mode=dispatch requires `repo` + `campaignDir`")
        return cls(
            host=raw["host"], team=raw["team"], bot=raw["bot"],
            bot_home=os.path.expanduser(raw["bot_home"]),
            humans=raw.get("humans", []),
            approvers=raw.get("approvers", {}),
            state_file=os.path.expanduser(raw["state_file"]),
            routes=routes, defaults=raw.get("defaults", {}),
        )

    def filters(self) -> list[dict]:
        return [{"name": self.team, "members_type": "team", "topic_name": ch}
                for ch in self.routes]


def first_token(body: str) -> str:
    body = body.strip()
    return body.split()[0].lower() if body else ""


# spec §4: a caption-less screenshot followed by a text message is ONE report
# split across two messages — the natural phone flow (photo first, description
# right after). A staged-but-undispatched payload stays linkable this long via
# recency; a direct thread-reply to the attachment message links at any age
# below the prune horizon.
ATTACH_LINK_WINDOW_MS = 10 * 60 * 1000
ATTACH_LINK_PRUNE_MS = 24 * 60 * 60 * 1000


def link_pending_attachments(pending: list[dict], env) -> list[str]:
    """Consume staged, still-unlinked attachment records that this message is
    the follow-up for: same sender + channel, and either recent (window) or the
    message is a thread-reply to the attachment itself. Mutates `pending`
    (matched + expired records are removed); returns matched staged paths in
    arrival order. Pure in-memory — only the daemon's listen thread touches it."""
    matched: list[str] = []
    keep: list[dict] = []
    for rec in pending:
        age = env.sent_at - rec["sentAt"]
        same = rec["channel"] == env.channel and rec["sender"] == env.sender
        replied = env.thread_root is not None and env.thread_root == rec["msgId"]
        if same and (replied or 0 <= age <= ATTACH_LINK_WINDOW_MS):
            matched.append(rec["path"])
        elif age > ATTACH_LINK_PRUNE_MS:
            continue                      # stale — never linkable again, drop
        else:
            keep.append(rec)
    pending[:] = keep
    return matched


def sd_notify(state: str) -> bool:
    """Send a state line to systemd via $NOTIFY_SOCKET (READY=1 / WATCHDOG=1).
    No-op (returns False) when not run under systemd or on any error — the daemon
    degrades gracefully without systemd/sdnotify (SB-4)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr[0] == "@":            # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.connect(addr)
            s.sendall(state.encode())
            return True
        finally:
            s.close()
    except OSError:
        return False


def decide(env: Envelope, cfg: Config, paused: bool,
           cursor: dict[str, int]) -> str:
    """Pure dispatch decision (PROTOCOL §2/§5/§7). No side effects."""
    route = cfg.routes.get(env.channel)
    if route is None:
        return DROP_UNROUTED
    # self / loop guards (PROTOCOL §7): own service, own bot, or system marker.
    if env.source == "local" or env.sender == cfg.bot:
        return DROP_SELF
    if env.has_marker():
        return DROP_MARKER
    # dedup (SB-1): per-conversation monotonic msgId cursor.
    if env.msg_id <= cursor.get(env.conv_id, 0):
        return DROP_SEEN
    # mode=capture: this channel is an inbox, not a front door. Capture and
    # triage want opposite things — capture must be instant, free, and lossless
    # (you are thumbing an idea in from a train); triage wants to read a week of
    # them together and is fine being batched. The other three modes fuse the
    # two, so every captured thought would spend a `claude -p` to classify one
    # fragment in isolation. Here the daemon just writes it down.
    # Daemon commands (checked below for text) still work — they are `!`-prefixed
    # and unambiguous — but nothing in a capture channel ever spawns an agent.
    if route.get("mode") == "capture":
        if env.type == "attachment":
            return CAPTURE_ATTACH
        if env.type != "text":
            return DROP_NONTEXT
        if first_token(env.body) in DAEMON_COMMANDS:
            return DAEMON_CMD
        return CAPTURE if env.body.strip() else DROP_NONTEXT

    if env.type == "attachment":
        # spec §4: a human's screenshot is bug-report payload — the
        # highest-bandwidth phone report is an image + one sentence. Stage it
        # always; dispatch when the caption gives the frontdesk something to
        # classify. Dispatchability mirrors the §5 text rules: humans free-form
        # (except command-only channels), bots only via an explicit `!task`
        # caption — which is exactly how a cross-host attachment forward
        # (FD-6, `fd.py forward --attach`) arrives on the target host.
        # Route-mode channels stage too (to a host-local spool) so a screenshot
        # on e.g. #product-chat can be re-uploaded cross-host, not dropped.
        tok = first_token(env.body)
        if route.get("mode") == "command-only" or env.sender not in cfg.humans:
            if tok not in DISPATCH_COMMANDS:
                return DROP_NONTEXT
        if paused:
            return PAUSED_PARK
        return ATTACH_DISPATCH if env.body.strip() else ATTACH_STAGE
    if env.type != "text":
        return DROP_NONTEXT
    tok = first_token(env.body)
    if tok in DAEMON_COMMANDS:
        return DAEMON_CMD
    # dispatchability (PROTOCOL §5): humans free-form (unless command-only
    # channel); everyone else command-only.
    is_human = env.sender in cfg.humans
    mode = route.get("mode", "dispatch")
    if mode == "command-only":
        dispatchable = tok in DISPATCH_COMMANDS
    elif is_human:
        dispatchable = True
    else:
        dispatchable = tok in DISPATCH_COMMANDS
    if not dispatchable:
        return DROP_NOT_DISPATCHABLE
    return PAUSED_PARK if paused else DISPATCH


class State:
    """Persisted dedup cursor: {conv_id: last_msg_id}."""
    def __init__(self, path: str):
        self.path = Path(path)
        self.cursor: dict[str, int] = {}
        if self.path.exists():
            try:
                self.cursor = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.cursor = {}

    def advance(self, conv_id: str, msg_id: int) -> None:
        if msg_id > self.cursor.get(conv_id, 0):
            self.cursor[conv_id] = msg_id
            self._persist()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Per-PID temp name: a fixed ".tmp" is a shared filename, so two writers
        # (a lingering old daemon during a restart, or a mis-started duplicate)
        # race — one replace()s, the other finds its temp already consumed and
        # dies with ENOENT. A pid-scoped temp is private to this process.
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.cursor))
        tmp.replace(self.path)  # atomic


class Daemon:
    def __init__(self, cfg: Config, dispatch_enabled: bool, react_enabled: bool,
                 verbose: bool = False):
        self.cfg = cfg
        self.dispatch_enabled = dispatch_enabled
        self.react_enabled = react_enabled
        self.verbose = verbose
        self.paused = False
        self.start_ts = time.time()
        self.state = State(cfg.state_file)
        self.kb = KeybaseAdapter(cfg.bot_home, marker_host=cfg.host)
        self.trigger = ConductorTrigger(cfg, self.log)          # spec §1
        self.dispatcher = Dispatcher(cfg, self.kb, self.log,
                                     trigger=self.trigger)      # SB-3 / HD-1
        self.commands = DaemonCommands(self)                    # SB-4 / SB-6
        self.supervisor = Supervisor(cfg, self.kb, self.log)   # SB-5
        # staged caption-less attachments awaiting their follow-up text (spec §4)
        self._pending_attach: list[dict] = []

    def log(self, *a) -> None:
        print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

    def _start_watchdog(self) -> None:
        """systemd Type=notify watchdog (SB-4). Ping WATCHDOG=1 at half the
        WatchdogSec interval. No-op without systemd."""
        sd_notify("READY=1")
        usec = os.environ.get("WATCHDOG_USEC")
        if not usec:
            return
        try:
            interval = max(1.0, int(usec) / 1e6 / 2.0)
        except ValueError:
            return

        def ping() -> None:
            while True:
                time.sleep(interval)
                sd_notify("WATCHDOG=1")

        threading.Thread(target=ping, name="sd-watchdog", daemon=True).start()
        self.log(f"sd_notify watchdog active — pinging every {interval:.0f}s")

    def _acquire_singleton(self, channels: list[str]) -> None:
        """Per-CHANNEL singleton guard: no two daemons may listen on the SAME
        channel, else every message is double-processed (👀 twice, two frontdesks
        spawned for one request).

        This was a HOST-level lock (a single `daemon.lock` beside the state file).
        That over-approximated the invariant: the thing that must not collide is a
        *channel*, not a host. The host-level guard made a legitimate MULTI-LANE
        host impossible — e.g. the laptop running `bot_laptop` on
        #app-ios AND `artilect_web` on #app-web, which are disjoint
        by construction (switchboard D4) and must both run.

        So we now lock each routed channel independently: disjoint lanes coexist,
        while a genuine channel collision still exits immediately. A daemon must
        win EVERY channel it intends to listen on or it takes none — partial
        acquisition would leave it half-serving a lane someone else owns. The fds
        are stashed on self so they stay open for the process lifetime (the OS
        releases them on exit)."""
        import fcntl
        self._lock_fds: list = []
        lock_dir = Path(self.cfg.state_file).parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        for ch in channels:
            lock_path = lock_dir / f"daemon.{ch}.lock"
            fd = open(lock_path, "a+")   # a+ = don't truncate on the loser
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fd.close()
                for held in self._lock_fds:   # all-or-nothing: drop what we won
                    held.close()
                self.log(f"another switchboard daemon holds {lock_path} — exiting "
                         f"(per-channel singleton guard; refusing to double-dispatch)")
                raise SystemExit(3)
            fd.seek(0)
            fd.truncate()
            fd.write(f"{os.getpid()}\n")
            fd.flush()
            self._lock_fds.append(fd)

    def run(self, channels: Optional[list[str]] = None) -> None:
        filters = self.cfg.filters()
        if channels:
            filters = [f for f in filters if f["topic_name"] in channels]
        # Lock only the channels we will actually listen on — not the whole host.
        self._acquire_singleton([f["topic_name"] for f in filters])
        mode = "LIVE-DISPATCH" if self.dispatch_enabled else "DRY-RUN (no dispatch)"
        self.log(f"switchboardd {self.cfg.host} as {self.cfg.bot} — {mode}; "
                 f"react={'on' if self.react_enabled else 'off'}; "
                 f"channels={[f['topic_name'] for f in filters]}")
        if not self.dispatch_enabled:
            self.log("dispatch DISABLED — will ack + log 'would dispatch', spawn nothing")
        self._start_watchdog()
        self.supervisor.start()   # SB-5 monitor tick (its own thread; never blocks)
        if self.dispatch_enabled:
            # spec §1: anything filed while the daemon was down still gets a
            # conductor promptly, not on the next mop-up tick.
            self.trigger.catch_up(self.cfg.routes)
        for env in self.kb.listen(filters):
            try:
                self.handle(env)
            except Exception as e:  # never let one message kill the loop
                self.log(f"ERROR handling msg {env.conv_id}:{env.msg_id}: {e}")
        # `listen()` is documented to block forever. Falling out of it means the
        # `keybase chat api-listen` child died (service down, not logged in,
        # network drop) — the daemon is now deaf. Returning normally here exits
        # 0, which systemd reads as "finished on purpose": `Restart=on-failure`
        # never fires and the front door stays shut, silently. That is exactly
        # how the 2026-08-04 → 08-19 outage happened. Fail loudly instead.
        raise ListenerDied(
            "keybase chat api-listen exited — daemon is deaf, forcing a restart")

    def handle(self, env: Envelope) -> None:
        outcome = decide(env, self.cfg, self.paused, self.state.cursor)
        tag = f"{env.channel}#{env.msg_id} <{env.sender}>"
        if outcome == DISPATCH:
            if self.react_enabled:
                self.kb.react(env, EYES)
            route = self.cfg.routes[env.channel]
            # spec §4: this text may be the description for screenshot(s) the
            # sender posted caption-less just before — link them into the body
            # so the filed INBOX entry carries the image path(s).
            linked = link_pending_attachments(self._pending_attach, env)
            if linked:
                env.body += "".join(f"\n[attachment: {p}]" for p in linked)
                env.attachment_path = env.attachment_path or linked[0]
                self.log(f"🔗 linked {len(linked)} staged attachment(s) → {tag}")
            if self.dispatch_enabled:
                self.log(f"DISPATCH {tag} → frontdesk cwd={route.get('repo', route.get('mode'))}")
                self._spawn_frontdesk(env, route)  # SB-3 (not yet built)
            else:
                self.log(f"👀 DRY-RUN would dispatch {tag} → "
                         f"{route.get('repo', route.get('mode'))}: {env.body[:60]!r}")
        elif outcome in (ATTACH_DISPATCH, ATTACH_STAGE):
            self._handle_attachment(env, outcome, tag)
        elif outcome in (CAPTURE, CAPTURE_ATTACH):
            self._handle_capture(env, outcome, tag)
        elif outcome == PAUSED_PARK:
            if self.react_enabled:
                self.kb.react(env, PAUSE)
            self.log(f"⏸️ paused, parked {tag}")
        elif outcome == DAEMON_CMD:
            self.log(f"daemon-cmd {first_token(env.body)} from {tag}")
            self.commands.handle(env)   # SB-4 / SB-6 — in-daemon, no LLM
        else:
            if self.verbose or outcome not in (DROP_SEEN, DROP_SELF):
                self.log(f"{outcome} {tag}")
        # advance cursor for everything we've now accounted for (PROTOCOL §2)
        if outcome != DROP_SEEN:
            self.state.advance(env.conv_id, env.msg_id)

    def _capture_sink(self, route: dict) -> Path:
        """Resolve mode=capture's `sink:` — the directory captured messages land
        in. Required by the schema for capture routes; validated at load."""
        return Path(route["sink"]).expanduser().resolve()

    def _handle_capture(self, env: Envelope, outcome: str, tag: str) -> None:
        """mode=capture: append the message verbatim to a month-stamped markdown
        log under the route's `sink`, download any payload beside it, ack 📥.

        Verbatim is the point. The sink for the job-search channel is a `raw/`
        directory whose README calls it "Marcus's own words, verbatim — source of
        truth, don't edit for polish"; a capture that summarised on the way in
        would destroy exactly what makes it useful later. So no LLM touches this
        path, and the daemon never rewrites a body.

        Failures here must not kill the listen loop or silently eat a thought:
        any error is logged and ⚠️-acked, and the cursor still advances (the
        message is accounted for), matching the attachment path's contract."""
        route = self.cfg.routes[env.channel]
        try:
            sink = self._capture_sink(route)
            sink.mkdir(parents=True, exist_ok=True)
            stamp = time.localtime(env.sent_at / 1000)
            log = sink / f"capture-{time.strftime('%Y-%m', stamp)}.md"
            body = env.body.strip()

            if outcome == CAPTURE_ATTACH:
                adir = sink / "attachments"
                adir.mkdir(parents=True, exist_ok=True)
                ext = Path(env.attachment_filename or "payload.bin").suffix or ".bin"
                dest = (adir / f"{time.strftime('%Y%m%d', stamp)}-msg{env.msg_id}{ext}").resolve()
                if not self.kb.download(env, str(dest)):
                    raise RuntimeError("attachment download failed")
                rel = dest.relative_to(sink)
                body = f"{body}\n\n![{dest.name}]({rel})" if body else f"![{dest.name}]({rel})"

            if not log.exists():
                log.write_text(
                    f"# Captured — {time.strftime('%B %Y', stamp)}\n\n"
                    f"Appended verbatim by switchboard from #{env.channel}. "
                    f"Unedited on purpose; triage happens elsewhere.\n")
            with log.open("a") as fh:
                fh.write(f"\n## {time.strftime('%Y-%m-%d %H:%M', stamp)} — "
                         f"{env.sender} (#{env.channel}#{env.msg_id})\n\n{body}\n")
        except Exception as e:
            self.log(f"CAPTURE FAILED {tag}: {e}")
            if self.react_enabled:
                self.kb.react(env, ":warning:")
            return
        if self.react_enabled:
            self.kb.react(env, INBOX)
        self.log(f"📥 captured {tag} → {log}")

    def _attachment_dir(self, route: dict, channel: str) -> Path:
        """Where payloads stage: `<campaignDir>/attachments` when the route has
        a local campaign; for routing-only channels (mode=route, no repo) a
        host-local spool under the state dir (mirrors dispatcher._audit_dir) so
        the frontdesk can re-upload the payload cross-host (FD-6)."""
        repo, cd = route.get("repo"), route.get("campaignDir")
        if repo and cd:
            return (Path(repo).expanduser() / cd).resolve() / "attachments"
        return (Path(self.cfg.state_file).expanduser().parent
                / "campaigns" / channel / "attachments")

    def _handle_attachment(self, env: Envelope, outcome: str, tag: str) -> None:
        """spec §4: stage the payload + caption in dumb code, then (when there
        is a caption) dispatch the frontdesk with the staged ABSOLUTE path in
        the body AND out-of-band on the envelope (`attachmentPath`) — absolute
        because campaign attachments are gitignored, so a repo-relative path
        dangles inside a megaloop worktree checkout; the filed INBOX entry must
        stay Read-able from wherever the wave agent runs on this host.
        Caption-less payloads are remembered so the sender's follow-up text
        message links them (see link_pending_attachments). Downloads never gate
        the listen loop's health: any failure is logged + ⚠️-acked and the
        message stays consumed."""
        import ledger
        route = self.cfg.routes[env.channel]
        adir = self._attachment_dir(route, env.channel)
        adir.mkdir(parents=True, exist_ok=True)
        ext = Path(env.attachment_filename or "payload.bin").suffix or ".bin"
        dest = (adir / f"msg{env.msg_id}{ext}").resolve()
        if not self.kb.download(env, str(dest)):
            self.log(f"attachment download FAILED {tag}")
            if self.react_enabled:
                self.kb.react(env, ":warning:")
            return
        if env.body.strip():
            (adir / f"msg{env.msg_id}.caption.txt").write_text(env.body + "\n")
        ledger.append(adir.parent, f"msg{env.msg_id}", "attachment-staged",
                      "daemon", {"file": dest.name, "sender": env.sender})
        if self.react_enabled:
            self.kb.react(env, EYES if outcome == ATTACH_DISPATCH else ":paperclip:")
        self.log(f"📎 staged {tag} → {dest.name}"
                 f"{' (dispatching caption)' if outcome == ATTACH_DISPATCH else ''}")
        if outcome == ATTACH_STAGE:
            # no caption to classify — park the path; the follow-up text (or a
            # later captioned attachment) picks it up and carries it along.
            self._pending_attach.append({
                "channel": env.channel, "sender": env.sender,
                "msgId": env.msg_id, "sentAt": env.sent_at, "path": str(dest)})
            return
        env.attachment_path = str(dest)
        linked = link_pending_attachments(self._pending_attach, env)
        env.body = (f"{env.body}\n[attachment: {dest}]"
                    + "".join(f"\n[attachment: {p}]" for p in linked))
        if linked:
            self.log(f"🔗 linked {len(linked)} staged attachment(s) → {tag}")
        if self.dispatch_enabled:
            self.dispatcher.submit(env, route)

    def _spawn_frontdesk(self, env: Envelope, route: dict) -> None:
        """SB-3: hand the envelope to the dispatcher. Non-blocking — the
        dispatcher enqueues per-channel (FIFO) and spawns `claude -p` under a
        global concurrency cap on its own threads (see dispatcher.py). The listen
        loop returns immediately."""
        self.dispatcher.submit(env, route)


def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    d = Daemon(cfg,
               dispatch_enabled=(args.dispatch == "on"),
               react_enabled=(args.react == "on"),
               verbose=args.verbose)
    chans = args.channels.split(",") if args.channels else None
    try:
        d.run(chans)
    except KeyboardInterrupt:
        d.log("stopped")
    except ListenerDied as e:
        d.log(f"FATAL: {e}")
        return 1        # non-zero so systemd's Restart=on-failure re-arms us
    return 0


def cmd_channels(args) -> int:
    cfg = Config.load(args.config)
    print(json.dumps(cfg.filters(), indent=2))
    return 0


def cmd_selftest(args) -> int:
    """Exercise the dispatch decision over synthetic envelopes. No Keybase."""
    cfg = Config(
        host="workstation", team="acme_agents", bot="bot_workstation",
        bot_home="/x", humans=["operator", "teammate"],
        approvers={"default": ["operator"]}, state_file="/x/state.json",
        routes={
            "app": {"channel": "app", "mode": "dispatch",
                           "repo": "/x/repo", "campaignDir": "campaign"},
            "agent-sync": {"channel": "agent-sync", "mode": "command-only"},
            "product-chat": {"channel": "product-chat", "mode": "route"},
            "resume": {"channel": "resume", "mode": "capture", "sink": "/x/raw"},
        },
        defaults={},
    )

    def env(sender="operator", body="hello", channel="app",
            mid=100, ctype="text", source="remote", thread=None):
        return Envelope(sender=sender, sender_device="d", team=cfg.team,
                        channel=channel, conv_id="c-" + channel, msg_id=mid,
                        thread_root=thread, body=body, sent_at=0, type=ctype,
                        source=source)

    cursor = {"c-app": 50}
    cases = [
        ("human free-form → dispatch", env(), DISPATCH),
        ("bot free-form → drop", env(sender="bot_laptop"), DROP_NOT_DISPATCHABLE),
        ("bot !task → dispatch", env(sender="bot_laptop", body="!task fix x"), DISPATCH),
        ("bot !ASK (case) → dispatch", env(sender="bot_laptop", body="!ASK y?"), DISPATCH),
        ("own bot → drop:self", env(sender="bot_workstation"), DROP_SELF),
        ("local source → drop:self", env(source="local"), DROP_SELF),
        ("marker'd → drop:marker", env(body="done ⟦sb:receipt inb=7⟧"), DROP_MARKER),
        ("non-text → drop:nontext", env(ctype="reaction"), DROP_NONTEXT),
        ("seen (mid<=cursor) → drop:seen", env(mid=40), DROP_SEEN),
        ("unrouted channel → drop:unrouted", env(channel="random", mid=100), DROP_UNROUTED),
        ("daemon cmd !status → daemon-cmd", env(body="!status"), DAEMON_CMD),
        # mode=capture: an inbox, not a front door — nothing here ever dispatches.
        ("capture: human text → capture",
         env(channel="resume", body="idea: lead the deck with the fork"), CAPTURE),
        ("capture: !task is NOT a dispatch here — it's just an idea that starts with a word",
         env(channel="resume", body="!task rewrite the summary line"), CAPTURE),
        ("capture: bot text still captured (no human/bot split in an inbox)",
         env(channel="resume", sender="bot_laptop", body="note"), CAPTURE),
        ("capture: attachment → capture:attach (no caption needed)",
         env(channel="resume", body="", ctype="attachment"), CAPTURE_ATTACH),
        ("capture: empty text → drop:nontext (nothing to write down)",
         env(channel="resume", body="   "), DROP_NONTEXT),
        ("capture: daemon cmd still works", env(channel="resume", body="!status"), DAEMON_CMD),
        ("capture: own bot still dropped (loop guard holds)",
         env(channel="resume", sender="bot_workstation"), DROP_SELF),
        ("capture: marker'd still dropped", env(channel="resume", body="x ⟦sb:digest⟧"), DROP_MARKER),
        ("daemon cmd !ping → daemon-cmd", env(body="!ping"), DAEMON_CMD),
        ("daemon cmd !pause → daemon-cmd", env(body="!pause"), DAEMON_CMD),
        ("daemon cmd !resume → daemon-cmd", env(body="!resume"), DAEMON_CMD),
        ("daemon cmd !nudge → daemon-cmd", env(body="!nudge"), DAEMON_CMD),
        ("daemon cmd !approve → daemon-cmd (in-daemon authz, no LLM)",
         env(body="!approve T45"), DAEMON_CMD),
        ("daemon cmd !recap → daemon-cmd (ledger render, no LLM)",
         env(body="!recap 6"), DAEMON_CMD),
        ("daemon cmd !retry → daemon-cmd (control file, no LLM)",
         env(body="!retry T7"), DAEMON_CMD),
        ("daemon cmd !drop → daemon-cmd (control file, no LLM)",
         env(body="!drop T7"), DAEMON_CMD),
        ("daemon cmd from bot still routes (handler does authz) → daemon-cmd",
         env(sender="bot_laptop", body="!status"), DAEMON_CMD),
        ("human free-form on command-only chan → drop",
         env(channel="agent-sync", body="hi", mid=5), DROP_NOT_DISPATCHABLE),
        ("!task on command-only chan → dispatch",
         env(channel="agent-sync", body="!task z", mid=6), DISPATCH),
        ("human captioned attachment on campaign chan → attach:dispatch",
         env(body="broken screen, see pic", ctype="attachment"), ATTACH_DISPATCH),
        ("human caption-less attachment → attach:stage",
         env(body="", ctype="attachment"), ATTACH_STAGE),
        ("bot free-form attachment → drop:nontext",
         env(sender="bot_laptop", body="x", ctype="attachment"), DROP_NONTEXT),
        ("bot !task-captioned attachment → attach:dispatch (cross-host forward)",
         env(sender="bot_laptop", body="!task broken login, see pic",
             ctype="attachment"), ATTACH_DISPATCH),
        ("attachment on command-only chan → drop:nontext",
         env(channel="agent-sync", body="pic", ctype="attachment", mid=7),
         DROP_NONTEXT),
        ("human captioned attachment on route-mode chan → attach:dispatch (spool)",
         env(channel="product-chat", body="ios paywall glitch, see pic",
             ctype="attachment", mid=8), ATTACH_DISPATCH),
        ("human caption-less attachment on route-mode chan → attach:stage",
         env(channel="product-chat", body="", ctype="attachment", mid=9),
         ATTACH_STAGE),
        ("marker'd attachment caption → drop:marker",
         env(body="done ⟦sb:receipt inb=7⟧", ctype="attachment"), DROP_MARKER),
        ("magnus !approve → daemon-cmd (in-daemon handler validates authz)",
         env(sender="teammate", body="!approve T45"), DAEMON_CMD),
    ]
    ok = True
    for name, e, expect in cases:
        got = decide(e, cfg, paused=False, cursor=cursor)
        mark = "ok " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{mark}] {name}: {got}")
    # paused path
    got = decide(env(), cfg, paused=True, cursor=cursor)
    pmark = "ok " if got == PAUSED_PARK else "FAIL"
    ok = ok and got == PAUSED_PARK
    print(f"  [{pmark}] paused human msg → paused-park: {got}")
    # adapter normalize: attachment caption becomes the body, filename rides
    ev = {"type": "chat", "source": "remote", "msg": {
        "id": 91, "sent_at_ms": 1, "conversation_id": "c1",
        "channel": {"name": "acme_agents", "topic_name": "app"},
        "sender": {"username": "operator", "device_name": "phone"},
        "content": {"type": "attachment", "attachment": {"object": {
            "filename": "IMG_1.png", "title": "synthesis missing, see pic"}}},
    }}
    ne = KeybaseAdapter.normalize(ev)
    nmark = ("ok " if ne and ne.body == "synthesis missing, see pic"
             and ne.attachment_filename == "IMG_1.png" else "FAIL")
    ok = ok and nmark == "ok "
    print(f"  [{nmark}] normalize(attachment): caption→body + filename")
    # envelope contract: the staged path rides out-of-band to the frontdesk
    assert ne is not None
    ne.attachment_path = "/x/campaign/attachments/msg91.png"
    pd = ne.to_protocol_dict()
    cmark = ("ok " if pd.get("attachmentPath") == ne.attachment_path
             and pd.get("attachmentFilename") == "IMG_1.png" else "FAIL")
    ok = ok and cmark == "ok "
    print(f"  [{cmark}] to_protocol_dict: attachmentPath/Filename ride the spawn contract")
    # follow-up linking (spec §4): caption-less screenshot + text = one report
    now = 1_000_000_000
    pend = [
        {"channel": "app", "sender": "operator", "msgId": 415,
         "sentAt": now - 60_000, "path": "/a/msg415.png"},          # fresh
        {"channel": "app", "sender": "teammate", "msgId": 500,
         "sentAt": now - 60_000, "path": "/a/msg500.png"},          # other sender
        {"channel": "app", "sender": "operator", "msgId": 300,
         "sentAt": now - 2 * ATTACH_LINK_WINDOW_MS, "path": "/a/msg300.png"},
    ]
    e_text = env(body="the scroll bug, see the screenshot", mid=416)
    e_text.sent_at = now
    got_paths = link_pending_attachments(pend, e_text)
    lmark = ("ok " if got_paths == ["/a/msg415.png"]
             and [r["msgId"] for r in pend] == [500, 300] else "FAIL")
    ok = ok and lmark == "ok "
    print(f"  [{lmark}] link: fresh same-sender staged png linked, others kept")
    # a thread-reply links regardless of the recency window
    e_reply = env(body="context for that pic", mid=999, thread=300)
    e_reply.sent_at = now
    got_paths = link_pending_attachments(pend, e_reply)
    rmark = ("ok " if got_paths == ["/a/msg300.png"]
             and [r["msgId"] for r in pend] == [500] else "FAIL")
    ok = ok and rmark == "ok "
    print(f"  [{rmark}] link: thread-reply to the attachment links past the window")
    # prune: entries beyond the horizon vanish without matching
    pend = [{"channel": "app", "sender": "operator", "msgId": 1,
             "sentAt": now - ATTACH_LINK_PRUNE_MS - 1, "path": "/a/old.png"}]
    got_paths = link_pending_attachments(pend, e_text)
    pmark2 = "ok " if got_paths == [] and pend == [] else "FAIL"
    ok = ok and pmark2 == "ok "
    print(f"  [{pmark2}] link: stale record pruned, never linked")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="switchboardd")
    sub = p.add_subparsers(dest="cmd", required=True)
    default_cfg = os.path.expanduser("~/.config/switchboard/routes.yaml")

    r = sub.add_parser("run", help="run the daemon")
    r.add_argument("--config", default=default_cfg)
    r.add_argument("--dispatch", choices=["on", "off"], default="off")
    r.add_argument("--react", choices=["on", "off"], default="on")
    r.add_argument("--channels", help="comma-separated subset of routed channels")
    r.add_argument("--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("channels", help="print resolved listen filters")
    c.add_argument("--config", default=default_cfg)
    c.set_defaults(func=cmd_channels)

    s = sub.add_parser("selftest", help="exercise the dispatch decision")
    s.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
