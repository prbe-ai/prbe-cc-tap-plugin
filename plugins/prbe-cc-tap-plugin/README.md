# prbe-cc-tap-plugin

A Claude Code plugin that ships per-session Claude Code transcripts to
Probe (`api.prbe.ai/webhooks/claude_code`) for ingestion. Runs as a
session-scoped daemon spawned by CC's `SessionStart` hook and torn down
on `SessionEnd`.

Zero runtime dependencies (stdlib only); Python 3.11+.

## Install

This plugin is published through the `prbe-ai` marketplace. From inside
Claude Code:

```
/plugin marketplace add prbe-ai/prbe-cc-tap-plugin
/plugin install prbe-cc-tap-plugin@prbe-ai
```

Then pair this laptop with your Probe workspace from a terminal. Pick the
highest-version dir under the cache — Claude Code keeps older versions
side-by-side, so a bare `*/` glob errors with `cd: too many arguments`
as soon as you've upgraded once:

```bash
cd "$(ls -1d ~/.claude/plugins/cache/prbe-ai/prbe-cc-tap-plugin/*/ | sort -V | tail -1)" && \
  python3 -m tap pair <pairing-token>
```

(`sort -V` is portable on macOS and Linux and orders `0.2.10` after
`0.2.9` correctly. `pair` writes its state to the stable
`~/.claude/plugins/prbe-cc-tap-plugin/` dir, so any installed version
is fine — newest is just the safe default.)

Get a pairing token from **https://dashboard.prbe.ai → Integrations → Claude Code**.

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

## State files

State lives at `~/.claude/plugins/prbe-cc-tap-plugin/` (override via
`PRBE_CC_TAP_PLUGIN_DIR`) — separate from the plugin code, which CC manages
under `~/.claude/plugins/cache/prbe-ai/prbe-cc-tap-plugin/<version>/`.
Keeping state at a stable path means version bumps don't require re-pairing.

| File | Purpose |
|------|---------|
| `.token` | Bearer token (mode 0600). Provisioned by `pair`. |
| `.config` | JSON for cadence overrides — see below. |
| `.disabled` | Presence disables the daemon entirely. |
| `.disabled_paths` | Newline-separated cwd prefixes to skip. |
| `state.db` | sqlite: file_offsets, outbox, meta. |
| `logs/<session_id>.log` | Per-session log file. |

## Cadence

The daemon is adaptive by default:

- **Active mode (60s)** while the transcript is advancing
- **Idle mode (300s)** after two consecutive empty ticks (≈2 min of no
  new transcript content)

Active resumes the moment new lines appear. This keeps ingestion near
real-time during work without flooding the backend on idle CC sessions.

Override either side via `.config`:

```bash
# Tighter active cadence; same idle.
echo '{"active_interval_seconds": 30}' \
  > ~/.claude/plugins/prbe-cc-tap-plugin/.config

# Both knobs.
echo '{"active_interval_seconds": 30, "idle_interval_seconds": 600}' \
  > ~/.claude/plugins/prbe-cc-tap-plugin/.config
```

Or disable adaptive switching entirely with the legacy single knob — sets
both active and idle to the same value:

```bash
echo '{"sync_interval_seconds": 60}' \
  > ~/.claude/plugins/prbe-cc-tap-plugin/.config
```

## Other configuration

Disable for one specific repo:

```bash
echo "/Users/me/private-repo" >> ~/.claude/plugins/prbe-cc-tap-plugin/.disabled_paths
```

Disable entirely:

```bash
touch ~/.claude/plugins/prbe-cc-tap-plugin/.disabled
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `PRBE_API_BASE_URL` | Override base URL (default `https://api.prbe.ai`). |
| `PRBE_CC_TAP_ACTIVE_INTERVAL_SECONDS` | Override active interval. |
| `PRBE_CC_TAP_IDLE_INTERVAL_SECONDS` | Override idle interval. |
| `PRBE_CC_TAP_INTERVAL_SECONDS` | Legacy single-knob — applies to both. |
| `PRBE_CC_TAP_PLUGIN_DIR` | Override state directory (for tests). |
| `PRBE_CC_TAP_TOKEN` | Override `.token` (for tests/dev). |

## Subcommands

```bash
python -m tap watch    # daemon (called by SessionStart hook)
python -m tap pair     # exchange pairing token for bearer
python -m tap status   # print local state
python -m tap revoke   # revoke device server-side + wipe local state
```

## Re-pair behavior

Running `pair` on an already-paired laptop mints a new device on the server
and **automatically revokes the old one** after the new pairing succeeds.
A failed new pair leaves the old token on disk untouched, so you're never
stranded.

## Development

```bash
cd plugins/prbe-cc-tap-plugin
uv venv --python 3.13 .venv
.venv/bin/python -m pip install -e .
uv run --with pytest --with pytest-mock python -m pytest tests/ -v
```
