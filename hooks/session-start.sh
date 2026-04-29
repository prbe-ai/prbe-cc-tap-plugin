#!/usr/bin/env bash
# SessionStart hook for prbe-cc-tap-plugin.
#
# Reads {session_id, transcript_path, cwd} from stdin and spawns the tap
# daemon detached, wrapped in a crash-recovery loop. Wrapper PID is recorded
# in /tmp/prbe-cc-tap-watcher-<sid>.pid for SessionEnd cleanup.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PLUGIN_DIR="${PRBE_CC_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/prbe-cc-tap-plugin}"
LOG_DIR="$PLUGIN_DIR/logs"
mkdir -p "$LOG_DIR"

HOOK_INPUT="$(cat)"
SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")
TRANSCRIPT_PATH=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null || echo "")
CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys,os; print(json.load(sys.stdin).get("cwd") or os.getcwd())' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ] || [ -z "$TRANSCRIPT_PATH" ]; then
    printf '{"continue": true}\n'
    exit 0
fi

LOG_FILE="${LOG_DIR}/${SESSION_ID}.log"

# Killswitch: presence of .disabled disables the daemon entirely.
if [ -f "$PLUGIN_DIR/.disabled" ]; then
    echo "[$(date -u +%FT%TZ)] killswitch active, skipping" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

# Without a token there's nothing to authenticate with. Surface once and no-op.
if [ ! -f "$PLUGIN_DIR/.token" ] && [ -z "${PRBE_CC_TAP_TOKEN:-}" ]; then
    echo "[$(date -u +%FT%TZ)] no token at $PLUGIN_DIR/.token; run 'python -m tap pair <token>' first" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

# Auto-update plugin code from origin/main before each session.
#
# Best-effort: any failure (offline, diverged tree, force-push that breaks
# ff-only, $PLUGIN_ROOT not a git checkout) falls through and we spawn the
# existing on-disk code. Updating must NEVER block CC session start.
#
# Skipped when $PLUGIN_DIR/.no-auto-update exists — escape hatch for users
# who pin a version or develop locally with a worktree they don't want
# clobbered.
#
# Update happens before the pidfile check on purpose: even if a daemon is
# already running for this session_id (resume), we want the on-disk code to
# reflect origin so the daemon's own mtime-detection picks it up next tick.
if [ ! -f "$PLUGIN_DIR/.no-auto-update" ] && [ -d "$PLUGIN_ROOT/.git" ]; then
    if git -C "$PLUGIN_ROOT" fetch --quiet origin main 2>>"$LOG_FILE" \
       && git -C "$PLUGIN_ROOT" merge --ff-only --quiet FETCH_HEAD 2>>"$LOG_FILE"; then
        NEW_HEAD=$(git -C "$PLUGIN_ROOT" rev-parse --short HEAD 2>/dev/null || echo "?")
        echo "[$(date -u +%FT%TZ)] auto-update: synced to origin/main ($NEW_HEAD)" >>"$LOG_FILE"
    else
        echo "[$(date -u +%FT%TZ)] auto-update: skipped (fetch/merge failed; using on-disk code)" >>"$LOG_FILE"
    fi
fi

PID_FILE="/tmp/prbe-cc-tap-watcher-${SESSION_ID}.pid"

# If a daemon is already running for this session_id (e.g. resumed session),
# don't spawn another.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    printf '{"continue": true}\n'
    exit 0
fi

# Resolve Python interpreter — prefer plugin-local venv.
PY="$PLUGIN_ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "[$(date -u +%FT%TZ)] no python3 found, daemon disabled" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

# Crash-recovery wrapper: respawn up to 5 times per minute.
# Self-terminates when shutdown sentinel exists (SessionEnd touches it).
WRAPPER_SCRIPT='
SID="$1"; TP="$2"; CWD="$3"; PY="$4"; ROOT="$5"; LOG="$6"
SHUTDOWN="/tmp/prbe-cc-tap-watcher-${SID}.shutdown"
RESTART_COUNT=0
WINDOW_START=$(date +%s)
while true; do
    [ -f "$SHUTDOWN" ] && exit 0
    NOW=$(date +%s)
    if [ $((NOW - WINDOW_START)) -ge 60 ]; then
        WINDOW_START=$NOW
        RESTART_COUNT=0
    fi
    if [ "$RESTART_COUNT" -ge 5 ]; then
        echo "[$(date -u +%FT%TZ)] tap: too many restarts in 1min, giving up" >>"$LOG"
        exit 1
    fi
    "$PY" -m tap watch --session-id "$SID" --transcript "$TP" --cwd "$CWD" --plugin-root "$ROOT" >>"$LOG" 2>&1 || true
    [ -f "$SHUTDOWN" ] && exit 0
    RESTART_COUNT=$((RESTART_COUNT + 1))
    sleep 5
done
'

PYTHONPATH="$PLUGIN_ROOT" \
    setsid /bin/bash -c "$WRAPPER_SCRIPT" wrapper \
    "$SESSION_ID" "$TRANSCRIPT_PATH" "$CWD" "$PY" "$PLUGIN_ROOT" "$LOG_FILE" \
    </dev/null >>"$LOG_FILE" 2>&1 &
WRAPPER_PID=$!
echo "$WRAPPER_PID" >"$PID_FILE"

printf '{"continue": true}\n'
