"""`python -m tap revoke` — revoke this device server-side and wipe local state.

Tolerant of network failures: local state is always wiped, even if the
server call fails. Uninstall must succeed offline.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

from tap import config as cfg
from tap import httpclient
from tap.storage import Storage

_META_KEYS_TO_WIPE = (
    "device_id",
    "customer_id",
    "paired_at",
    "last_heartbeat_at",
    "last_successful_post_at",
    "last_401_at",
)


def run() -> int:
    token = cfg.load_token()

    server_err = ""
    if token:
        url = cfg.api_base_url() + cfg.REVOKE_PATH
        resp = httpclient.post_json(url, json.dumps({}).encode("utf-8"), bearer=token)
        if resp.classification != httpclient.Classification.SUCCESS:
            server_err = resp.error or f"status {resp.status}"

    # Always wipe local state.
    with contextlib.suppress(FileNotFoundError):
        os.remove(cfg.token_file())

    storage = Storage(cfg.state_db_path())
    try:
        for k in _META_KEYS_TO_WIPE:
            storage.delete_meta(k)
        storage.clear_outbox()
    finally:
        storage.close()

    if server_err:
        print(f"server-side revoke failed (local state still wiped): {server_err}", file=sys.stderr)
        return 0
    print("Revoked. Local credentials and state cleared.")
    return 0


def main(_argv: list[str] | None = None) -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
