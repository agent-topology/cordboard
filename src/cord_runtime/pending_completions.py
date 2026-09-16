"""Durable tracking for a Run whose completion outlives its submitting call (#50).

`aegra_client.execute`/`resume` synchronously wait up to their own
``timeout``; a Run still queued/running when that budget elapses
(``waiting_reason == "deadline"``), or one an ``interrupt()`` paused, is not
resolved inside that call. This module is the durable record connecting such
a Run back to whichever (Assistant, Subject) concurrency claim and managed
Deployment activity claim `router.route_signal` may still be holding on its
behalf, plus the identity (``connection``, ``assistant``, ``subject``,
``cascade_depth``) the #18 cascade needs once the Run actually reaches
terminal state -- so a completion observed later (`router.sweep_pending`
re-polling a still-running invocation, or `approval_inbox.submit_response`
resolving a resumed one) still starts exactly one matching child with the
correct causing Run id, the same as an immediate synchronous success.

Keyed by (deployment, thread_id): only one submission -- the original
``execute()`` or the latest ``resume()`` -- is ever outstanding on a Thread
at a time, so one entry is enough to track it. Storage follows
`cord_runtime.connections`'s atomic single-JSON-file pattern; unlike
`cord_runtime.concurrency`/`cord_runtime.response_dedupe`, no ``flock`` guards
these updates -- a lost update between two truly concurrent sweeps just means
an entry is not resolved until the next sweep, never a duplicate submission
or a double-released claim, since every mutation here is idempotent.
"""

import contextlib
import json
import os
from pathlib import Path
import tempfile

PENDING_DIRNAME = ".cordboard"
PENDING_FILENAME = "pending_completions.json"


class PendingCompletionError(ValueError):
    """Invalid input or storage for the pending-completion tracking store."""


def pending_path(board_dir: Path) -> Path:
    return Path(board_dir) / PENDING_DIRNAME / PENDING_FILENAME


def load_pending(board_dir: Path) -> dict:
    path = pending_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PendingCompletionError(f"pending completions file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise PendingCompletionError(f"pending completions file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".pending-completions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _key(deployment: str, thread_id: str) -> str:
    for name, value in (("deployment alias", deployment), ("thread ID", thread_id)):
        if not isinstance(value, str) or not value.strip():
            raise PendingCompletionError(f"{name} must be a non-empty string")
    # A JSON-encoded list, matching `concurrency._key`'s pattern: no delimiter
    # choice can collide with either field's own content.
    return json.dumps([deployment, thread_id])


def record_pending(board_dir: Path, deployment: str, thread_id: str, invocation_id: str, *,
                    assistant: str, subject: str, cascade_depth: int,
                    concurrency_held: bool, deployment_held: bool, now: float) -> None:
    """Start, or overwrite, one Thread's outstanding-invocation record.

    ``assistant``/``subject``/``cascade_depth`` are fixed by the *first* call
    for a (deployment, thread_id) pair -- overwritten only if a caller
    explicitly passes new values, since a resume never changes which Rule or
    Subject originally caused this Thread's Run. ``invocation_id`` and the
    ``*_held`` claim flags always take this call's values: they describe the
    Thread's *current* outstanding invocation.
    """
    for name, value in (("deployment alias", deployment), ("thread ID", thread_id),
                        ("invocation ID", invocation_id), ("assistant", assistant), ("subject", subject)):
        if not isinstance(value, str) or not value.strip():
            raise PendingCompletionError(f"{name} must be a non-empty string")
    if not isinstance(cascade_depth, int) or isinstance(cascade_depth, bool) or cascade_depth < 0:
        raise PendingCompletionError("cascade_depth must be a non-negative integer")
    data = load_pending(board_dir)
    key = _key(deployment, thread_id)
    record = {
        "deployment": deployment, "thread_id": thread_id, "invocation_id": invocation_id,
        "assistant": assistant, "subject": subject, "cascade_depth": cascade_depth,
        "concurrency_held": bool(concurrency_held), "deployment_held": bool(deployment_held),
        "recorded_at": now,
    }
    data[key] = record
    _write_atomic(pending_path(board_dir), data)


def update_pending(board_dir: Path, deployment: str, thread_id: str, *, invocation_id: str | None = None,
                    concurrency_held: bool | None = None, deployment_held: bool | None = None,
                    now: float) -> dict | None:
    """Update an existing entry's current invocation id and/or held claims.

    A no-op returning ``None`` if no entry is tracked for this (deployment,
    thread_id) pair -- there is nothing to transition.
    """
    data = load_pending(board_dir)
    key = _key(deployment, thread_id)
    record = data.get(key)
    if record is None:
        return None
    if invocation_id is not None:
        record["invocation_id"] = invocation_id
    if concurrency_held is not None:
        record["concurrency_held"] = bool(concurrency_held)
    if deployment_held is not None:
        record["deployment_held"] = bool(deployment_held)
    record["recorded_at"] = now
    _write_atomic(pending_path(board_dir), data)
    return dict(record)


def get_pending(board_dir: Path, deployment: str, thread_id: str) -> dict | None:
    """Return the tracked entry for (deployment, thread_id), or ``None``."""
    return load_pending(board_dir).get(_key(deployment, thread_id))


def resolve_pending(board_dir: Path, deployment: str, thread_id: str) -> dict | None:
    """Remove and return one Thread's entry once its Run reached genuine
    terminal state and every claim it held has been released. A no-op
    returning ``None`` if nothing was tracked (safe to call from more than
    one concurrent finalizer for the same Thread)."""
    data = load_pending(board_dir)
    key = _key(deployment, thread_id)
    record = data.pop(key, None)
    if record is not None:
        _write_atomic(pending_path(board_dir), data)
    return record
