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
#
# Why a SIGTERM trap that forwards to the python child: macOS doesn't ship
# `setsid` so we can't put the wrapper + daemon in their own process group
# and rely on `kill -TERM -<pgid>` to take down both at once. Instead we
# detach via `nohup ... & disown` (POSIX-portable) and have the wrapper
# bash forward SIGTERM/SIGINT explicitly to the python child it spawns.
WRAPPER_SCRIPT='
SID="$1"; TP="$2"; CWD="$3"; PY="$4"; ROOT="$5"; LOG="$6"
SHUTDOWN="/tmp/prbe-cc-tap-watcher-${SID}.shutdown"
RESTART_COUNT=0
WINDOW_START=$(date +%s)
CHILD_PID=""
trap '\''[ -n "$CHILD_PID" ] && kill -TERM "$CHILD_PID" 2>/dev/null; exit 0'\'' TERM INT
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
    "$PY" -m tap watch --session-id "$SID" --transcript "$TP" --cwd "$CWD" --plugin-root "$ROOT" >>"$LOG" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
    [ -f "$SHUTDOWN" ] && exit 0
    RESTART_COUNT=$((RESTART_COUNT + 1))
    sleep 5
done
'

# Detach the wrapper. nohup ignores SIGHUP so it survives CC's exit; `&`
# backgrounds it; `disown` removes it from this shell's job table so the
# parent (this hook) can exit cleanly without reaping it. On Linux this is
# equivalent to setsid (just without the new process group); on macOS it's
# the only portable option since setsid isn't installed by default.
PYTHONPATH="$PLUGIN_ROOT" \
    nohup /bin/bash -c "$WRAPPER_SCRIPT" wrapper \
    "$SESSION_ID" "$TRANSCRIPT_PATH" "$CWD" "$PY" "$PLUGIN_ROOT" "$LOG_FILE" \
    </dev/null >>"$LOG_FILE" 2>&1 &
WRAPPER_PID=$!
disown
echo "$WRAPPER_PID" >"$PID_FILE"

printf '{"continue": true}\n'
