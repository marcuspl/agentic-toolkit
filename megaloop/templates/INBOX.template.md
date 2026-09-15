# INBOX — <campaign>

Append-only queue for switchboard (PROTOCOL §11). The frontdesk dispatcher adds
one JSONL line under `flock(INBOX.md)` per filed dev-task; the megaloop conductor
drains it under the same lock (ML-1: promote each line to a BOARD row with dedup +
finalized kind + deps + wave placement, then rewrite this block removing the
consumed lines). Nothing edits an existing line. `inbId` is monotonic per campaign
(`INB-<n>`; source of `n` = `INBOX.seq`, else max-seen+1).

```jsonl
{"inbId":"INB-7","ts":1783480343575,"sender":"marcusrydberg","channel":"true-north","msgId":812,"threadRoot":null,"kindGuess":"code","body":"add retry to the uploader"}
```
