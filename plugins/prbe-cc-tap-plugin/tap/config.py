"""Plugin configuration: paths, env, sync interval, killswitch.

All paths derive from PRBE_CC_TAP_PLUGIN_DIR (env override) or
~/.claude/plugins/prbe-cc-tap-plugin/ so the install script and the
daemon agree without coordination.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "prbe-cc-tap-plugin"

DEFAULT_API_BASE_URL = "https://api.prbe.ai"
DEFAULT_SYNC_INTERVAL_SECONDS = 300

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


def sync_interval_seconds() -> int:
    """Resolve sync interval from env > .config > default."""
    env = os.environ.get("PRBE_CC_TAP_INTERVAL_SECONDS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    cfg = config_file()
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            n = int(data.get("sync_interval_seconds", DEFAULT_SYNC_INTERVAL_SECONDS))
            if n > 0:
                return n
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return DEFAULT_SYNC_INTERVAL_SECONDS


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
    sync_interval_s: int

    @property
    def shutdown_sentinel(self) -> Path:
        return shutdown_sentinel(self.session_id)
