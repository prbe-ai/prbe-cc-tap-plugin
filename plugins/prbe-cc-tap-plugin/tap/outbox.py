"""Build batch payloads, enqueue them, and drain the outbox.

Mirrors prbe-agent-tap's buildBatchBody + drainer.tick. `raw` events embed
the parsed-JSON line as a JSON value (not a stringified one), so the
backend sees the same structure Claude Code wrote.
"""

from __future__ import annotations

import json
import logging
import time

from tap import config as cfg
from tap import httpclient
from tap.storage import Storage

log = logging.getLogger("prbe-cc-tap.outbox")


class HaltError(Exception):
    """Raised when the server returns 401 — token is dead, daemon must exit."""


def build_batch_body(
    *,
    device_id: str,
    session_id: str,
    batch_seq: int,
    cwd: str,
    base_line_no: int,
    lines: list[bytes],
) -> bytes:
    """Construct the JSON body for /webhooks/claude_code.

    `raw` per-line is the parsed JSON value (we know lines validated). If a
    line somehow fails to parse we embed the raw string instead, matching
    Go's lenient json.RawMessage when faced with non-JSON.
    """
    events = []
    for i, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raw = line.decode("utf-8", errors="replace")
        events.append({"line_no": base_line_no + i, "raw": raw})
    body = {
        "device_id": device_id,
        "session_id": session_id,
        "batch_seq": batch_seq,
        "cwd": cwd,
        "events": events,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def enqueue(
    *,
    storage: Storage,
    session_id: str,
    batch_seq: int,
    cwd: str,
    body: bytes,
    now: int,
) -> None:
    storage.enqueue_batch(
        session_id=session_id,
        batch_seq=batch_seq,
        cwd=cwd,
        body=body,
        created_at=now,
        next_attempt_at=now,
    )


def drain_once(*, storage: Storage, token: str, base_url: str, session_id: str) -> bool:
    """Pop the next due batch for session_id and POST it.

    Returns True if a row was processed (caller may want to drain again),
    False if this session has nothing due. Raises HaltError on 401.
    """
    now = int(time.time())
    row = storage.next_due_batch(now, session_id)
    if row is None:
        storage.enforce_outbox_cap()
        return False

    if not token:
        storage.mark_failure(row.id, now + 30, "no device token")
        return True

    url = base_url + cfg.WEBHOOK_PATH
    resp = httpclient.post_json(url, row.body, bearer=token)

    if resp.classification == httpclient.Classification.SUCCESS:
        storage.mark_success(row.id)
        storage.set_meta("last_successful_post_at", str(now))
        return True
    if resp.classification == httpclient.Classification.POISON:
        log.warning(
            "outbox: poison drop id=%d status=%d body=%r",
            row.id, resp.status, resp.body[:200],
        )
        storage.mark_success(row.id)
        return True
    if resp.classification == httpclient.Classification.HALT:
        storage.clear_outbox()
        storage.set_meta("last_401_at", str(now))
        raise HaltError("device token revoked (401)")

    msg = resp.error or f"http {resp.status}"
    next_at = now + int(httpclient.backoff_seconds(row.attempt_count))
    storage.mark_failure(row.id, next_at, msg)
    return True
