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

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_NAME = "prbe-cc-tap-plugin"

DEFAULT_API_BASE_URL = "https://api.prbe.ai"
DEFAULT_ACTIVE_INTERVAL_SECONDS = 60
DEFAULT_IDLE_INTERVAL_SECONDS = 300

WEBHOOK_PATH = "/webhooks/claude_code"
PAIR_PATH = "/agent-tap/pair"
REVOKE_PATH = "/agent-tap/revoke"


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


def api_base_url() -> str:
    return os.environ.get("PRBE_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")


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
