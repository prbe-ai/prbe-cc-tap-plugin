# prbe-cc-tap-plugin

A Claude Code plugin that ships per-session Claude Code transcripts to
Probe (`api.prbe.ai/webhooks/claude_code`) for ingestion. A Python port
of [prbe-agent-tap](https://github.com/prbe-ai/prbe-agent-tap), refactored
to run as a session-scoped daemon owned by Claude Code's SessionStart /
SessionEnd hooks instead of a launchd/systemd service.

Zero runtime dependencies (stdlib only); Python 3.11+.

## Install

The same install script that wires up the Probe MCP server also installs
this plugin and provisions the bearer token via the `pair` subcommand.

The manual path:

```bash
python -m tap pair <pairing-token>
```

This exchanges a one-shot pairing token for a long-lived bearer at
`~/.claude/plugins/prbe-cc-tap-plugin/.token`. Once paired, every Claude
Code session will spawn the daemon automatically through the SessionStart
hook.

## How it works

```
┌─ Claude Code session ───────────────────────────────────────────────┐
│                                                                      │
│  SessionStart hook ──► spawns tap daemon (detached, crash-loop)      │
│                              │                                       │
│                              ▼                                       │
│                       every sync_interval (default 5min):            │
│                       1. tail transcript JSONL (byte-offset cursor)  │
│                       2. validate each new line as JSON              │
│                       3. build batch body, enqueue to sqlite outbox  │
│                       4. drain outbox: POST /webhooks/claude_code    │
│                          - 2xx → mark success                        │
│                          - 401 → halt + clear outbox                 │
│                          - 4xx (poison) → drop                       │
│                          - else → exponential backoff retry          │
│                                                                      │
│  SessionEnd hook ──► SIGTERMs daemon, cleans up sentinel             │
└──────────────────────────────────────────────────────────────────────┘
```

## Config files

All under `~/.claude/plugins/prbe-cc-tap-plugin/` (override via
`PRBE_CC_TAP_PLUGIN_DIR`):

| File | Purpose |
|------|---------|
| `.token` | Bearer token (mode 0600). Provisioned by `pair`. |
| `.config` | JSON `{"sync_interval_seconds": 300}`. Default 300. |
| `.disabled` | Presence disables the daemon entirely. |
| `.disabled_paths` | Newline-separated cwd prefixes to skip. |
| `.no-auto-update` | Presence skips the SessionStart auto-update. |
| `state.db` | sqlite: file_offsets, outbox, meta. |
| `logs/<session_id>.log` | Per-session log file. |

## Configuration

Change the sync interval to 60 seconds:

```bash
echo '{"sync_interval_seconds": 60}' > ~/.claude/plugins/prbe-cc-tap-plugin/.config
```

Disable for one specific repo:

```bash
echo "/Users/me/private-repo" >> ~/.claude/plugins/prbe-cc-tap-plugin/.disabled_paths
```

Disable entirely:

```bash
touch ~/.claude/plugins/prbe-cc-tap-plugin/.disabled
```

## Updates

Each Claude Code session start runs `git fetch + git merge --ff-only` against
`origin/main` on `~/.claude/plugins/prbe-cc-tap-plugin/`. If the network is
down, the plugin diverges from origin, or anything else fails, the existing
on-disk code is used and the session is never blocked.

If a daemon is already running for this user when an update lands on disk,
its `tap/__init__.py` mtime check picks up the new code on the next tick and
exits cleanly so the wrapper respawns into it.

To force an update outside a session, just re-run the install one-liner —
it's idempotent. To pin a version (e.g. while iterating locally with a
worktree), block auto-update:

```bash
touch ~/.claude/plugins/prbe-cc-tap-plugin/.no-auto-update
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `PRBE_API_BASE_URL` | Override base URL (default `https://api.prbe.ai`). |
| `PRBE_CC_TAP_INTERVAL_SECONDS` | Override sync interval. |
| `PRBE_CC_TAP_PLUGIN_DIR` | Override plugin directory (for tests). |
| `PRBE_CC_TAP_TOKEN` | Override `.token` (for tests/dev). |

## Subcommands

```bash
python -m tap watch    # daemon (called by SessionStart)
python -m tap pair     # exchange pairing token for bearer
python -m tap status   # print local state
python -m tap revoke   # revoke device server-side + wipe local state
```

## File layout

```
prbe-cc-tap-plugin/
├── README.md
├── pyproject.toml
├── .claude-plugin/plugin.json
├── hooks/
│   ├── hooks.json          # SessionStart + SessionEnd
│   ├── session-start.sh    # spawn detached daemon, crash-loop wrapper
│   └── session-end.sh      # SIGTERM the wrapper, cleanup
└── tap/
    ├── __main__.py         # `python -m tap <subcommand>`
    ├── config.py
    ├── storage.py
    ├── transcript.py
    ├── httpclient.py
    ├── outbox.py
    ├── main.py             # daemon loop
    ├── pair.py
    ├── status.py
    └── revoke.py
```
