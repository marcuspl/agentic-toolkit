#!/usr/bin/env python3
"""digest — a standing status rollup for the switchboard, posted to a low-noise
overview channel (#switchboard). Answers "what's the state of the work?" without
scrolling a busy task channel: shipped / merged-awaiting-deploy / open / recent.

Deterministic, no LLM. Reads the campaign BOARD.md (resolved from routes.yaml by
channel) + optionally the meta switchboard build board, renders a phone-sized
digest, and posts it with a ⟦sb:digest⟧ marker (so the daemon's loop guard drops
it). Idle-quiet: it fingerprints the rollup and skips posting when nothing changed
since the last digest (state in ~/.config/switchboard/digest-state.json), so a
daily cron over a quiet week stays silent.

Usage:
  digest.py --channel app --to switchboard [--config PATH] [--force] [--print]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import fcntl
import subprocess
import sys
from pathlib import Path

import yaml

_ROW = re.compile(r"^\| (?P<id>[A-Za-z]+[\w-]*) \|")
_STATUSES = ("TODO", "BLOCKED", "DISPATCHED", "SELF-REVIEW", "RETURNED", "MERGED",
             "DONE", "GATED", "DEFERRED", "WONTFIX", "FAILED", "LIVE", "PARTIAL")
_TID = re.compile(r"\bT\d+\b")


def _rows(board: Path) -> list[dict]:
    """Parse board rows format-agnostically: id = first cell; status = whichever
    known status token is a standalone cell (works across the campaign board's and
    the meta build board's differing column layouts)."""
    out = []
    for ln in board.read_text().splitlines():
        m = _ROW.match(ln)
        if not m:
            continue
        cells = [x.strip() for x in ln.split("|")]
        status = next((c for c in cells if c in _STATUSES), None)
        if not status:
            continue
        out.append({"id": m.group("id"), "title": cells[2] if len(cells) > 2 else "",
                    "kind": cells[3] if len(cells) > 4 else "", "status": status,
                    "notes": cells[-2] if len(cells) > 2 else ""})
    return out


def _recent_merges(board: Path, n: int = 4) -> list[str]:
    """Headlines of the most recent merge-log entries. The merge log is the
    authoritative deploy record and states deploy state in prose ('SHIPPED TO
    PROD' / 'NOT pushed / NOT deployed'), so surfacing its latest entries is a
    reliable way to convey what's live vs. awaiting a gate — without brittle
    per-row deploy-state inference from the board table."""
    text = board.read_text()
    i = text.find("## Merge log")
    if i == -1:
        return []
    out = []
    for para in text[i:].split("\n- ")[1:]:
        # take the bolded lead sentence, else the first line, trimmed
        m = re.search(r"\*\*(.+?)\*\*", para)
        head = (m.group(1) if m else para.splitlines()[0]).strip().lstrip("*").strip()
        if head and not head.startswith("(none"):
            out.append(_trim(head, 96))
        if len(out) >= n:
            break
    return out


def _trim(s: str, n: int = 52) -> str:
    return (s[: n - 1].rstrip() + "…") if len(s) > n else s



def _listening(raw: dict) -> tuple[list[str], list[str]]:
    """Which routed channels actually have a daemon listening on them.

    Host-agnostic on purpose: rather than asking systemd (workstation) or launchd
    (laptop), test the invariant directly. switchboardd holds an exclusive
    flock on `daemon.<channel>.lock` for every channel it listens to, so a lock
    we can take is a channel nobody is hearing. Opened "a+" — never truncate,
    the loser of the race must not clobber the winner's pid.
    """
    lock_dir = Path(os.path.expanduser(raw["state_file"])).parent
    live, deaf = [], []
    for ch in [r["channel"] for r in raw.get("routes", [])]:
        path = lock_dir / f"daemon.{ch}.lock"
        if not path.exists():
            deaf.append(ch)
            continue
        try:
            with path.open("a+") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    deaf.append(ch)        # we got it → nobody was holding it
                except OSError:
                    live.append(ch)        # held → a daemon is listening
        except OSError:
            deaf.append(ch)
    return live, deaf


def _channel_drift(raw: dict) -> list[str]:
    """Channels that exist on the team but appear in neither `routes` nor
    `unrouted_ok`. Invariant #5 makes unlisted channels silent, which is correct
    but indistinguishable from forgotten — four channels sat unrouted for weeks
    while their authors assumed someone was listening. This names them."""
    try:
        out = subprocess.run(
            ["keybase", "-H", os.path.expanduser(raw["bot_home"]),
             "chat", "list-channels", raw["team"]],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    existing = re.findall(r"^#(\S+)", out, re.M)
    known = {r["channel"] for r in raw.get("routes", [])} | set(raw.get("unrouted_ok", []))
    return sorted(c for c in existing if c not in known)


def health(raw: dict) -> tuple[list[str], bool]:
    """Lines for the digest's health block + whether everything is nominal.

    `ok=False` must defeat idle-quiet upstream: a persistently dead front door
    renders a byte-identical digest every day, so fingerprint suppression would
    turn the one condition worth shouting about into permanent silence. That is
    how a two-week outage stays invisible while the digest posts cheerfully.
    """
    live, deaf = _listening(raw)
    drift = _channel_drift(raw)
    L, ok = [], True
    if deaf:
        ok = False
        L.append(f"🔴 NOT LISTENING: {', '.join('#' + c for c in deaf)} — "
                 f"messages there are going nowhere. Check the daemon.")
    if live:
        L.append(f"🟢 listening: {', '.join('#' + c for c in live)}")
    if drift:
        # Deliberately does NOT clear `ok`. Drift is informational, not urgent:
        # it belongs in the body (so the fingerprint changes and you get told
        # once, the day a new channel appears) but forcing a daily post until
        # someone routes four channels would be the noise that trains people to
        # skim the digest — which is how the dead-daemon line gets missed too.
        L.append(f"⚠️ unrouted channels exist: {', '.join('#' + c for c in drift)} — "
                 f"route them in routes.yaml or add to `unrouted_ok`.")
    return L, ok


def render(campaign_board: Path, meta_board: "Path | None",
           raw: "dict | None" = None) -> tuple[str, bool]:
    rows = _rows(campaign_board)
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)
    open_rows = [r for r in rows if r["status"] in ("TODO", "RETURNED", "BLOCKED", "GATED")]

    L = [f"📊 Switchboard digest — {campaign_board.parent.name}",
         f"({len(rows)} rows: " +
         ", ".join(f"{len(v)} {k}" for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))) + ")",
         ""]
    recent = _recent_merges(campaign_board)
    if recent:
        L.append("🔀 Recent merges / releases (deploy state in each line):")
        L += [f"  • {h}" for h in recent]
        L.append("")
    if open_rows:
        L.append("🔜 Open / pending:")
        L += [f"  • {r['id']} [{r['status'].lower()}] {_trim(r['title'])}" for r in open_rows]
        L.append("")
    if meta_board and meta_board.exists():
        mrows = _rows(meta_board)
        done = sum(1 for r in mrows if r["status"] in ("DONE", "LIVE"))
        L.append(f"🧰 Switchboard build: {done}/{len(mrows)} done "
                 f"(system built + live; remainder = mac activation + Go port).")
    ok = True
    if raw is not None:
        hl, ok = health(raw)
        if hl:
            L.append("")
            L += hl
            L.append("")
    L.append("Full board: `!status` · per-row timeline: `!recap` · "
             "merged-not-deployed → see merge log.")
    return "\n".join(L), ok


def _load(config: str, channel: str):
    raw = yaml.safe_load(Path(config).expanduser().read_text())
    route = {r["channel"]: r for r in raw.get("routes", [])}.get(channel)
    if not route or not (route.get("repo") and route.get("campaignDir")):
        sys.exit(f"digest: channel '{channel}' has no repo+campaignDir")
    board = (Path(route["repo"]).expanduser() / route["campaignDir"] / "BOARD.md").resolve()
    return raw, board


def _post(raw: dict, to_topic: str, body: str) -> None:
    req = {"method": "send", "params": {"options": {
        "channel": {"name": raw["team"], "members_type": "team", "topic_name": to_topic},
        "message": {"body": f"{body}\n⟦sb:digest host={raw.get('host', '?')}⟧"}}}}
    subprocess.run(["keybase", "-H", os.path.expanduser(raw["bot_home"]), "chat", "api"],
                   input=json.dumps(req), capture_output=True, text=True, timeout=45)


def main() -> int:
    p = argparse.ArgumentParser(prog="digest")
    p.add_argument("--channel", required=True, help="campaign channel to summarize")
    p.add_argument("--to", required=True, help="topic to post the digest to")
    p.add_argument("--config", default=os.path.expanduser("~/.config/switchboard/routes.yaml"))
    p.add_argument("--meta-board",
                   default=os.path.expanduser("~/.config/switchboard/BOARD.md"))
    p.add_argument("--force", action="store_true", help="post even if unchanged")
    p.add_argument("--print", action="store_true", help="print only, do not post")
    args = p.parse_args()

    raw, board = _load(args.config, args.channel)
    body, ok = render(board, Path(args.meta_board), raw)

    if args.print:
        print(body)
        return 0

    # idle-quiet: skip if the rollup is byte-identical to the last one posted
    state = Path(args.config).expanduser().parent / "digest-state.json"
    sig = hashlib.sha256(body.encode()).hexdigest()
    if not args.force and ok:
        try:
            if json.loads(state.read_text()).get(args.to) == sig:
                print("digest: unchanged since last post — idle-quiet, skipping")
                return 0
        except (OSError, json.JSONDecodeError):
            pass
    _post(raw, args.to, body)
    try:
        prev = json.loads(state.read_text()) if state.exists() else {}
    except json.JSONDecodeError:
        prev = {}
    prev[args.to] = sig
    state.write_text(json.dumps(prev))
    print(f"digest: posted to #{args.to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
