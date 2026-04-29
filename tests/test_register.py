"""Tests for the Claude Code plugin registration logic.

The pure dict-in/dict-out helpers (`_ensure_registry_entry`,
`_drop_registry_entry`, `_ensure_settings_enabled`,
`_drop_settings_entries`) carry the interesting logic. We assert idempotency,
preservation of unrelated entries, and round-trip register/unregister.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_claude_dir(monkeypatch, tmp_path: Path):
    """Each test gets its own pretend ~/.claude so we never touch the user's
    real Claude Code config."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("PRBE_CC_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    (tmp_path / "plugin").mkdir()
    yield claude


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_ensure_registry_entry_inserts_into_empty_registry() -> None:
    from tap.register import PLUGIN_KEY, _ensure_registry_entry

    out = _ensure_registry_entry(
        {"version": 2, "plugins": {}},
        install_path="/tmp/p",
        version="0.1.0",
        sha="abc123",
        now="2026-04-29T00:00:00.000Z",
    )
    assert out["version"] == 2
    entries = out["plugins"][PLUGIN_KEY]
    assert len(entries) == 1
    e = entries[0]
    assert e["scope"] == "user"
    assert e["installPath"] == "/tmp/p"
    assert e["version"] == "0.1.0"
    assert e["gitCommitSha"] == "abc123"
    assert e["installedAt"] == "2026-04-29T00:00:00.000Z"
    assert e["lastUpdated"] == "2026-04-29T00:00:00.000Z"


def test_ensure_registry_entry_preserves_unrelated_plugins() -> None:
    from tap.register import PLUGIN_KEY, _ensure_registry_entry

    seed = {
        "version": 2,
        "plugins": {
            "vercel@claude-plugins-official": [{"scope": "user", "version": "0.40.0"}],
            "warp@claude-code-warp": [{"scope": "user", "version": "2.0.0"}],
        },
    }
    out = _ensure_registry_entry(
        seed, install_path="/tmp/p", version="0.1.0", sha=None,
        now="2026-04-29T00:00:00.000Z",
    )
    assert out["plugins"]["vercel@claude-plugins-official"] == [
        {"scope": "user", "version": "0.40.0"}
    ]
    assert out["plugins"]["warp@claude-code-warp"] == [
        {"scope": "user", "version": "2.0.0"}
    ]
    assert PLUGIN_KEY in out["plugins"]


def test_ensure_registry_entry_keeps_original_installed_at_on_re_register() -> None:
    """Re-registering should not lie about install time — keep the first
    installedAt and only bump lastUpdated."""
    from tap.register import PLUGIN_KEY, _ensure_registry_entry

    original = "2026-01-01T00:00:00.000Z"
    seed = {
        "version": 2,
        "plugins": {
            PLUGIN_KEY: [
                {
                    "scope": "user",
                    "installPath": "/old",
                    "version": "0.0.9",
                    "installedAt": original,
                    "lastUpdated": original,
                    "gitCommitSha": "old",
                }
            ]
        },
    }
    out = _ensure_registry_entry(
        seed, install_path="/new", version="0.1.0", sha="new",
        now="2026-04-29T00:00:00.000Z",
    )
    e = out["plugins"][PLUGIN_KEY][0]
    assert e["installedAt"] == original  # preserved
    assert e["lastUpdated"] == "2026-04-29T00:00:00.000Z"  # bumped
    assert e["installPath"] == "/new"
    assert e["gitCommitSha"] == "new"


def test_ensure_registry_entry_is_idempotent() -> None:
    """Two consecutive calls with the same args produce identical output —
    re-running register doesn't keep growing the registry."""
    from tap.register import _ensure_registry_entry

    seed = {"version": 2, "plugins": {}}
    once = _ensure_registry_entry(
        seed, install_path="/tmp/p", version="0.1.0", sha="abc",
        now="2026-04-29T00:00:00.000Z",
    )
    twice = _ensure_registry_entry(
        once, install_path="/tmp/p", version="0.1.0", sha="abc",
        now="2026-04-29T00:00:00.000Z",
    )
    assert once == twice


def test_drop_registry_entry_removes_only_our_key() -> None:
    from tap.register import PLUGIN_KEY, _drop_registry_entry

    seed = {
        "version": 2,
        "plugins": {
            PLUGIN_KEY: [{"scope": "user"}],
            "warp@claude-code-warp": [{"scope": "user"}],
        },
    }
    out = _drop_registry_entry(seed)
    assert PLUGIN_KEY not in out["plugins"]
    assert "warp@claude-code-warp" in out["plugins"]


def test_ensure_settings_enabled_adds_plugin_and_marketplace() -> None:
    from tap.register import (
        MARKETPLACE,
        MARKETPLACE_SOURCE,
        PLUGIN_KEY,
        _ensure_settings_enabled,
    )

    out = _ensure_settings_enabled({})
    assert out["enabledPlugins"][PLUGIN_KEY] is True
    assert out["extraKnownMarketplaces"][MARKETPLACE] == MARKETPLACE_SOURCE


def test_ensure_settings_enabled_preserves_existing_keys() -> None:
    from tap.register import PLUGIN_KEY, _ensure_settings_enabled

    seed = {
        "alwaysThinkingEnabled": True,
        "enabledPlugins": {
            "warp@claude-code-warp": True,
            "vercel@claude-plugins-official": True,
        },
        "extraKnownMarketplaces": {
            "claude-code-warp": {"source": {"source": "github", "repo": "x/y"}},
        },
    }
    out = _ensure_settings_enabled(seed)
    assert out["alwaysThinkingEnabled"] is True
    assert out["enabledPlugins"]["warp@claude-code-warp"] is True
    assert out["enabledPlugins"]["vercel@claude-plugins-official"] is True
    assert out["enabledPlugins"][PLUGIN_KEY] is True
    assert out["extraKnownMarketplaces"]["claude-code-warp"] == {
        "source": {"source": "github", "repo": "x/y"}
    }


def test_ensure_settings_enabled_is_idempotent() -> None:
    from tap.register import _ensure_settings_enabled

    once = _ensure_settings_enabled({})
    twice = _ensure_settings_enabled(once)
    assert once == twice


def test_drop_settings_removes_enabled_only_keeps_marketplace() -> None:
    """Other plugins from the same marketplace might still rely on the
    extraKnownMarketplaces entry, so unregister must not touch it."""
    from tap.register import (
        MARKETPLACE,
        PLUGIN_KEY,
        _drop_settings_entries,
        _ensure_settings_enabled,
    )

    seeded = _ensure_settings_enabled({})
    dropped = _drop_settings_entries(seeded)
    assert PLUGIN_KEY not in dropped["enabledPlugins"]
    assert MARKETPLACE in dropped["extraKnownMarketplaces"]


def test_load_json_returns_default_on_missing_file(tmp_path: Path) -> None:
    from tap.register import _load_json

    assert _load_json(tmp_path / "nope.json", {"x": 1}) == {"x": 1}


def test_load_json_returns_default_on_corrupt_file(tmp_path: Path) -> None:
    from tap.register import _load_json

    p = tmp_path / "broken.json"
    p.write_text("{ not valid json")
    assert _load_json(p, {"x": 1}) == {"x": 1}


def test_load_json_returns_default_on_non_dict(tmp_path: Path) -> None:
    """Defensive: a hand-edited file containing `[]` or `null` shouldn't
    crash the registration."""
    from tap.register import _load_json

    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]")
    assert _load_json(p, {"x": 1}) == {"x": 1}


# ---------------------------------------------------------------------------
# Top-level register() / unregister() round-trip
# ---------------------------------------------------------------------------


def test_register_writes_both_files_atomically(_isolated_claude_dir: Path) -> None:
    from tap import config as cfg
    from tap.register import (
        MARKETPLACE,
        PLUGIN_KEY,
        _registry_path,
        _settings_path,
        register,
    )

    plugin_dir = cfg.plugin_dir()
    # Pretend we're a real git checkout for the SHA capture.
    with mock.patch("tap.register._git_commit_sha", return_value="deadbeef"):
        rc = register()
    assert rc == 0

    registry = json.loads(_registry_path().read_text())
    settings = json.loads(_settings_path().read_text())

    assert registry["version"] == 2
    entries = registry["plugins"][PLUGIN_KEY]
    assert len(entries) == 1
    assert entries[0]["installPath"] == str(plugin_dir.resolve())
    assert entries[0]["gitCommitSha"] == "deadbeef"

    assert settings["enabledPlugins"][PLUGIN_KEY] is True
    assert MARKETPLACE in settings["extraKnownMarketplaces"]


def test_register_then_unregister_round_trip(_isolated_claude_dir: Path) -> None:
    from tap.register import (
        PLUGIN_KEY,
        _registry_path,
        _settings_path,
        register,
        unregister,
    )

    with mock.patch("tap.register._git_commit_sha", return_value="abc"):
        register()
    unregister()

    registry = json.loads(_registry_path().read_text())
    settings = json.loads(_settings_path().read_text())

    assert PLUGIN_KEY not in registry.get("plugins", {})
    assert PLUGIN_KEY not in settings.get("enabledPlugins", {})


def test_register_preserves_other_plugins_in_user_files(
    _isolated_claude_dir: Path, tmp_path: Path
) -> None:
    """Seed installed_plugins.json + settings.json with other plugins,
    register, and verify we left them alone."""
    from tap.register import (
        PLUGIN_KEY,
        _registry_path,
        _settings_path,
        register,
    )

    _registry_path().parent.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "vercel@claude-plugins-official": [
                        {"scope": "user", "version": "0.40.0"}
                    ],
                },
            }
        )
    )
    _settings_path().write_text(
        json.dumps(
            {
                "alwaysThinkingEnabled": True,
                "enabledPlugins": {"vercel@claude-plugins-official": True},
            }
        )
    )

    with mock.patch("tap.register._git_commit_sha", return_value="abc"):
        register()

    registry = json.loads(_registry_path().read_text())
    settings = json.loads(_settings_path().read_text())

    # Pre-existing plugin survives.
    assert "vercel@claude-plugins-official" in registry["plugins"]
    assert settings["enabledPlugins"]["vercel@claude-plugins-official"] is True
    # Custom user setting survives.
    assert settings["alwaysThinkingEnabled"] is True
    # Ours is added.
    assert PLUGIN_KEY in registry["plugins"]
    assert settings["enabledPlugins"][PLUGIN_KEY] is True


def test_register_errors_when_plugin_dir_missing(
    _isolated_claude_dir: Path, monkeypatch, tmp_path: Path, capsys
) -> None:
    """If the plugin code isn't actually on disk, register() must fail
    loudly — registering a path that doesn't exist would just silently
    break the next CC session."""
    from tap.register import register

    monkeypatch.setenv("PRBE_CC_TAP_PLUGIN_DIR", str(tmp_path / "definitely-missing"))
    rc = register()
    assert rc == 1
    err = capsys.readouterr().err
    assert "plugin directory not found" in err


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
