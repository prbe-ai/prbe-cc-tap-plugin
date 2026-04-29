#!/usr/bin/env bash
# Probe Claude Code tap plugin installer.
#
# Usage:
#   curl -fsSL https://api.prbe.ai/install/cc-tap-plugin | sh
#
# Installs (or updates in place) the plugin under
# ~/.claude/plugins/prbe-cc-tap-plugin/. Pairing is a separate step:
#
#   cd ~/.claude/plugins/prbe-cc-tap-plugin && python3 -m tap pair <token>
#
# Idempotent — safe to re-run to update the plugin.

set -euo pipefail

PLUGIN_DIR="${PRBE_CC_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/prbe-cc-tap-plugin}"
REPO_URL="${PRBE_CC_TAP_REPO_URL:-https://github.com/prbe-ai/prbe-cc-tap-plugin.git}"

err() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || err "$1 not found in PATH"; }

need git
need python3

# Python 3.11+ required (matches pyproject.toml).
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || err "python3 3.11+ required (have $(python3 --version 2>&1))"

mkdir -p "$(dirname "$PLUGIN_DIR")"

if [ -d "$PLUGIN_DIR/.git" ]; then
    printf 'updating %s\n' "$PLUGIN_DIR"
    git -C "$PLUGIN_DIR" fetch --quiet origin main
    git -C "$PLUGIN_DIR" reset --hard --quiet origin/main
elif [ -e "$PLUGIN_DIR" ]; then
    err "$PLUGIN_DIR exists but is not a git checkout — move or remove it first"
else
    printf 'cloning into %s\n' "$PLUGIN_DIR"
    git clone --quiet "$REPO_URL" "$PLUGIN_DIR"
fi

cat <<EOF

Installed.
  plugin:  $PLUGIN_DIR

Next: pair this device with your Probe workspace. Get a token from
https://dashboard.prbe.ai → Integrations → Claude Code, then run:

  cd $PLUGIN_DIR && python3 -m tap pair <token>

After pairing, open a new Claude Code session — the daemon starts
automatically via the SessionStart hook and ships transcripts to Probe.
EOF
