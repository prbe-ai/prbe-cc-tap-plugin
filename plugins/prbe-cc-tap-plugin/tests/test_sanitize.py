"""Tests for transcript event sanitization.

Two layers:
  - sanitize_event() unit tests: per-field drop behavior, content preservation
  - build_batch_body() integration: the body is None when every line gets
    sanitized away, otherwise contains only the kept events
"""

from __future__ import annotations

import json

from tap.outbox import build_batch_body
from tap.sanitize import sanitize_event


# ---------------------------------------------------------------------------
# sanitize_event — drop full events (CC bookkeeping)
# ---------------------------------------------------------------------------


def test_drops_stop_hook_summary() -> None:
    event = {
        "type": "system",
        "subtype": "stop_hook_summary",
        "hookCount": 1,
        "uuid": "x",
    }
    assert sanitize_event(event) is None


def test_drops_turn_duration() -> None:
    event = {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 347448,
        "messageCount": 140,
        "uuid": "y",
    }
    assert sanitize_event(event) is None


def test_keeps_unknown_system_subtypes() -> None:
    """A `system` event we don't explicitly drop passes through (after
    top-level + message-level cleanup). Defensive: don't silently drop new
    system events CC adds in the future."""
    event = {"type": "system", "subtype": "user_warning", "uuid": "z"}
    out = sanitize_event(event)
    assert out is not None
    assert out["type"] == "system"
    assert out["subtype"] == "user_warning"


# ---------------------------------------------------------------------------
# sanitize_event — top-level field stripping
# ---------------------------------------------------------------------------


def test_drops_request_id_and_meta_flags() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "requestId": "req_xyz",
        "isSidechain": False,
        "isMeta": False,
        "diagnostics": None,
        "timestamp": "2026-04-29T19:31:18.640Z",
    }
    out = sanitize_event(event)
    assert "requestId" not in out
    assert "isSidechain" not in out
    assert "isMeta" not in out
    assert "diagnostics" not in out
    assert out["uuid"] == "u"
    assert out["timestamp"] == "2026-04-29T19:31:18.640Z"


def test_keeps_threading_metadata() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "parentUuid": "p",
        "sessionId": "s",
        "timestamp": "2026-04-29T19:31:18.640Z",
        "userType": "external",
        "cwd": "/x",
        "gitBranch": "main",
    }
    out = sanitize_event(event)
    assert out["uuid"] == "u"
    assert out["parentUuid"] == "p"
    assert out["sessionId"] == "s"
    assert out["timestamp"] == "2026-04-29T19:31:18.640Z"
    assert out["userType"] == "external"
    assert out["cwd"] == "/x"
    assert out["gitBranch"] == "main"


# ---------------------------------------------------------------------------
# sanitize_event — message-level stripping
# ---------------------------------------------------------------------------


def test_drops_usage_iterations_cache_creation() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1, "output_tokens": 100},
            "iterations": [{"input_tokens": 1}],
            "cache_creation": {"ephemeral_5m_input_tokens": 0},
            "service_tier": "standard",
            "inference_geo": "",
            "speed": "standard",
            "id": "msg_anthropic_internal",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "stop_details": None,
        },
    }
    out = sanitize_event(event)
    msg = out["message"]
    assert "usage" not in msg
    assert "iterations" not in msg
    assert "cache_creation" not in msg
    assert "service_tier" not in msg
    assert "inference_geo" not in msg
    assert "speed" not in msg
    assert "id" not in msg, "Anthropic's per-message id is dropped (top-level uuid is enough)"
    assert "stop_sequence" not in msg
    assert "stop_details" not in msg
    # Stop_reason is content-relevant; keep it.
    assert msg["stop_reason"] == "end_turn"
    # Content survives intact.
    assert msg["content"] == [{"type": "text", "text": "hi"}]
    assert msg["role"] == "assistant"


# ---------------------------------------------------------------------------
# sanitize_event — content block sanitization
# ---------------------------------------------------------------------------


def test_drops_thinking_signature_keeps_thinking_text() -> None:
    """The big base64 `signature` blob on thinking blocks is the single
    largest field in real CC transcripts. Dropping it shrinks payloads
    dramatically; the actual `thinking` text is preserved."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Let me think about this carefully.",
                    "signature": "EqwgClkIDRgC..." * 200,  # huge base64 blob
                },
                {"type": "text", "text": "Here's my answer."},
            ],
        },
    }
    out = sanitize_event(event)
    blocks = out["message"]["content"]
    assert len(blocks) == 2
    # Thinking block: text kept, signature dropped.
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "Let me think about this carefully."
    assert "signature" not in blocks[0]
    # Text block untouched.
    assert blocks[1] == {"type": "text", "text": "Here's my answer."}


def test_passes_through_tool_use_and_tool_result_blocks() -> None:
    """Tool calls + results are content — don't strip anything from them."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Bash",
                    "input": {"command": "ls", "description": "list files"},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "file1\nfile2\n",
                    "is_error": False,
                },
            ],
        },
    }
    out = sanitize_event(event)
    blocks = out["message"]["content"]
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["name"] == "Bash"
    assert blocks[0]["input"] == {"command": "ls", "description": "list files"}
    assert blocks[1]["type"] == "tool_result"
    assert blocks[1]["content"] == "file1\nfile2\n"


def test_user_event_with_text_content_kept_intact() -> None:
    event = {
        "type": "user",
        "uuid": "u",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "What's the weather?"}],
        },
    }
    out = sanitize_event(event)
    assert out["type"] == "user"
    assert out["message"]["content"][0]["text"] == "What's the weather?"


# ---------------------------------------------------------------------------
# sanitize_event — defensive on weird inputs
# ---------------------------------------------------------------------------


def test_non_dict_event_passes_through() -> None:
    """If a malformed line ends up here as a string (lenient JSON fallback),
    don't try to sanitize it — pass through so the caller sees raw input."""
    assert sanitize_event("not a dict") == "not a dict"
    assert sanitize_event(42) == 42
    assert sanitize_event(None) is None  # but this collides with "drop entire event" — see note below
    assert sanitize_event([1, 2, 3]) == [1, 2, 3]


def test_message_without_content_list_passes_through() -> None:
    """If `message` exists but has no list `content`, leave content alone."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": "plain string content"},
    }
    out = sanitize_event(event)
    assert out["message"]["content"] == "plain string content"


# ---------------------------------------------------------------------------
# build_batch_body integration
# ---------------------------------------------------------------------------


def _line(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")


def test_build_batch_body_returns_none_when_all_dropped() -> None:
    """Tick that only saw bookkeeping events → no payload to ship."""
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=0,
        cwd="/x",
        base_line_no=0,
        lines=[
            _line({"type": "system", "subtype": "stop_hook_summary", "uuid": "1"}),
            _line({"type": "system", "subtype": "turn_duration", "uuid": "2"}),
        ],
    )
    assert body is None


def test_build_batch_body_keeps_content_drops_bookkeeping() -> None:
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=3,
        cwd="/x",
        base_line_no=10,
        lines=[
            _line({"type": "user", "uuid": "u1", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
            _line({"type": "system", "subtype": "stop_hook_summary", "uuid": "u2"}),
            _line({"type": "assistant", "uuid": "u3", "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}}),
        ],
    )
    assert body is not None
    parsed = json.loads(body)
    assert parsed["session_id"] == "sess"
    assert parsed["batch_seq"] == 3
    # Only the user + assistant events survive. Bookkeeping line is gone.
    events = parsed["events"]
    assert len(events) == 2
    assert events[0]["line_no"] == 10
    assert events[0]["raw"]["type"] == "user"
    assert events[1]["line_no"] == 12
    assert events[1]["raw"]["type"] == "assistant"


def test_build_batch_body_strips_thinking_signature_and_usage() -> None:
    """End-to-end: a real-shaped assistant message with all the noise fields
    ships only the content."""
    big_signature = "Eqwg" + "A" * 5000  # mock the giant base64
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=0,
        cwd="/x",
        base_line_no=0,
        lines=[
            _line({
                "type": "assistant",
                "uuid": "u",
                "requestId": "req_drop_me",
                "message": {
                    "role": "assistant",
                    "id": "msg_drop_me",
                    "content": [
                        {"type": "thinking", "thinking": "reasoning text", "signature": big_signature},
                        {"type": "text", "text": "answer"},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 100, "cache_read_input_tokens": 99999},
                    "iterations": [{"input_tokens": 1}],
                    "cache_creation": {"ephemeral_5m_input_tokens": 0},
                    "service_tier": "standard",
                    "stop_reason": "end_turn",
                },
            }),
        ],
    )
    assert body is not None
    parsed = json.loads(body)
    assert "requestId" not in parsed["events"][0]["raw"]
    msg = parsed["events"][0]["raw"]["message"]
    assert "usage" not in msg
    assert "iterations" not in msg
    assert "cache_creation" not in msg
    assert "service_tier" not in msg
    assert "id" not in msg
    assert msg["stop_reason"] == "end_turn"
    blocks = msg["content"]
    assert blocks[0]["thinking"] == "reasoning text"
    assert "signature" not in blocks[0]
    assert blocks[1] == {"type": "text", "text": "answer"}
    # Sanity: payload is much smaller than what it would have been with the signature.
    assert len(body) < 1000, f"payload should be under 1KB after stripping, got {len(body)}"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
