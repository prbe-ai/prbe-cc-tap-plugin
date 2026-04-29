"""`python -m tap status` — print local daemon state."""

from __future__ import annotations

import sys
import time

from tap import config as cfg
from tap.storage import Storage


def _relative(unix_str: str) -> str:
    if not unix_str:
        return "never"
    try:
        n = int(unix_str)
    except ValueError:
        return unix_str
    delta = max(0, int(time.time()) - n)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} minutes ago"
    if delta < 86400:
        return f"{delta // 3600} hours ago"
    return f"{delta // 86400} days ago"


def run() -> int:
    storage = Storage(cfg.state_db_path())
    try:
        device_id = storage.get_meta("device_id")
        if not device_id:
            print("prbe-cc-tap: not paired — run `python -m tap pair <token>`")
            return 1
        last_401 = storage.get_meta("last_401_at")
        if last_401:
            print(f"prbe-cc-tap: halted (token revoked at {_relative(last_401)})")
            print("  Run `python -m tap pair <token>` with a fresh token to resume.")
            return 1
        print("prbe-cc-tap: paired")
        print(f"  device:        {device_id}")
        print(f"  customer:      {storage.get_meta('customer_id')}")
        print(f"  paired:        {_relative(storage.get_meta('paired_at'))}")
        print(f"  last shipped:  {_relative(storage.get_meta('last_successful_post_at'))}")
        print(f"  outbox:        {storage.outbox_row_count()} rows, {storage.outbox_byte_size()} bytes")
        print(f"  interval:      {cfg.sync_interval_seconds()}s")
        return 0
    finally:
        storage.close()


def main(_argv: list[str] | None = None) -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
