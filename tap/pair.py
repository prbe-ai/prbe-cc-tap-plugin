"""`python -m tap pair <pairing-token>` — exchange pairing token for a bearer.

POSTs to ${API}/agent-tap/pair with {pairing_token, os, hostname}. On
Success the response carries {device_id, device_token, customer_id}; we
write the bearer to ${PLUGIN_DIR}/.token (mode 0600) and persist
device_id/customer_id/paired_at to meta. Any prior last_401_at is cleared.
"""

from __future__ import annotations

import json
import platform
import socket
import sys
import time

from tap import config as cfg
from tap import httpclient
from tap.storage import Storage


def _os_label() -> str:
    p = platform.system().lower()
    if p == "darwin":
        return "macos"
    return p


def run(pairing_token: str) -> int:
    if not pairing_token:
        print("error: pairing token required", file=sys.stderr)
        return 2

    body = json.dumps({
        "pairing_token": pairing_token,
        "os": _os_label(),
        "hostname": socket.gethostname(),
    }).encode("utf-8")

    url = cfg.api_base_url() + cfg.PAIR_PATH
    resp = httpclient.post_json(url, body)

    if resp.classification == httpclient.Classification.HALT:
        print(
            "pairing token rejected by server (request a fresh one from the dashboard)",
            file=sys.stderr,
        )
        return 1
    if resp.classification != httpclient.Classification.SUCCESS:
        msg = resp.error or f"status {resp.status}"
        print(f"pair failed: {msg}", file=sys.stderr)
        return 1

    data = httpclient.parse_json(resp)
    device_id = data.get("device_id", "")
    device_token = data.get("device_token", "")
    customer_id = data.get("customer_id", "")
    if not device_id or not device_token:
        print("pair response missing device_id or device_token", file=sys.stderr)
        return 1

    cfg.write_token(device_token)

    storage = Storage(cfg.state_db_path())
    try:
        storage.set_meta("device_id", device_id)
        storage.set_meta("customer_id", customer_id)
        storage.set_meta("paired_at", str(int(time.time())))
        storage.delete_meta("last_401_at")
    finally:
        storage.close()

    print(f"Paired. device_id={device_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    if len(argv) != 1:
        print("usage: python -m tap pair <pairing-token>", file=sys.stderr)
        return 2
    return run(argv[0])


if __name__ == "__main__":
    sys.exit(main())
