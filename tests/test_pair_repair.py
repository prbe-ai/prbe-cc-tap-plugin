"""Tests for re-pair behavior — the auto-revoke of the prior server-side
device entry when `python -m tap pair <new-token>` runs on a laptop that's
already paired.

Five scenarios, all gated through tap.httpclient.post_json mock:
  1. First-ever pair (no .token on disk) → no revoke call.
  2. Re-pair, old revoke succeeds → success message, both calls made.
  3. Re-pair, old revoke 401 (already gone) → silent, no spurious warning.
  4. Re-pair, old revoke fails (network/5xx) → warning emitted, new pair
     still wins; user is paired against the new device.
  5. New pair fails → no revoke attempted, old token stays on disk so the
     user isn't stranded.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from tap import httpclient


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="prbe-pair-test-")
    monkeypatch.setenv("PRBE_CC_TAP_PLUGIN_DIR", tmp)
    monkeypatch.setenv("PRBE_API_BASE_URL", "https://api.invalid")
    yield Path(tmp)


def _success_response(body: bytes) -> httpclient.Response:
    return httpclient.Response(
        status=200, body=body,
        classification=httpclient.Classification.SUCCESS,
    )


def _halt_response() -> httpclient.Response:
    return httpclient.Response(
        status=401, body=b"",
        classification=httpclient.Classification.HALT,
        error="HTTP Error 401",
    )


def _retry_response(status: int = 503, error: str = "upstream down") -> httpclient.Response:
    return httpclient.Response(
        status=status, body=b"upstream",
        classification=httpclient.Classification.RETRY,
        error=error,
    )


def _pair_body(device_id: str = "new-dev-1", token: str = "new-token") -> bytes:
    import json as _j
    return _j.dumps({
        "device_id": device_id,
        "device_token": token,
        "customer_id": "cust-1",
    }).encode("utf-8")


# ---------------------------------------------------------------------------


def test_first_pair_does_not_revoke(_isolated_plugin_dir: Path) -> None:
    """No .token on disk yet — pair runs once, doesn't try to revoke
    something that doesn't exist."""
    from tap.pair import run

    calls: list[dict] = []

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        calls.append({"url": url, "bearer": bearer})
        # Only the pair URL should be hit.
        assert url.endswith("/agent-tap/pair")
        return _success_response(_pair_body())

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    assert len(calls) == 1, "first pair must NOT call revoke"
    assert calls[0]["url"].endswith("/agent-tap/pair")


def test_repair_revokes_old_token_after_new_pair(_isolated_plugin_dir: Path, capsys) -> None:
    """A second pair on the same laptop captures the old bearer, mints
    the new one, and POSTs revoke with the OLD bearer. The old device is
    cleanly retired."""
    from tap import config as cfg
    from tap.pair import run

    # Seed an existing pairing on disk.
    cfg.write_token("old-token")

    pair_calls: list[dict] = []
    revoke_calls: list[dict] = []

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            pair_calls.append({"bearer": bearer})
            return _success_response(_pair_body(token="brand-new"))
        if url.endswith("/agent-tap/revoke"):
            revoke_calls.append({"bearer": bearer})
            return _success_response(b'{"ok":true}')
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    assert len(pair_calls) == 1
    assert pair_calls[0]["bearer"] is None  # pair endpoint is JWT-in-body, no bearer
    assert len(revoke_calls) == 1, "re-pair must revoke the old server-side device"
    assert revoke_calls[0]["bearer"] == "old-token", "must revoke with the OLD bearer, not the new one"

    out = capsys.readouterr().out
    assert "Revoked previous pairing on this device." in out
    assert "Paired." in out

    # Confirm the new token is what's on disk now.
    assert cfg.load_token() == "brand-new"


def test_repair_silently_ignores_revoke_401(_isolated_plugin_dir: Path, capsys) -> None:
    """Old token might already be revoked (e.g. user revoked from
    dashboard before re-pairing). The 401 response is a benign no-op —
    no scary warning needed."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-already-revoked-token")

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            return _success_response(_pair_body())
        if url.endswith("/agent-tap/revoke"):
            return _halt_response()
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    out = capsys.readouterr()
    assert "Revoked previous pairing" not in out.out
    assert "warning" not in out.err.lower()
    assert "Paired." in out.out


def test_repair_warns_but_succeeds_when_revoke_fails(_isolated_plugin_dir: Path, capsys) -> None:
    """Revoke 5xx / network blip leaves the old device orphaned in the
    dashboard. We surface a warning but the new pair still commits — the
    user is never stranded over a flaky revoke."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-token")

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            return _success_response(_pair_body(token="brand-new"))
        if url.endswith("/agent-tap/revoke"):
            return _retry_response()
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "could not revoke previous pairing" in err
    # User is paired against the new device — that part isn't compromised.
    assert cfg.load_token() == "brand-new"


def test_failed_new_pair_does_not_touch_old_token(_isolated_plugin_dir: Path) -> None:
    """If the new pair fails (bad pairing token, server 5xx), we must
    NOT revoke the old one. Otherwise a bad re-pair attempt would
    strand the user with no working pairing at all."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-token")

    revoke_calls: list[dict] = []

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            return _halt_response()  # pairing token rejected
        if url.endswith("/agent-tap/revoke"):
            revoke_calls.append({"bearer": bearer})
            return _success_response(b'{"ok":true}')
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("rejected-pairing-token")

    assert rc == 1, "bad pair must surface as failure"
    assert revoke_calls == [], "must NOT revoke the old token on failed re-pair"
    # Old token is still on disk — user is still paired.
    assert cfg.load_token() == "old-token"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
