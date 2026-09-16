"""Cross-process claim and outcome store for one interrupt's response (#44).

`approval_expiry.py`'s own docstring documents its first-writer-wins
disposition as a *single-process, sequential-ordering* guarantee, not
cross-process file locking, and names true concurrent-process dedupe of
responses as backlog per #14's own blockers. This module is that dedupe: a
durable, ``flock``-guarded claim per ``(deployment, thread_id, interrupt_id)``
-- one specific runtime interrupt, never a whole Thread -- taken before a
resume submission is ever attempted, and exactly one of three durable
outcomes recorded after: ``RESUMED``, ``REJECTED`` (the entity boundary
declined it), or ``UNKNOWN`` (an ambiguous transport failure -- never
retried automatically here, safe to reconsider deliberately).

Storage and locking follow `cord_runtime.concurrency`'s exact pattern:
``_locked`` serializes the whole check-and-set with an OS file lock spanning
the read and the write, since a bare read-then-write lets two real
concurrent responses both win (#19's lesson, reapplied here for responses
rather than Signal submissions).
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

RESPONSE_DEDUPE_DIRNAME = ".cordboard"
RESPONSE_DEDUPE_FILENAME = "response_dedupe.json"
_LOCK_FILENAME = RESPONSE_DEDUPE_FILENAME + ".lock"

RESUMED = "resumed"
REJECTED = "rejected"
UNKNOWN = "unknown"

_OUTCOMES = frozenset({RESUMED, REJECTED, UNKNOWN})


class ResponseDedupeError(ValueError):
    """Invalid input or storage for the per-interrupt response claim store."""


def response_dedupe_path(board_dir: Path) -> Path:
    return Path(board_dir) / RESPONSE_DEDUPE_DIRNAME / RESPONSE_DEDUPE_FILENAME


def load_response_dedupe(board_dir: Path) -> dict:
    path = response_dedupe_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResponseDedupeError(f"response dedupe file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ResponseDedupeError(f"response dedupe file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".response-dedupe-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


@contextlib.contextmanager
def _locked(board_dir: Path):
    """Hold an exclusive OS file lock across one check-and-set (see #19's
    lesson, reapplied in `cord_runtime.concurrency._locked`)."""
    lock_path = Path(board_dir) / RESPONSE_DEDUPE_DIRNAME / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _key(deployment: str, thread_id: str, interrupt_id: str) -> str:
    for name, value in (("deployment alias", deployment), ("thread ID", thread_id),
                        ("interrupt ID", interrupt_id)):
        if not isinstance(value, str) or not value.strip():
            raise ResponseDedupeError(f"{name} must be a non-empty string")
    # A JSON-encoded list rather than a joined string: no delimiter choice
    # can collide with any field's own content (`concurrency._key`'s pattern).
    return json.dumps([deployment, thread_id, interrupt_id])


def claim(board_dir: Path, deployment: str, thread_id: str, interrupt_id: str, *, now: float) -> bool:
    """Claim one interrupt's response slot before attempting resume.

    Returns ``True`` the first time (the caller may proceed to authorize and
    submit); ``False`` on every later call for the same interrupt while a
    claim is already recorded, regardless of that claim's eventual outcome --
    the caller must treat this as a duplicate submission and not resubmit.
    """
    key = _key(deployment, thread_id, interrupt_id)
    with _locked(board_dir):
        data = load_response_dedupe(board_dir)
        if key in data:
            return False
        data[key] = {"claimed_at": now, "outcome": None, "resolved_at": None}
        _write_atomic(response_dedupe_path(board_dir), data)
        return True


def record_outcome(board_dir: Path, deployment: str, thread_id: str, interrupt_id: str,
                    outcome: str, *, now: float) -> dict:
    """Durably record one claimed interrupt's outcome.

    First-writer-wins, mirroring `approval_expiry._dispose`: once an outcome
    is recorded it is never overwritten, so a duplicate/racing call for the
    same interrupt returns the original recorded outcome unchanged.
    ``UNKNOWN`` is a real, durable outcome here -- it marks the response as
    unresolved pending deliberate reconsideration, not as safely retryable.
    """
    if outcome not in _OUTCOMES:
        raise ResponseDedupeError(f"outcome must be one of {sorted(_OUTCOMES)}")
    key = _key(deployment, thread_id, interrupt_id)
    with _locked(board_dir):
        data = load_response_dedupe(board_dir)
        record = data.get(key)
        if record is None:
            raise ResponseDedupeError(f"no claimed response for {deployment}/{thread_id}/{interrupt_id}")
        if record["outcome"] is None:
            record["outcome"] = outcome
            record["resolved_at"] = now
            _write_atomic(response_dedupe_path(board_dir), data)
        return dict(record)


def get_claim(board_dir: Path, deployment: str, thread_id: str, interrupt_id: str) -> dict | None:
    """Return the claim record for one interrupt, or ``None`` if unclaimed."""
    key = _key(deployment, thread_id, interrupt_id)
    with _locked(board_dir):
        return load_response_dedupe(board_dir).get(key)
