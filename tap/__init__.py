"""prbe-cc-tap-plugin tap daemon.

Tails the active Claude Code transcript, batches new lines, and ships them
to api.prbe.ai/webhooks/claude_code. State and lifecycle are owned by
Claude Code's session hooks — daemon dies when the session ends.
"""

__version__ = "0.1.0"
