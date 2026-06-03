"""prbe-cc-tap-plugin tap daemon.

Tails the active Claude Code transcript, batches new lines, and ships them
to the Probe backend's /webhooks/claude_code endpoint. The backend host is
learned from the pairing token (no hardcoded host). State and lifecycle are
owned by Claude Code's session hooks — daemon dies when the session ends.
"""

__version__ = "0.2.6"
