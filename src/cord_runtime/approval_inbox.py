"""Waiting-Run discovery and authorized resume across Deployments (#44).

Closes the residual scope #14/#15/#28 left as backlog: no existing module
scanned every registered Deployment for pending runtime interrupts, and
neither `run_continuity` nor `approval_expiry` keyed anything by *which*
interrupt on a Thread was being answered -- both assumed a caller already
knew the single (Deployment, Thread) pair to act on. This module is the
platform inbox: it enumerates waiting interrupts (`discover_waiting`) and
routes one operator response through the entity's authorization boundary,
the response-dedupe claim, and `backend.resume`, in that order
(`submit_response`), recording exactly one durable disposition per
interrupt.

`langgraph.types.Interrupt` (pinned ``langgraph==1.2.11``) carries a
deterministic ``.id`` per task namespace, and a Thread's public ``/state``
already exposes a tuple of tasks each with a tuple of interrupts -- so two
concurrently pending interrupts on one Thread are already distinguishable
through Aegra's existing public API. This module is what actually reads
that shape; it is not a schema change to Aegra or LangGraph.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cord_runtime import response_dedupe, run_continuity
from cord_runtime.approval_expiry import UnknownWaitingApproval, record_waiting, resolve_approved
from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.backends.base import RuntimeStatus
from cord_runtime.connections import load_connections
from cord_runtime.entity_auth import AuthSubmission, EntityAuthBoundary


class ApprovalInboxError(ValueError):
    """Invalid input, e.g. a submission naming an unregistered Deployment."""


@dataclass(frozen=True)
class WaitingInterrupt:
    """One pending runtime interrupt, fully identified for display or
    response -- never a business approval/plan identifier from the graph's
    own payload (#14's own distinction, preserved here)."""

    deployment: str
    assistant: str
    subject: str | None
    logical_run_id: str
    thread_id: str
    interrupt_id: str
    value: Any
    revision: str | None


@dataclass(frozen=True)
class SubmissionResult:
    """``status`` is one of ``"duplicate"``, ``"rejected"``, ``"resumed"``, or
    ``"unknown"`` -- the last is durable and deliberately not an exception,
    since an ambiguous transport outcome is a real, recorded state, not a
    failure to raise."""

    status: str
    reason: str | None = None


def _revision_of(state: dict) -> str | None:
    checkpoint = state.get("checkpoint") or {}
    return checkpoint.get("checkpoint_id") or state.get("checkpoint_id")


def _interrupts_of(state: dict) -> list[dict]:
    found = []
    for task in state.get("tasks") or []:
        for interrupt in task.get("interrupts") or []:
            found.append(interrupt)
    return found


def discover_waiting(board_dir: Path, *, reminder_after: float, timeout_after: float,
                      now: float, backend_factory=AegraExecutionBackend) -> list[WaitingInterrupt]:
    """Scan every registered Deployment for pending runtime interrupts.

    Only Threads `run_continuity` already knows about are scanned -- a
    Thread this board never submitted or resumed cannot be this board's to
    discover. Each still-interrupted Thread's deadlines are started (or
    recovered, restart-safe) through `approval_expiry.record_waiting` exactly
    as before #44; per-interrupt claim/response state is tracked separately
    (`response_dedupe`) so answering one interrupt never touches another
    pending on the same Thread.

    A Deployment or Thread that raises a transport ``RuntimeError`` while
    being probed is skipped, never raised through to the caller -- one
    unreachable Deployment must not hide every other Deployment's waiting
    interrupts.
    """
    waiting: list[WaitingInterrupt] = []
    connections = load_connections(board_dir)
    continuity = run_continuity.load_run_continuity(board_dir)
    for deployment, threads in continuity.items():
        connection = connections.get(deployment)
        if connection is None:
            continue
        backend = backend_factory(connection["endpoint"], board_dir=board_dir, deployment=deployment)
        for thread_id, record in threads.items():
            latest_invocation = record["api_run_ids"][-1]
            try:
                status = backend.status(thread_id, latest_invocation)
                if status is not RuntimeStatus.INTERRUPTED:
                    continue
                run_info = backend.get_run(thread_id, latest_invocation)
                state = backend.get_state(thread_id)
            except RuntimeError:
                continue
            interrupts = _interrupts_of(state)
            if not interrupts:
                continue
            record_waiting(board_dir, deployment, thread_id, record["run_id"],
                            now=now, reminder_after=reminder_after, timeout_after=timeout_after)
            revision = _revision_of(state)
            for interrupt in interrupts:
                waiting.append(WaitingInterrupt(
                    deployment=deployment, assistant=run_info.get("assistant_id"),
                    subject=run_info.get("subject"), logical_run_id=record["run_id"],
                    thread_id=thread_id, interrupt_id=interrupt["id"], value=interrupt.get("value"),
                    revision=revision,
                ))
    return waiting


def submit_response(board_dir: Path, *, deployment: str, thread_id: str, interrupt_id: str,
                     approver: str, response_value: Any, revision: str | None,
                     auth_boundary: EntityAuthBoundary, now: float,
                     backend_factory=AegraExecutionBackend) -> SubmissionResult:
    """Route one operator's answer to the exact interrupt it targets.

    Order matters and each step is durable before the next begins:

    1. `response_dedupe.claim` -- a duplicate/racing submission for the same
       interrupt stops here, before authorization or transport.
    2. `auth_boundary.authorize` -- rejected, stale, or revision-mismatched
       submissions never reach ``backend.resume``. A UI-supplied ``approver``
       is only ever a claim passed to the boundary, never treated as
       authority on its own.
    3. ``backend.resume`` -- the one call that can create a new invocation;
       its ambiguous-transport failure is recorded as ``UNKNOWN``, never
       retried automatically.
    """
    connection = load_connections(board_dir).get(deployment)
    if connection is None:
        raise ApprovalInboxError(f"unknown deployment alias '{deployment}'")
    if not response_dedupe.claim(board_dir, deployment, thread_id, interrupt_id, now=now):
        return SubmissionResult("duplicate", "a response to this interrupt was already claimed")

    backend = backend_factory(connection["endpoint"], board_dir=board_dir, deployment=deployment)
    try:
        state = backend.get_state(thread_id)
    except RuntimeError:
        response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                        response_dedupe.UNKNOWN, now=now)
        return SubmissionResult("unknown", "could not read current Thread state")

    still_pending = interrupt_id in {i["id"] for i in _interrupts_of(state)}
    current_revision = _revision_of(state)
    assistant = ""
    if still_pending:
        try:
            record = run_continuity.logical_run(board_dir, deployment, thread_id)
            run_info = backend.get_run(thread_id, record["api_run_ids"][-1])
            assistant = run_info.get("assistant_id") or ""
        except (RuntimeError, run_continuity.UnknownThread):
            assistant = ""

    submission = AuthSubmission(
        deployment=deployment, assistant=assistant, thread_id=thread_id, interrupt_id=interrupt_id,
        approver=approver, response_value=response_value, revision=revision,
    )
    decision = auth_boundary.authorize(submission, current_revision=current_revision, still_pending=still_pending)
    if not decision.accepted:
        response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                        response_dedupe.REJECTED, now=now)
        return SubmissionResult("rejected", decision.reason)

    try:
        backend.resume(thread_id, assistant, response_value)
    except RuntimeError:
        response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                        response_dedupe.UNKNOWN, now=now)
        return SubmissionResult("unknown", "resume submission had an ambiguous transport outcome")

    response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                    response_dedupe.RESUMED, now=now)
    try:
        resolve_approved(board_dir, deployment, thread_id, now=now)
    except UnknownWaitingApproval:
        pass  # no approval_expiry clock was ever started for this Thread; resume still succeeded.
    return SubmissionResult("resumed")
