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
  - transcript file missing for 5 ticks (file deleted / session torn down)
  - orphan session detected (no process holds the transcript open) —
    happens when CC is hard-killed (SIGKILL / OS reboot / force-quit) and
    SessionEnd never fires; touches the shutdown sentinel so the wrapper
    exits too instead of respawning a doomed daemon
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
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

# Run the orphan-session check (lsof on transcript) every N ticks. At the
# default 5min sync_interval, 12 ticks ≈ 1 hour. lsof is a subprocess and
# we don't need fast detection — orphans only matter for tidy cleanup.
ORPHAN_CHECK_EVERY_TICKS = 12

# Hard cap on how long we'll wait for lsof to return; if it hangs, we'd
# rather assume "alive" and skip than block the tick.
ORPHAN_LSOF_TIMEOUT_S = 5

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


def _transcript_has_active_reader(path: Path) -> bool | None:
    """True/False if lsof can determine; None if lsof is unavailable.

    `lsof -t -- <path>` lists PIDs that hold an open fd on `path`. The daemon
    itself opens the transcript only briefly inside _tick_read, so when this
    function runs (after the tick's read+enqueue completed) the daemon's own
    fd is closed and won't show up. CC keeps the transcript fd open for the
    session's lifetime, so an empty result means CC is dead.

    Returning None (lsof not installed, weird container, timeout) is treated
    by the caller as "can't tell, assume alive" — we never orphan-exit on
    ambiguous signal.
    """
    try:
        result = subprocess.run(
            ["lsof", "-t", "--", str(path)],
            capture_output=True,
            timeout=ORPHAN_LSOF_TIMEOUT_S,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return bool(result.stdout.strip())


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
    tick_count = 0

    # Track whether we ever saw a process holding the transcript fd. Without
    # this gate, an early lsof miss (e.g. before CC has fully opened the file)
    # would orphan-exit a healthy daemon. We only treat "no reader" as orphan
    # if we previously observed a reader.
    seen_active_reader = False

    while not _shutdown_observed(c):
        tick_count += 1

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

        # Orphan-session detection. CC keeps the transcript fd open for the
        # session's lifetime; if no process holds it, the session is gone.
        # Only trips after we've previously observed a reader, so a startup
        # race or a system without lsof can't false-positive us into exit.
        if tick_count % ORPHAN_CHECK_EVERY_TICKS == 0:
            has_reader = _transcript_has_active_reader(c.transcript_path)
            if has_reader is True:
                seen_active_reader = True
            elif has_reader is False and seen_active_reader:
                log.info(
                    "no process holds %s open; CC session ended without SessionEnd, exiting",
                    c.transcript_path,
                )
                # Touch the sentinel so the wrapper exits instead of respawning
                # us into the same dead-session state.
                try:
                    c.shutdown_sentinel.touch()
                except OSError:
                    pass
                return 0

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
