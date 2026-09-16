"""Declared (Assistant, Subject) concurrency policy for the router (#17).

`cord_runtime.aegra_client.execute` blocks synchronously for up to its
``timeout`` while a Run is in flight, and each `cord` CLI invocation is its
own process, so an overlapping arrival for the same (Assistant, Subject) pair
is a second, separate process racing the first. The claim below is a durable
file lock for exactly that window, not an in-memory one: it has to survive
across processes, not just across threads.

The only declared conflict behavior implemented is "skip" (ARCHITECTURE.md
"Isolation, routing, and approval"): a second arrival for a pair that is
already claimed is turned away rather than queued, retried, or run anyway.
Other declared policies are not part of this slice.

A claim also carries the timestamp it was taken at so a crashed holder (the
process died between claiming and releasing, never reaching the `finally`)
does not lock a Subject out forever: a claim older than ``stale_after`` is
treated as abandoned and silently reclaimed by the next arrival. Callers pick
``stale_after`` to comfortably exceed the longest a legitimate hold can take
(the execution timeout plus any managed-Deployment startup budget).
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

CONCURRENCY_DIRNAME = ".cordboard"
CONCURRENCY_FILENAME = "concurrency.json"
_LOCK_FILENAME = CONCURRENCY_FILENAME + ".lock"


class ConcurrencyError(ValueError):
    """Invalid input or storage for the (Assistant, Subject) concurrency claim store."""


def concurrency_path(board_dir: Path) -> Path:
    return Path(board_dir) / CONCURRENCY_DIRNAME / CONCURRENCY_FILENAME


def load_concurrency(board_dir: Path) -> dict:
    path = concurrency_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConcurrencyError(f"concurrency file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ConcurrencyError(f"concurrency file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".concurrency-", suffix=".tmp")
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
    """Hold an exclusive OS file lock across one check-and-set.

    `load_concurrency` + `_write_atomic` alone is a read-then-write: two
    processes (or threads) can both read "unclaimed" before either writes,
    and both then return ``True`` for the same (Assistant, Subject) pair --
    silently breaking the "skip" guarantee this module exists to provide
    (#19). `flock` serializes the whole check-and-set across processes, not
    just within one interpreter, matching this module's own docstring.
    """
    lock_path = Path(board_dir) / CONCURRENCY_DIRNAME / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _key(assistant: str, subject: str) -> str:
    if not isinstance(assistant, str) or not assistant.strip():
        raise ConcurrencyError("assistant must be a non-empty string")
    if not isinstance(subject, str) or not subject.strip():
        raise ConcurrencyError("subject must be a non-empty string")
    # A JSON-encoded 2-element list rather than a joined string: no delimiter
    # choice can collide with either field's own content.
    return json.dumps([assistant, subject])


def try_claim(board_dir: Path, assistant: str, subject: str, *, now: float, stale_after: float) -> bool:
    """Claim the (Assistant, Subject) pair. Returns ``True`` when claimed (the
    caller may proceed), ``False`` when another live claim already holds it
    (the caller's declared "skip" behavior applies).
    """
    key = _key(assistant, subject)
    with _locked(board_dir):
        data = load_concurrency(board_dir)
        existing = data.get(key)
        if existing is not None and now - existing["claimed_at"] < stale_after:
            return False
        data[key] = {"claimed_at": now}
        _write_atomic(concurrency_path(board_dir), data)
        return True


def release(board_dir: Path, assistant: str, subject: str) -> None:
    """Release a held (Assistant, Subject) claim. A no-op if none is held."""
    key = _key(assistant, subject)
    with _locked(board_dir):
        data = load_concurrency(board_dir)
        if key in data:
            del data[key]
            _write_atomic(concurrency_path(board_dir), data)


def renew(board_dir: Path, assistant: str, subject: str, *, now: float) -> None:
    """Bump a held claim's ``claimed_at`` so it does not go stale while a Run
    genuinely still owns it (#50): a Run that outlives its submitting call's
    own ``timeout`` is re-observed by `router.sweep_pending`, not held by any
    single process for its whole lifetime, so ``stale_after``'s crash-recovery
    window would otherwise reclaim this Subject out from under a Run that is
    still actually running. A no-op if no claim is held (already released or
    reclaimed by another arrival) -- never fabricates one.
    """
    key = _key(assistant, subject)
    with _locked(board_dir):
        data = load_concurrency(board_dir)
        if key in data:
            data[key]["claimed_at"] = now
            _write_atomic(concurrency_path(board_dir), data)
