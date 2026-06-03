"""Plugin configuration: paths, env, sync intervals, killswitch.

All paths derive from PRBE_CC_TAP_PLUGIN_DIR (env override) or
~/.claude/plugins/prbe-cc-tap-plugin/ so the install script and the
daemon agree without coordination.

Cadence model: the daemon runs adaptively. While the transcript is
advancing it ticks at the active interval (default 60s); after two
consecutive empty ticks it slows to the idle interval (default 300s)
to reduce backend load on idle CC sessions. A single legacy knob
(sync_interval_seconds) overrides both — set it if you want a flat
cadence with no adaptive switching.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PLUGIN_NAME = "prbe-cc-tap-plugin"

DEFAULT_ACTIVE_INTERVAL_SECONDS = 60
DEFAULT_IDLE_INTERVAL_SECONDS = 300

WEBHOOK_PATH = "/webhooks/claude_code"
PAIR_PATH = "/agent-tap/pair"
REVOKE_PATH = "/agent-tap/revoke"

# Env override for the backend host. There is deliberately NO hardcoded
# fallback: the host is learned from the pairing token's `iss` claim at pair
# time and persisted to .config. A baked-in default is exactly what silently
# broke ingestion when the backend moved off api.prbe.ai — every tick kept
# hitting a dead host and failing with a cryptic DNS error instead of saying
# "not configured". See api_base_url() / base_url_from_pairing_token().
ENV_API_BASE_URL = "PRBE_API_BASE_URL"
CONFIG_API_BASE_URL_KEY = "api_base_url"

# The pairing token's `iss` is UNSIGNED from the plugin's side (we hold no
# key), so a pasted token could otherwise name any host as the upload target.
# Constrain the token-derived host to https + a Probe-owned domain. Probe mints
# pairing tokens with a `*.prbe.ai` issuer (api.knowledge.prbe.ai today,
# api.prbe.ai historically). Self-hosted/dev backends use the PRBE_API_BASE_URL
# env override instead, which is an explicit local choice and not gated here.
ALLOWED_HOST_EXACT = "prbe.ai"
ALLOWED_HOST_SUFFIX = ".prbe.ai"


def plugin_dir() -> Path:
    env = os.environ.get("PRBE_CC_TAP_PLUGIN_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "plugins" / PLUGIN_NAME


def token_file() -> Path:
    return plugin_dir() / ".token"


def config_file() -> Path:
    return plugin_dir() / ".config"


def disabled_file() -> Path:
    return plugin_dir() / ".disabled"


def disabled_paths_file() -> Path:
    return plugin_dir() / ".disabled_paths"


def state_db_path() -> Path:
    return plugin_dir() / "state.db"


def log_dir() -> Path:
    return plugin_dir() / "logs"


def shutdown_sentinel(session_id: str) -> Path:
    return Path("/tmp") / f"prbe-cc-tap-watcher-{session_id}.shutdown"


class APIBaseURLUnset(RuntimeError):
    """No backend host is configured.

    Raised instead of falling back to a hardcoded host. There is no baked-in
    default by design — the host comes from the pairing token's `iss` claim
    (persisted at pair time) or an explicit env override, and if neither is
    present we fail loudly rather than silently ship to a guessed URL.
    """


def _env_base_url() -> str | None:
    v = os.environ.get(ENV_API_BASE_URL, "").strip()
    return v.rstrip("/") if v else None


def api_base_url() -> str:
    """Resolve the backend base URL: env override > value persisted at pair.

    No hardcoded fallback — raises APIBaseURLUnset when unconfigured.
    """
    env = _env_base_url()
    if env:
        return env
    persisted = _read_config_dict().get(CONFIG_API_BASE_URL_KEY)
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip().rstrip("/")
    raise APIBaseURLUnset(
        "no backend host configured — pair this device with "
        "`python -m tap pair <token>` (the host is read from the token), "
        f"or set {ENV_API_BASE_URL}"
    )


def _jwt_claim(token: str, claim: str) -> str | None:
    """Best-effort read of a string claim from an unverified JWT payload.

    We don't hold the signing key, so we don't verify — the server verifies
    the signature when we POST /agent-tap/pair, and a forged host just makes
    pairing fail. Returns None for anything that isn't a well-formed JWT
    carrying a non-empty string `claim`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    val = payload.get(claim) if isinstance(payload, dict) else None
    return val if isinstance(val, str) and val.strip() else None


def base_url_from_pairing_token(pairing_token: str) -> str:
    """Derive and validate the backend base URL from a pairing JWT's `iss`.

    The dashboard mints the pairing token with `iss` set to the backend host
    (e.g. `api.knowledge.prbe.ai`). Reading it here lets the plugin follow the
    backend across domain moves with no hardcoded host and no plugin update.

    The token is UNSIGNED from our side, so `iss` is attacker-controllable when
    a user pastes a forged token. We require https + a Probe-owned host before
    it can become the upload target — otherwise a pasted token could pin an
    arbitrary host and harvest the device bearer and transcripts (the re-pair
    path would even POST the user's existing bearer to it). Self-hosted/dev
    backends use the PRBE_API_BASE_URL env override, which is not gated here.
    """
    iss = _jwt_claim(pairing_token, "iss")
    if not iss:
        raise ValueError(
            "pairing token carries no `iss` host claim; cannot determine the "
            "backend host (request a fresh token from the dashboard)"
        )
    url = iss.strip()
    if "://" not in url:
        url = "https://" + url
    url = url.rstrip("/")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or not (host == ALLOWED_HOST_EXACT or host.endswith(ALLOWED_HOST_SUFFIX))
    ):
        raise ValueError(
            f"pairing token `iss` ({iss!r}) is not an allowed Probe backend; "
            f"expected an https://*.{ALLOWED_HOST_EXACT} host. For a self-hosted "
            f"or dev backend, set {ENV_API_BASE_URL} instead"
        )
    return url


def pair_base_url(pairing_token: str) -> str:
    """Base URL for the pair request: env override > token `iss`."""
    return _env_base_url() or base_url_from_pairing_token(pairing_token)


def persist_api_base_url(url: str) -> None:
    """Persist the resolved base URL into .config so the daemon and revoke
    reach the same backend the pairing pinned (merging, not clobbering)."""
    data = _read_config_dict()
    data[CONFIG_API_BASE_URL_KEY] = url.rstrip("/")
    p = config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _parse_positive_int(value: Any) -> int | None:
    """Best-effort positive int. Returns None for missing / unparseable / <= 0."""
    if value is None:
        return None
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _read_config_dict() -> dict[str, Any]:
    p = config_file()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def intervals() -> tuple[int, int]:
    """Return (active_seconds, idle_seconds).

    Resolution order, per knob: env > .config > default.

    Legacy single-knob escape hatch: PRBE_CC_TAP_INTERVAL_SECONDS (env) or
    `sync_interval_seconds` (config) — if set, applies to BOTH active and
    idle. For users who want flat cadence with no adaptive switching.

    Idle is clamped to >= active so we never accidentally tick faster when
    the user thinks they've slowed us down.
    """
    config_data = _read_config_dict()

    # Legacy override path — flat cadence.
    legacy_env = _parse_positive_int(os.environ.get("PRBE_CC_TAP_INTERVAL_SECONDS"))
    if legacy_env is not None:
        return legacy_env, legacy_env
    legacy_cfg = _parse_positive_int(config_data.get("sync_interval_seconds"))
    if legacy_cfg is not None:
        return legacy_cfg, legacy_cfg

    # Adaptive path.
    active = (
        _parse_positive_int(os.environ.get("PRBE_CC_TAP_ACTIVE_INTERVAL_SECONDS"))
        or _parse_positive_int(config_data.get("active_interval_seconds"))
        or DEFAULT_ACTIVE_INTERVAL_SECONDS
    )
    idle = (
        _parse_positive_int(os.environ.get("PRBE_CC_TAP_IDLE_INTERVAL_SECONDS"))
        or _parse_positive_int(config_data.get("idle_interval_seconds"))
        or DEFAULT_IDLE_INTERVAL_SECONDS
    )
    if idle < active:
        idle = active
    return active, idle


def load_token() -> str | None:
    env = os.environ.get("PRBE_CC_TAP_TOKEN")
    if env:
        return env.strip() or None
    p = token_file()
    if p.is_file():
        try:
            t = p.read_text(encoding="utf-8").strip()
            return t or None
        except OSError:
            return None
    return None


def write_token(token: str) -> None:
    """Atomic write of the bearer token at mode 0600."""
    p = token_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def killswitch_active() -> bool:
    return disabled_file().exists()


def cwd_disabled(cwd: Path) -> bool:
    p = disabled_paths_file()
    if not p.is_file():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    cwd_str = str(cwd)
    for line in lines:
        prefix = line.strip()
        if prefix and cwd_str.startswith(prefix):
            return True
    return False


@dataclass(frozen=True)
class WatchConfig:
    session_id: str
    transcript_path: Path
    cwd: Path
    plugin_root: Path
    token: str
    active_interval_s: int
    idle_interval_s: int

    @property
    def shutdown_sentinel(self) -> Path:
        return shutdown_sentinel(self.session_id)
