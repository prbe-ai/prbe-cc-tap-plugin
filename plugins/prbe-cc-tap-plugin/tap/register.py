"""`python -m tap register` / `unregister` — wire the plugin into Claude Code.

Cloning the plugin into ~/.claude/plugins/prbe-cc-tap-plugin/ is necessary
but not sufficient: Claude Code only loads plugins that are also recorded
in two files:

  ~/.claude/plugins/installed_plugins.json
      registry of installed plugins, keyed by `<plugin>@<marketplace>`,
      with installPath / version / installedAt fields.

  ~/.claude/settings.json
      `enabledPlugins` map (gates whether registered plugins actually load)
      and `extraKnownMarketplaces` (declares non-official marketplaces the
      registry can refer to).

`register()` writes both atomically and is fully idempotent — re-running
preserves any other plugins the user has installed and just refreshes our
own entries (commit SHA, lastUpdated). `unregister()` is the inverse.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tap import __version__
from tap import config as cfg

# Plugin identity inside Claude Code's registry.
PLUGIN_NAME = "prbe-cc-tap-plugin"
MARKETPLACE = "prbe-ai"
PLUGIN_KEY = f"{PLUGIN_NAME}@{MARKETPLACE}"

# Source declaration for extraKnownMarketplaces — tells CC where this
# marketplace lives if it ever needs to (re)resolve it.
MARKETPLACE_SOURCE = {
    "source": {
        "source": "github",
        "repo": "prbe-ai/prbe-cc-tap-plugin",
    }
}


def _claude_dir() -> Path:
    """Directory CC reads its config + plugins from. Override via CLAUDE_CONFIG_DIR
    is supported by some CC builds, fall back to ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def _registry_path() -> Path:
    return _claude_dir() / "plugins" / "installed_plugins.json"


def _settings_path() -> Path:
    return _claude_dir() / "settings.json"


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read JSON; if missing/empty/corrupt, return the default. We never
    want a malformed file the user hand-edited to crash the registration."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    text = text.strip()
    if not text:
        return default
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return default
    return data if isinstance(data, dict) else default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _git_commit_sha(plugin_dir: Path) -> str | None:
    """Best-effort capture of the plugin's current git HEAD. Returns None
    if the plugin dir isn't a git checkout (e.g. tarball install)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(plugin_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _now_iso() -> str:
    # Trailing "Z" matches what CC writes for the other plugin entries.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# Registry mutations (pure dict-in / dict-out — easy to test)
# ---------------------------------------------------------------------------


def _ensure_registry_entry(
    registry: dict[str, Any],
    *,
    install_path: str,
    version: str,
    sha: str | None,
    now: str,
) -> dict[str, Any]:
    """Insert or refresh our entry inside installed_plugins.json's `plugins`
    map. Preserves the registry's other top-level fields (e.g. `version`)
    and other plugins."""
    out = dict(registry)
    out.setdefault("version", 2)
    plugins = dict(out.get("plugins") or {})

    existing_list = plugins.get(PLUGIN_KEY) or []
    user_entries = [e for e in existing_list if e.get("scope") != "user"]
    new_entry: dict[str, Any] = {
        "scope": "user",
        "installPath": install_path,
        "version": version,
        "installedAt": now,
        "lastUpdated": now,
    }
    # Keep the original installedAt if we already had one — don't fabricate
    # a "fresh install" timestamp on every re-register.
    for e in existing_list:
        if e.get("scope") == "user" and "installedAt" in e:
            new_entry["installedAt"] = e["installedAt"]
            break
    if sha:
        new_entry["gitCommitSha"] = sha

    plugins[PLUGIN_KEY] = [new_entry, *user_entries]
    out["plugins"] = plugins
    return out


def _drop_registry_entry(registry: dict[str, Any]) -> dict[str, Any]:
    out = dict(registry)
    plugins = dict(out.get("plugins") or {})
    plugins.pop(PLUGIN_KEY, None)
    out["plugins"] = plugins
    return out


def _ensure_settings_enabled(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    enabled = dict(out.get("enabledPlugins") or {})
    enabled[PLUGIN_KEY] = True
    out["enabledPlugins"] = enabled

    marketplaces = dict(out.get("extraKnownMarketplaces") or {})
    if MARKETPLACE not in marketplaces:
        marketplaces[MARKETPLACE] = MARKETPLACE_SOURCE
        out["extraKnownMarketplaces"] = marketplaces
    return out


def _drop_settings_entries(settings: dict[str, Any]) -> dict[str, Any]:
    """Remove our enabledPlugins entry. Leave extraKnownMarketplaces alone —
    other plugin installs from the same marketplace would still need it,
    and pruning it during a single-plugin uninstall risks breaking them."""
    out = dict(settings)
    enabled = dict(out.get("enabledPlugins") or {})
    enabled.pop(PLUGIN_KEY, None)
    out["enabledPlugins"] = enabled
    return out


# ---------------------------------------------------------------------------
# Public CLI
# ---------------------------------------------------------------------------


def register() -> int:
    plugin_dir = cfg.plugin_dir()
    if not plugin_dir.is_dir():
        print(
            f"error: plugin directory not found at {plugin_dir} — "
            f"clone the plugin first (re-run install.sh).",
            file=sys.stderr,
        )
        return 1

    sha = _git_commit_sha(plugin_dir)
    now = _now_iso()

    registry_path = _registry_path()
    registry = _load_json(registry_path, {"version": 2, "plugins": {}})
    new_registry = _ensure_registry_entry(
        registry,
        install_path=str(plugin_dir.resolve()),
        version=__version__,
        sha=sha,
        now=now,
    )
    if new_registry != registry:
        _atomic_write_json(registry_path, new_registry)

    settings_path = _settings_path()
    settings = _load_json(settings_path, {})
    new_settings = _ensure_settings_enabled(settings)
    if new_settings != settings:
        _atomic_write_json(settings_path, new_settings)

    print(f"Registered {PLUGIN_KEY} (version {__version__}, sha {sha or 'unknown'}).")
    print("Hooks fire on the NEXT Claude Code session — restart CC to activate.")
    return 0


def unregister() -> int:
    registry_path = _registry_path()
    registry = _load_json(registry_path, {"version": 2, "plugins": {}})
    new_registry = _drop_registry_entry(registry)
    if new_registry != registry:
        _atomic_write_json(registry_path, new_registry)

    settings_path = _settings_path()
    settings = _load_json(settings_path, {})
    new_settings = _drop_settings_entries(settings)
    if new_settings != settings:
        _atomic_write_json(settings_path, new_settings)

    print(f"Unregistered {PLUGIN_KEY}. Plugin directory still on disk; remove manually if desired.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tap register")
    parser.add_argument(
        "--unregister",
        action="store_true",
        help="remove the plugin's entries from installed_plugins.json + settings.json",
    )
    args = parser.parse_args(argv)
    return unregister() if args.unregister else register()


if __name__ == "__main__":
    sys.exit(main())
