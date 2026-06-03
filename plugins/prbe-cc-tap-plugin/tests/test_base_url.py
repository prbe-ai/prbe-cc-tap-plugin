"""Backend-host resolution — the "no hardcoded fallback" contract.

The host is learned from the pairing token's `iss` claim and persisted; an
env var can override. There is deliberately no baked-in default (a stale one
is what silently broke ingestion when the backend moved off api.prbe.ai), so
an unconfigured plugin must fail loudly rather than guess.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import pytest

from tap import config as cfg


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="prbe-baseurl-test-")
    monkeypatch.setenv("PRBE_CC_TAP_PLUGIN_DIR", tmp)
    monkeypatch.delenv("PRBE_API_BASE_URL", raising=False)
    yield Path(tmp)


def _make_jwt(payload: dict) -> str:
    """A structurally-valid JWT (unsigned-ish). Signature is never verified
    locally — only the payload's claims are read."""
    def _seg(d: dict) -> str:
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_seg({'alg': 'HS256', 'typ': 'JWT'})}.{_seg(payload)}.sig"


# --- the contract: no hardcoded fallback -----------------------------------


def test_no_hardcoded_default_constant() -> None:
    """A reintroduced DEFAULT_API_BASE_URL is the regression we're guarding."""
    assert not hasattr(cfg, "DEFAULT_API_BASE_URL")


def test_api_base_url_raises_when_unconfigured() -> None:
    with pytest.raises(cfg.APIBaseURLUnset):
        cfg.api_base_url()


# --- resolution precedence -------------------------------------------------


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("PRBE_API_BASE_URL", "https://env.example/")
    assert cfg.api_base_url() == "https://env.example"  # trailing slash trimmed


def test_persisted_value_used_without_env() -> None:
    cfg.persist_api_base_url("https://api.knowledge.prbe.ai")
    assert cfg.api_base_url() == "https://api.knowledge.prbe.ai"


def test_env_beats_persisted(monkeypatch) -> None:
    cfg.persist_api_base_url("https://persisted.example")
    monkeypatch.setenv("PRBE_API_BASE_URL", "https://env.example")
    assert cfg.api_base_url() == "https://env.example"


def test_persist_merges_not_clobbers() -> None:
    """Writing the host must not wipe cadence knobs already in .config."""
    cfg.config_file().write_text(json.dumps({"active_interval_seconds": 42}))
    cfg.persist_api_base_url("https://api.knowledge.prbe.ai")
    data = json.loads(cfg.config_file().read_text())
    assert data["active_interval_seconds"] == 42
    assert data["api_base_url"] == "https://api.knowledge.prbe.ai"


# --- derivation from the pairing token -------------------------------------


def test_derives_https_from_iss_hostname() -> None:
    token = _make_jwt({"iss": "api.knowledge.prbe.ai", "aud": "agent-tap"})
    assert cfg.base_url_from_pairing_token(token) == "https://api.knowledge.prbe.ai"


def test_accepts_apex_and_subdomains_and_strips_whitespace() -> None:
    for iss, expected in [
        ("prbe.ai", "https://prbe.ai"),
        ("api.prbe.ai", "https://api.prbe.ai"),
        ("api.knowledge.prbe.ai", "https://api.knowledge.prbe.ai"),
        ("https://api.knowledge.prbe.ai/", "https://api.knowledge.prbe.ai"),
        ("  api.knowledge.prbe.ai  ", "https://api.knowledge.prbe.ai"),
    ]:
        assert cfg.base_url_from_pairing_token(_make_jwt({"iss": iss})) == expected


def test_missing_iss_raises() -> None:
    token = _make_jwt({"aud": "agent-tap"})  # no iss
    with pytest.raises(ValueError):
        cfg.base_url_from_pairing_token(token)


def test_non_jwt_token_raises() -> None:
    with pytest.raises(ValueError):
        cfg.base_url_from_pairing_token("not-a-jwt")


# --- security: a forged/unsigned `iss` cannot redirect the upload host -------


def test_rejects_non_probe_and_unsafe_hosts() -> None:
    """The token is unsigned from our side, so `iss` must be constrained to an
    https Probe host. Each of these would otherwise pin an attacker host as the
    transcript/bearer upload target."""
    hostile = [
        "evil.com",                       # not a Probe host
        "http://api.prbe.ai",             # TLS downgrade
        "http://evil.com",                # downgrade + foreign host
        "https://api.prbe.ai@evil.com",   # userinfo confusion → real host evil.com
        "https://evil.com",               # foreign host, valid https
        "https://notprbe.ai",             # suffix without the dot boundary
        "https://prbe.ai.evil.com",       # lookalike subdomain
        "//evil.com",                     # scheme-relative → empty host
        "ftp://api.prbe.ai",              # wrong scheme
    ]
    for iss in hostile:
        with pytest.raises(ValueError):
            cfg.base_url_from_pairing_token(_make_jwt({"iss": iss}))


def test_pair_base_url_prefers_env_over_token(monkeypatch) -> None:
    monkeypatch.setenv("PRBE_API_BASE_URL", "https://override.example")
    token = _make_jwt({"iss": "api.knowledge.prbe.ai"})
    assert cfg.pair_base_url(token) == "https://override.example"


# --- end-to-end pairing (no env override — the real production path) ---------


def test_pair_derives_validates_and_persists_host(monkeypatch) -> None:
    """No env override: host comes from the token's iss, the pair POST targets
    that host, and it's persisted so the daemon reaches the same backend."""
    from tap import httpclient
    from tap.pair import run

    posted: list[str] = []

    def fake_post(url, body, *, bearer=None, timeout=30.0):
        posted.append(url)
        return httpclient.Response(
            status=201,
            body=json.dumps(
                {"device_id": "d1", "device_token": "t1", "customer_id": "c1"}
            ).encode("utf-8"),
            classification=httpclient.Classification.SUCCESS,
        )

    monkeypatch.setattr("tap.pair.httpclient.post_json", fake_post)
    rc = run(_make_jwt({"iss": "api.knowledge.prbe.ai"}))

    assert rc == 0
    assert posted == ["https://api.knowledge.prbe.ai/agent-tap/pair"]
    assert cfg.api_base_url() == "https://api.knowledge.prbe.ai"


def test_pair_aborts_on_hostile_iss_without_posting(monkeypatch) -> None:
    """A forged host fails before any network call — no token leaves the box."""
    from tap.pair import run

    def boom(*_a, **_k):
        raise AssertionError("post_json must not be called for a rejected host")

    monkeypatch.setattr("tap.pair.httpclient.post_json", boom)
    assert run(_make_jwt({"iss": "https://evil.com"})) == 1


# --- status must not report a hostless-but-paired device as healthy ----------


def test_status_flags_paired_device_with_no_host(capsys) -> None:
    from tap.status import run
    from tap.storage import Storage

    storage = Storage(cfg.state_db_path())
    storage.set_meta("device_id", "d1")
    storage.close()

    rc = run()
    assert rc == 1
    assert "no backend host on record" in capsys.readouterr().out


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
