"""Keybase transport adapter for switchboard (PROTOCOL.md §9).

The ONLY module that knows Keybase specifics. Everything above it speaks the
Envelope (PROTOCOL §1). Keybase is Zoom-owned / maintenance-mode, so keeping the
surface here means the transport is swappable without touching daemon logic.

Surface (PROTOCOL §9):
  listen(filters)            -> yields Envelope for each incoming text-ish event
  react(env, emoji)          -> post an emoji reaction (the 👀 / ⏸️ ack, SB-2)
  reply(env, body, threaded) -> post a message, in-thread when threaded
  read(channel, num)         -> backfill / cursor recovery

All keybase calls go through `keybase -H <bot_home> chat api` with the request on
STDIN (json.dumps) — this sidesteps the JSON-quoting pain noted in the
`keybase-agent-sync` memory (no shell interpolation of the body at all).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Iterator, Optional

# Every system-emitted message carries a marker (PROTOCOL §4); inbound messages
# carrying one are system-authored → dropped (loop guard, PROTOCOL §7).
MARKER_RE = re.compile(r"⟦sb:[^⟧]*⟧")


@dataclass
class Envelope:
    """Normalized message (PROTOCOL §1). Field names match the spec table."""
    sender: str
    sender_device: str
    team: str
    channel: str          # topic_name, no '#'
    conv_id: str
    msg_id: int
    thread_root: Optional[int]
    body: str
    sent_at: int          # ms
    type: str             # content.type: text / attachment / reaction / ...
    source: str           # remote / local  (local == our own service)
    attachment_filename: str = ""   # original filename (type == "attachment")
    attachment_path: str = ""       # abs path of the staged payload (daemon sets
                                    # it after download, spec §4) — "" until then
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def channel_obj(self) -> dict:
        return {"name": self.team, "members_type": "team", "topic_name": self.channel}

    def has_marker(self) -> bool:
        return bool(MARKER_RE.search(self.body))

    def to_protocol_dict(self) -> dict:
        """Serialize to the PROTOCOL §1 envelope shape (camelCase keys). This is
        the cross-lane contract handed to the frontdesk dispatcher via
        SWITCHBOARD_ENVELOPE (SB-3 spawn contract) — keep the keys aligned with
        PROTOCOL.md §1."""
        return {
            "sender": self.sender,
            "senderDevice": self.sender_device,
            "team": self.team,
            "channel": self.channel,
            "convId": self.conv_id,
            "msgId": self.msg_id,
            "threadRoot": self.thread_root,
            "body": self.body,
            "sentAt": self.sent_at,
            "type": self.type,
            # Attachment fields (spec §4): the staged payload's abs path rides
            # out-of-band so the frontdesk doesn't depend on the in-band
            # "[attachment: …]" body line surviving an LLM restatement.
            "attachmentFilename": self.attachment_filename,
            "attachmentPath": self.attachment_path,
        }


class KeybaseAdapter:
    def __init__(self, bot_home: str, marker_host: str = "?"):
        self.bot_home = bot_home            # KEYBASE_HOME for this bot (isolation)
        self.marker_host = marker_host      # stamped into ⟦sb:…host=⟧ on outbound

    # ---- low-level -------------------------------------------------------
    def _base(self) -> list[str]:
        return ["keybase", "-H", self.bot_home]

    def _api(self, request: dict) -> dict:
        """Run one `chat api` call, request via stdin (no shell quoting)."""
        proc = subprocess.run(
            self._base() + ["chat", "api"],
            input=json.dumps(request),
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"keybase chat api failed: {proc.stderr.strip()}")
        out = json.loads(proc.stdout or "{}")
        if isinstance(out, dict) and out.get("error"):
            raise RuntimeError(f"keybase chat api error: {out['error']}")
        return out

    @staticmethod
    def normalize(event: dict) -> Optional[Envelope]:
        """api-listen event OR a `read` result item -> Envelope (or None)."""
        msg = event.get("msg", event)
        if "id" not in msg:
            return None
        content = msg.get("content", {})
        ctype = content.get("type", "")
        text = content.get("text", {}) or {}
        ch = msg.get("channel", {})
        sender = msg.get("sender", {})
        # Attachments (spec §4): the caption ("title") is the message body —
        # a captioned screenshot IS a bug report — and the filename rides along
        # so the daemon can stage the payload with its real extension.
        att_filename = ""
        body = text.get("body", "") if ctype == "text" else ""
        if ctype == "attachment":
            obj = (content.get("attachment", {}) or {}).get("object", {}) or {}
            body = (obj.get("title") or "").strip()
            att_filename = obj.get("filename", "")
        return Envelope(
            sender=sender.get("username", ""),
            sender_device=sender.get("device_name", ""),
            team=ch.get("name", ""),
            channel=ch.get("topic_name", ""),
            conv_id=msg.get("conversation_id", ""),
            msg_id=int(msg.get("id", 0)),
            thread_root=text.get("replyTo"),
            body=body,
            sent_at=int(msg.get("sent_at_ms", 0)),
            type=ctype,
            source=event.get("source", "remote"),
            attachment_filename=att_filename,
            raw=msg,
        )

    # ---- surface ---------------------------------------------------------
    def listen(self, filters: list[dict]) -> Iterator[Envelope]:
        """Stream Envelopes from `keybase chat api-listen`.

        `filters` is the --filter-channels list of channel objects. Non-chat
        notifications and un-normalizable events are skipped. Blocks forever;
        the caller (daemon) supervises the process lifetime.
        """
        cmd = self._base() + ["chat", "api-listen",
                              "--filter-channels", json.dumps(filters)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # status/banner lines go to stderr, but be safe
                if event.get("type") != "chat":
                    continue  # conv / wallet notifications
                env = self.normalize(event)
                if env is not None:
                    yield env
        finally:
            proc.terminate()

    def download(self, env: Envelope, dest: str) -> bool:
        """Fetch an attachment message's payload to a local path (spec §4).
        `chat api` has no download method — this is the one CLI-subcommand
        call in the adapter. Returns False on any failure; never raises."""
        try:
            proc = subprocess.run(
                self._base() + ["chat", "download", env.team,
                                "--channel", env.channel,
                                str(env.msg_id), "-o", dest],
                capture_output=True, text=True, timeout=120,
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def react(self, env: Envelope, emoji: str) -> None:
        """Post an emoji reaction on env's message (the 👀/⏸️ ack, SB-2)."""
        self._api({"method": "reaction", "params": {"options": {
            "channel": env.channel_obj,
            "message_id": env.msg_id,
            "message": {"body": emoji},
        }}})

    def post(self, channel_obj: dict, body: str, marker: Optional[str] = None,
             reply_to: Optional[int] = None) -> None:
        """Low-level channel send with a mandatory ⟦sb:…⟧ marker (PROTOCOL §4 /
        invariant 6). Used for both threaded replies and top-level channel posts
        (supervision alerts, SB-5; debrief-style posts). `reply_to` threads it."""
        if marker is None:
            marker = f"⟦sb:reply host={self.marker_host}⟧"
        opts = {"channel": channel_obj, "message": {"body": f"{body}  {marker}"}}
        if reply_to:
            opts["reply_to"] = reply_to
        self._api({"method": "send", "params": {"options": opts}})

    def reply(self, env: Envelope, body: str, threaded: bool = True,
              marker: Optional[str] = None) -> None:
        """Post a message. Threaded to env by default. Appends a ⟦sb:…⟧ marker
        (PROTOCOL §4) so the system never re-dispatches its own output."""
        reply_to = (env.thread_root or env.msg_id) if threaded else None
        self.post(env.channel_obj, body, marker, reply_to=reply_to)

    def read(self, channel: str, team: str, num: int = 10) -> list[Envelope]:
        out = self._api({"method": "read", "params": {"options": {
            "channel": {"name": team, "members_type": "team", "topic_name": channel},
            "pagination": {"num": num},
        }}})
        msgs = out.get("result", {}).get("messages", [])
        return [e for e in (self.normalize(m) for m in msgs) if e is not None]
