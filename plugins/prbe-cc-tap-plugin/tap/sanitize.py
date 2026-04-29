"""Sanitize Claude Code transcript events before shipping.

The raw JSONL Claude Code writes is bloated with API metadata that has no
value for the knowledge graph: token-usage tallies, cache stats, big base64
`signature` blobs on thinking blocks, internal CC bookkeeping events
(`stop_hook_summary`, `turn_duration`).

What we actually want to send is the *content*: user prompts, assistant text,
tool calls, tool results — plus the threading metadata (uuid, parentUuid,
sessionId, timestamp, role) needed to reconstruct the conversation
downstream. Everything else is noise.

`sanitize_event(event)` returns:
  - None        → drop the event entirely (CC bookkeeping with no content)
  - dict        → trimmed copy with the noise fields removed
  - input as-is → if the input isn't a dict (defensive — non-JSON lines
                  shouldn't reach here, but if they do we don't mangle them)
"""

from __future__ import annotations

from typing import Any

# Top-level fields to drop from every event. These are CC bookkeeping or
# Anthropic API metadata, never content.
_DROP_TOP_LEVEL: frozenset[str] = frozenset({
    "requestId",
    "isSidechain",
    "isMeta",
    "diagnostics",
})

# Fields inside `message` that are pure API/runtime metadata, not content.
_DROP_MESSAGE: frozenset[str] = frozenset({
    "usage",
    "iterations",
    "cache_creation",
    "service_tier",
    "inference_geo",
    "speed",
    "stop_details",
    "stop_sequence",
    "diagnostics",
    "id",  # Anthropic's per-message API id; we already keep top-level uuid
})

# `system` events with these subtypes have no content — drop entirely.
# stop_hook_summary  = CC's per-hook timing/output; pure bookkeeping.
# turn_duration      = how long a turn took; pure bookkeeping.
_DROP_SYSTEM_SUBTYPES: frozenset[str] = frozenset({
    "stop_hook_summary",
    "turn_duration",
})

# `thinking` blocks carry both a `thinking` text field (content — keep) and
# a `signature` field (huge base64-encoded model state — drop). Stripping
# signature shrinks payloads dramatically without losing any human-readable
# content.
_THINKING_DROP: frozenset[str] = frozenset({"signature"})


def sanitize_event(event: Any) -> Any:
    """Trim a transcript event to ship only content, not Anthropic API metadata.

    Returns None for events that should be dropped entirely.
    """
    if not isinstance(event, dict):
        return event

    # Drop CC-internal system events with no content value.
    if event.get("type") == "system":
        sub = event.get("subtype")
        if sub in _DROP_SYSTEM_SUBTYPES:
            return None

    out = {k: v for k, v in event.items() if k not in _DROP_TOP_LEVEL}

    msg = out.get("message")
    if isinstance(msg, dict):
        msg_out = {k: v for k, v in msg.items() if k not in _DROP_MESSAGE}
        content = msg_out.get("content")
        if isinstance(content, list):
            msg_out["content"] = [_sanitize_block(b) for b in content]
        out["message"] = msg_out

    return out


def _sanitize_block(block: Any) -> Any:
    """Per-content-block sanitization. Currently: drop the giant `signature`
    field on thinking blocks. Other block types pass through unchanged so
    we don't accidentally drop content (e.g. tool_use input args, tool_result
    output content, plain text)."""
    if not isinstance(block, dict):
        return block
    if block.get("type") == "thinking":
        return {k: v for k, v in block.items() if k not in _THINKING_DROP}
    return block
