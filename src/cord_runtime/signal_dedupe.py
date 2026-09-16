"""Durable Signal-ID deduplication for the router's submission step (#17).

``claim`` is called once per Signal, immediately before the router submits a
Run through `cord_runtime.aegra_client.execute`. A claim is written *before*
that submission attempt and is never rolled back, regardless of how the
attempt turns out: `aegra_client.execute`'s own contract already warns that a
transport failure is ambiguous -- the Run may still have been accepted -- so
treating "the call raised" as "the Signal was never submitted" would let a
retried delivery create a second Run. The same durability covers a process
restart between the claim and the outcome: the claim is on disk, not in
memory, so a redelivered Signal after a crash still reads as a duplicate.

A Signal that never reaches submission (unmatched, an invalid mapping, or an
unreachable managed Deployment) must not be claimed here -- nothing was
submitted, so a corrected redelivery should be free to try again. Callers
enforce that ordering; this module only tracks the claims it is given.

Retention is a plain age window, pruned lazily on each write so the store
does not grow without bound. Storage follows `cord_runtime.connections`'s
atomic single-JSON-file pattern.
"""

import contextlib
import json
import os
from pathlib import Path
import tempfile

DEDUPE_DIRNAME = ".cordboard"
DEDUPE_FILENAME = "signal_dedupe.json"

DEFAULT_RETENTION_SECONDS = 24 * 60 * 60  # 24 hours (preserved per issue #17's proposed default)


class SignalDedupeError(ValueError):
    """Invalid input or storage for the Signal-ID dedupe claim store."""


def dedupe_path(board_dir: Path) -> Path:
    return Path(board_dir) / DEDUPE_DIRNAME / DEDUPE_FILENAME


def load_signal_dedupe(board_dir: Path) -> dict:
    path = dedupe_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SignalDedupeError(f"signal dedupe file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise SignalDedupeError(f"signal dedupe file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".signal-dedupe-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _prune(data: dict, *, now: float, retention: float) -> dict:
    return {sid: rec for sid, rec in data.items() if now - rec["claimed_at"] < retention}


def is_duplicate(board_dir: Path, signal_id: str, *, now: float,
                  retention: float = DEFAULT_RETENTION_SECONDS) -> bool:
    """Return whether ``signal_id`` already has a live claim, without claiming it."""
    if not isinstance(signal_id, str) or not signal_id.strip():
        raise SignalDedupeError("signal id must be a non-empty string")
    data = _prune(load_signal_dedupe(board_dir), now=now, retention=retention)
    return signal_id in data


def claim(board_dir: Path, signal_id: str, *, now: float,
           retention: float = DEFAULT_RETENTION_SECONDS) -> bool:
    """Claim ``signal_id`` for submission. Returns ``True`` the first time within
    the retention window (the caller should proceed), ``False`` on every
    later call for the same id while that claim is still live (the caller
    must treat this delivery as a duplicate and skip submission).
    """
    if not isinstance(signal_id, str) or not signal_id.strip():
        raise SignalDedupeError("signal id must be a non-empty string")
    data = _prune(load_signal_dedupe(board_dir), now=now, retention=retention)
    if signal_id in data:
        _write_atomic(dedupe_path(board_dir), data)
        return False
    data[signal_id] = {"claimed_at": now}
    _write_atomic(dedupe_path(board_dir), data)
    return True
