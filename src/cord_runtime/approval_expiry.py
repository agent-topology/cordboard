"""Board-level reminder/timeout/halt clock for waiting approvals (#15).

The board -- not the graph -- decides when a waiting (interrupted) Run has
gone unanswered long enough to remind or halt it; graph/entity approval
validation stays authoritative over what a response means (ARCHITECTURE.md
"Isolation, routing, and approval"). This module only tracks the clock: a
deadline pair (reminder, timeout) keyed by (Deployment, Thread) and the one
durable disposition -- "resumed" or "halted" -- that ends the wait, whichever
side reaches it first. It never fabricates a resume decision on the graph's
behalf; the timeout path only calls the platform's chosen public halt/
cancellation action (`cord_runtime.aegra_client.cancel`), injected by the
caller so this module needs no network or wall-clock dependency to test.

`now` is always supplied by the caller (an injected clock) rather than read
here, so reminder/timeout/halt can be exercised deterministically without
real waiting. Storage follows `cord_runtime.connections`'s atomic
single-JSON-file pattern. Identity composes with `cord_runtime.run_continuity`:
callers pass that module's logical `run_id`, not a raw API Run ID, so a
resume's fresh API Run ID never looks like a new pending execution here.

Waiting-Run discovery across Deployments, the authorized submission boundary,
and dedupe of concurrent/stale/repeated *responses* remain backlog per #14's
own blockers; this module assumes the caller already knows which
(Deployment, Thread) pair is waiting.
"""

import contextlib
import json
import os
from pathlib import Path
import tempfile

APPROVAL_EXPIRY_DIRNAME = ".cordboard"
APPROVAL_EXPIRY_FILENAME = "approval_expiry.json"

WAITING = "waiting"
REMINDER_DUE = "reminder_due"
TIMEOUT_DUE = "timeout_due"
RESOLVED = "resolved"

RESUMED = "resumed"
HALTED = "halted"


class ApprovalExpiryError(ValueError):
    """Invalid input or storage for the approval reminder/timeout clock."""


class UnknownWaitingApproval(ApprovalExpiryError):
    """No waiting approval has been recorded yet for this Deployment/Thread pair."""


def expiry_path(board_dir: Path) -> Path:
    return Path(board_dir) / APPROVAL_EXPIRY_DIRNAME / APPROVAL_EXPIRY_FILENAME


def load_approval_expiry(board_dir: Path) -> dict:
    path = expiry_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalExpiryError(f"approval expiry file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ApprovalExpiryError(f"approval expiry file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".approval-expiry-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _validate(deployment: str, thread_id: str, run_id: str) -> None:
    for name, value in (("deployment alias", deployment), ("thread ID", thread_id),
                        ("run ID", run_id)):
        if not isinstance(value, str) or not value.strip():
            raise ApprovalExpiryError(f"{name} must be a non-empty string")


def _require(board_dir: Path, deployment: str, thread_id: str) -> dict:
    data = load_approval_expiry(board_dir)
    try:
        return data[deployment][thread_id]
    except KeyError:
        raise UnknownWaitingApproval(f"no waiting approval recorded for {deployment}/{thread_id}") from None


def record_waiting(board_dir: Path, deployment: str, thread_id: str, run_id: str, *,
                    now: float, reminder_after: float, timeout_after: float) -> dict:
    """Start, or recover, a waiting approval's deadlines.

    The first call for a (Deployment, Thread) pair fixes ``waiting_since``,
    ``reminder_at``, and ``timeout_at`` from ``now``. Every later call for the
    same pair -- including one made after a process restart, with a different
    ``now`` -- returns the stored record unchanged: a restart never grants an
    already-waiting approval a fresh clock, and the pending execution's
    identity (``run_id``) is fixed at first recording too.
    """
    _validate(deployment, thread_id, run_id)
    if timeout_after < reminder_after:
        raise ApprovalExpiryError("timeout_after must not be shorter than reminder_after")
    data = load_approval_expiry(board_dir)
    threads = data.setdefault(deployment, {})
    record = threads.get(thread_id)
    if record is None:
        record = {
            "run_id": run_id,
            "waiting_since": now,
            "reminder_at": now + reminder_after,
            "timeout_at": now + timeout_after,
            "reminded": False,
            "disposition": None,
            "resolved_at": None,
        }
        threads[thread_id] = record
        _write_atomic(expiry_path(board_dir), data)
    return dict(record)


def evaluate(board_dir: Path, deployment: str, thread_id: str, *, now: float) -> str:
    """Return the current status without mutating stored state.

    One of ``WAITING``, ``REMINDER_DUE``, ``TIMEOUT_DUE``, or ``RESOLVED``
    (a disposition -- resumed or halted -- is already durably recorded).
    """
    record = _require(board_dir, deployment, thread_id)
    if record["disposition"] is not None:
        return RESOLVED
    if now >= record["timeout_at"]:
        return TIMEOUT_DUE
    if now >= record["reminder_at"]:
        return REMINDER_DUE
    return WAITING


def mark_reminded(board_dir: Path, deployment: str, thread_id: str, *, now: float) -> dict:
    """Record that the in-app reminder fired, without touching the deadline.

    Idempotent, and a no-op once the wait is already resolved; delivering the
    reminder itself (SMTP/Slack) is out of scope here (#15) -- this is only
    the durable "did we already remind" state a delivery executor can poll.
    """
    data = load_approval_expiry(board_dir)
    try:
        record = data[deployment][thread_id]
    except KeyError:
        raise UnknownWaitingApproval(f"no waiting approval recorded for {deployment}/{thread_id}") from None
    if not record["reminded"] and record["disposition"] is None:
        record["reminded"] = True
        record["reminded_at"] = now
        _write_atomic(expiry_path(board_dir), data)
    return dict(record)


def _dispose(board_dir: Path, deployment: str, thread_id: str, disposition: str, *, now: float) -> dict:
    data = load_approval_expiry(board_dir)
    try:
        record = data[deployment][thread_id]
    except KeyError:
        raise UnknownWaitingApproval(f"no waiting approval recorded for {deployment}/{thread_id}") from None
    if record["disposition"] is None:
        record["disposition"] = disposition
        record["resolved_at"] = now
        _write_atomic(expiry_path(board_dir), data)
    return dict(record)


def resolve_approved(board_dir: Path, deployment: str, thread_id: str, *, now: float) -> dict:
    """Record that the approval itself arrived, whichever side wins the race.

    First-writer-wins against a concurrent ``resolve_timed_out``: if a halt
    already landed, this returns that disposition unchanged instead of
    overwriting it, so exactly one durable disposition survives either way.
    """
    return _dispose(board_dir, deployment, thread_id, RESUMED, now=now)


def resolve_timed_out(board_dir: Path, deployment: str, thread_id: str, *, now: float, halt) -> dict:
    """Perform the chosen public halt/cancellation action and record it durably.

    Checks the stored disposition first: if the approval already resolved
    (e.g. a concurrent resume), ``halt`` is never called, so a Run that just
    got approved is never also cancelled and no graph-specific rejection is
    forged on its behalf. ``halt`` is a zero-argument callable performing the
    platform's one supported public action (e.g. `aegra_client.cancel`); a
    ``RuntimeError`` it raises (ambiguous transport, per that module's own
    contract) propagates unchanged and the wait is left undisposed -- safe to
    reconsider on the next clock tick instead of being silently recorded as
    halted when the cancellation's outcome is actually unknown.
    """
    record = _require(board_dir, deployment, thread_id)
    if record["disposition"] is not None:
        return dict(record)
    halt()
    return _dispose(board_dir, deployment, thread_id, HALTED, now=now)
