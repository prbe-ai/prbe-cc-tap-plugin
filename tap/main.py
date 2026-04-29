"""Daemon loop — `python -m tap watch ...`.

Spawned by hooks/session-start.sh. Owns its own lifecycle for the duration
of the Claude Code session: ticks every sync_interval_seconds, reads new
transcript content, batches + enqueues, then drains the outbox.

Exits cleanly on:
  - SIGTERM/SIGINT
  - shutdown sentinel /tmp/prbe-cc-tap-watcher-<sid>.shutdown
  - killswitch ~/.claude/plugins/prbe-cc-tap-plugin/.disabled
  - cwd matching .disabled_paths
  - 401 halt from the server
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from tap import config as cfg
from tap import outbox
from tap.outbox import HaltError
from tap.storage import FileOffset, Storage
from tap.transcript import read_new, validate_json

log = logging.getLogger("prbe-cc-tap")

# Drain budget per tick — keep ticking responsive even if many batches are due.
MAX_DRAIN_PER_TICK = 64

_shutdown_requested = False


def _install_signal_handlers() -> None:
    def _handler(_sig: int, _frame: object) -> None:
        global _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _shutdown_observed(c: cfg.WatchConfig) -> bool:
    return (
        _shutdown_requested
        or c.shutdown_sentinel.exists()
        or cfg.killswitch_active()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tap watch")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--plugin-root", required=True, type=Path)
    args = parser.parse_args(argv)

    log_dir = cfg.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_dir / f"{args.session_id}.log"),
                  logging.StreamHandler()],
    )
    _install_signal_handlers()

    if cfg.killswitch_active():
        log.info("killswitch active, exiting")
        return 0
    if cfg.cwd_disabled(args.cwd):
        log.info("cwd %s matched .disabled_paths, exiting", args.cwd)
        return 0

    token = cfg.load_token()
    if not token:
        log.info("no token at %s; run `python -m tap pair <token>` first", cfg.token_file())
        return 0

    config = cfg.WatchConfig(
        session_id=args.session_id,
        transcript_path=args.transcript,
        cwd=args.cwd,
        plugin_root=args.plugin_root,
        token=token,
        sync_interval_s=cfg.sync_interval_seconds(),
    )

    storage = Storage(cfg.state_db_path())

    if storage.get_meta("last_401_at"):
        log.warning("halted: last_401_at set — re-pair to resume")
        storage.close()
        return 1

    log.info(
        "tap starting session=%s transcript=%s cwd=%s interval=%ds",
        config.session_id, config.transcript_path, config.cwd, config.sync_interval_s,
    )
    try:
        return _run_loop(config, storage)
    finally:
        storage.close()
        log.info("tap exited")


def _run_loop(c: cfg.WatchConfig, storage: Storage) -> int:
    base_url = cfg.api_base_url()
    device_id = storage.get_meta("device_id")  # may be empty for legacy/test setups

    # Resume batch_seq from any rows still queued for this session, else 0.
    batch_seq = max(0, storage.max_batch_seq(c.session_id) + 1)

    missing_ticks = 0

    while not _shutdown_observed(c):
        try:
            read = _tick_read(c, storage)
        except FileNotFoundError:
            missing_ticks += 1
            log.warning("transcript missing (tick %d): %s", missing_ticks, c.transcript_path)
            if missing_ticks >= 5:
                log.warning("transcript missing for %d ticks, exiting", missing_ticks)
                return 0
            read = None
        else:
            missing_ticks = 0

        if read is not None:
            new_lines, line_no_base, commit_offset = read
            committed = False
            if new_lines:
                now = int(time.time())
                body = outbox.build_batch_body(
                    device_id=device_id,
                    session_id=c.session_id,
                    batch_seq=batch_seq,
                    cwd=str(c.cwd),
                    base_line_no=line_no_base,
                    lines=new_lines,
                )
                try:
                    outbox.enqueue(
                        storage=storage,
                        session_id=c.session_id,
                        batch_seq=batch_seq,
                        cwd=str(c.cwd),
                        body=body,
                        now=now,
                    )
                    batch_seq += 1
                    commit_offset()
                    committed = True
                except Exception:
                    # Offset was NOT advanced; same lines are re-read next tick.
                    log.exception("enqueue failed; lines will be re-read next tick")
            if not committed and not new_lines:
                # No lines this tick — still refresh last_seen_at + inode/size.
                commit_offset()

        # Drain a bounded number of rows.
        try:
            drained = 0
            while drained < MAX_DRAIN_PER_TICK and outbox.drain_once(
                storage=storage, token=c.token, base_url=base_url,
                session_id=c.session_id,
            ):
                drained += 1
        except HaltError as e:
            log.error("halt: %s", e)
            return 1
        except Exception:
            log.exception("drain raised; will retry next tick")

        # Sleep in 1s slices so SIGTERM/sentinel/killswitch are responsive.
        slept = 0
        while slept < c.sync_interval_s and not _shutdown_observed(c):
            time.sleep(1)
            slept += 1

    return 0


def _tick_read(
    c: cfg.WatchConfig, storage: Storage
) -> tuple[list[bytes], int, Callable[[], None]]:
    """Read new lines from the transcript and validate; do NOT persist offset.

    Returns (validated_lines, base_line_no_for_first_line, commit_fn). The
    caller invokes commit_fn once it has successfully enqueued a batch (or
    decided to commit even with no new lines). Until then, the cursor stays
    where it was so a failed enqueue re-reads the same bytes next tick.
    """
    path_str = str(c.transcript_path)
    prev = storage.get_offset(path_str)
    prev_byte = prev.byte_offset if prev else 0
    last_line_no = prev.last_line_no if prev else 0

    res = read_new(c.transcript_path, prev_byte)

    valid: list[bytes] = []
    invalid_count = 0
    for line in res.lines:
        if validate_json(line):
            valid.append(line)
        else:
            invalid_count += 1

    base_line_no = last_line_no
    new_last_line_no = last_line_no + len(res.lines)

    if invalid_count:
        log.warning("dropped %d malformed JSON lines this tick", invalid_count)

    def commit() -> None:
        storage.upsert_offset(FileOffset(
            path=path_str,
            session_id=c.session_id,
            cwd=str(c.cwd),
            last_line_no=new_last_line_no,
            last_seen_at=int(time.time()),
            inode=res.inode,
            size=res.file_size,
            byte_offset=res.new_byte_offset,
        ))

    return valid, base_line_no, commit


if __name__ == "__main__":
    sys.exit(main())
