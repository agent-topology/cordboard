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

Managed resume and completion routing (#50): ``submit_response`` now calls
`cord_runtime.deployment_lifecycle.ensure_started` before ever resuming --
the same shared entry point `cord_runtime.router.route_signal` calls -- since
a managed Deployment that went idle while its Run sat interrupted must be
running again before a resume can reach it. Startup failure is returned
before the response-dedupe claim is ever taken, so a corrected retry is still
free to try again, mirroring the router's own ordering. Once resumed, the
outcome is read rather than discarded: genuine terminal success releases the
Deployment's activity claim and routes the same Run's own `run.finished`
Signal through `router.route_signal` (#18's cascade, #50 AC1/AC3); a Run
still queued/running past this call's own wait budget keeps the claim held
and its identity tracked in `cord_runtime.pending_completions` for
`router.sweep_pending` to finish observing later, exactly like the router's
own synchronous path; an interrupt again releases the claim so it can go
idle until the next resume restarts it (#50 AC2/AC5).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cord_runtime import pending_completions, response_dedupe, run_continuity
from cord_runtime.approval_expiry import UnknownWaitingApproval, record_waiting, resolve_approved
from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.backends.base import ClientWaitOutcome, RuntimeStatus
from cord_runtime.connections import load_connections
from cord_runtime.deployment_lifecycle import ensure_started, release_active
from cord_runtime.entity_auth import AuthSubmission, EntityAuthBoundary
from cord_runtime.router import route_signal
from cord_runtime.signals import run_finished_signal

_HEALTH_TIMEOUT = 30.0


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


def _discover_thread(board_dir: Path, deployment: str, connection: dict, thread_id: str, record: dict, *,
                      reminder_after: float, timeout_after: float, now: float,
                      backend_factory) -> list[WaitingInterrupt]:
    backend = backend_factory(connection["endpoint"], board_dir=board_dir, deployment=deployment)
    latest_invocation = record["api_run_ids"][-1]
    try:
        status = backend.status(thread_id, latest_invocation)
        if status is not RuntimeStatus.INTERRUPTED:
            return []
        run_info = backend.get_run(thread_id, latest_invocation)
        state = backend.get_state(thread_id)
    except RuntimeError:
        return []
    interrupts = _interrupts_of(state)
    if not interrupts:
        return []
    record_waiting(board_dir, deployment, thread_id, record["run_id"],
                    now=now, reminder_after=reminder_after, timeout_after=timeout_after)
    revision = _revision_of(state)
    return [WaitingInterrupt(
        deployment=deployment, assistant=run_info.get("assistant_id"),
        subject=run_info.get("subject"), logical_run_id=record["run_id"],
        thread_id=thread_id, interrupt_id=interrupt["id"], value=interrupt.get("value"),
        revision=revision,
    ) for interrupt in interrupts]


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
        for thread_id, record in threads.items():
            waiting.extend(_discover_thread(
                board_dir, deployment, connection, thread_id, record,
                reminder_after=reminder_after, timeout_after=timeout_after, now=now,
                backend_factory=backend_factory))
    return waiting


def discover_waiting_for_thread(board_dir: Path, deployment: str, thread_id: str, *,
                                 reminder_after: float, timeout_after: float, now: float,
                                 backend_factory=AegraExecutionBackend) -> list[WaitingInterrupt]:
    """The single-(Deployment, Thread) case of `discover_waiting`, for a Run
    detail page's action slot (#49): a full board-wide scan is too expensive
    to repeat on every page render/poll (ADR-0018 SS4's "separate what is
    actually expensive from what actually changes often"), so this resolves
    exactly one Thread's waiting interrupts without touching any other.
    """
    connection = load_connections(board_dir).get(deployment)
    if connection is None:
        return []
    record = run_continuity.load_run_continuity(board_dir).get(deployment, {}).get(thread_id)
    if record is None:
        return []
    return _discover_thread(board_dir, deployment, connection, thread_id, record,
                            reminder_after=reminder_after, timeout_after=timeout_after, now=now,
                            backend_factory=backend_factory)


def submit_response(board_dir: Path, *, deployment: str, thread_id: str, interrupt_id: str,
                     approver: str, response_value: Any, revision: str | None,
                     auth_boundary: EntityAuthBoundary, now: float, timeout: float = 120.0,
                     backend_factory=AegraExecutionBackend) -> SubmissionResult:
    """Route one operator's answer to the exact interrupt it targets.

    Order matters and each step is durable before the next begins:

    0. `deployment_lifecycle.ensure_started` -- a managed Deployment that went
       idle while its Run sat interrupted must be running again before a
       resume can reach it (#50 AC5); a startup failure returns here, before
       the response-dedupe claim is ever taken, so a corrected retry is still
       free to try again (mirrors `router.route_signal`'s own ordering).
    1. `response_dedupe.claim` -- a duplicate/racing submission for the same
       interrupt stops here, before authorization or transport.
    2. `auth_boundary.authorize` -- rejected, stale, or revision-mismatched
       submissions never reach ``backend.resume``. A UI-supplied ``approver``
       is only ever a claim passed to the boundary, never treated as
       authority on its own.
    3. ``backend.resume`` -- the one call that can create a new invocation;
       its ambiguous-transport failure is recorded as ``UNKNOWN``, never
       retried automatically.

    Once resumed, the outcome decides the Deployment activity claim
    `ensure_started` just took (#50): genuine terminal success releases it
    and routes this Run's own `run.finished` Signal through
    `router.route_signal` (#18's cascade); a Run still queued/running when
    ``timeout`` elapses, or an ambiguous resume-wait transport failure, keeps
    the claim held and the Thread tracked in `cord_runtime.
    pending_completions` for `router.sweep_pending` to finish observing;
    an interrupt again, or a genuine terminal failure/cancellation, releases
    the claim -- every other path that took it (duplicate, rejected, an
    unreadable Thread) also releases it before returning.
    """
    connection = load_connections(board_dir).get(deployment)
    if connection is None:
        raise ApprovalInboxError(f"unknown deployment alias '{deployment}'")

    started = ensure_started(board_dir, deployment, connection, now=now, health_timeout=_HEALTH_TIMEOUT)
    if started["status"] == "start_failed":
        return SubmissionResult("deployment_unavailable", started["error"])

    retain_deployment_claim = False
    try:
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
        subject = None
        if still_pending:
            try:
                record = run_continuity.logical_run(board_dir, deployment, thread_id)
                run_info = backend.get_run(thread_id, record["api_run_ids"][-1])
                assistant = run_info.get("assistant_id") or ""
                subject = run_info.get("subject")
            except (RuntimeError, run_continuity.UnknownThread):
                assistant = ""

        submission = AuthSubmission(
            deployment=deployment, assistant=assistant, thread_id=thread_id, interrupt_id=interrupt_id,
            approver=approver, response_value=response_value, revision=revision,
        )
        decision = auth_boundary.authorize(submission, current_revision=current_revision,
                                            still_pending=still_pending)
        if not decision.accepted:
            response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                            response_dedupe.REJECTED, now=now)
            return SubmissionResult("rejected", decision.reason)

        # The identity #18's cascade needs once this Run actually finishes
        # (#50): recovered from the pending-completion this Thread's original
        # interrupt recorded, since Aegra's own transported config carries no
        # cascade depth. A Thread `route_signal` never submitted (e.g. a Run
        # started outside `cord`) has no such entry -- cascade_depth defaults
        # to 0, the same as any other fresh Signal.
        pending = pending_completions.get_pending(board_dir, deployment, thread_id)
        cascade_depth = pending["cascade_depth"] if pending is not None else 0
        if not subject and pending is not None:
            subject = pending["subject"]

        try:
            result = backend.resume(thread_id, assistant, response_value, timeout=timeout)
        except RuntimeError:
            response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                            response_dedupe.UNKNOWN, now=now)
            # Ambiguous transport (`backend.resume`'s own contract): the
            # resume may still have been accepted, so the Deployment's
            # activity claim is retained rather than released out from under
            # a Run that might still be executing (#50).
            retain_deployment_claim = True
            return SubmissionResult("unknown", "resume submission had an ambiguous transport outcome")

        response_dedupe.record_outcome(board_dir, deployment, thread_id, interrupt_id,
                                        response_dedupe.RESUMED, now=now)
        try:
            resolve_approved(board_dir, deployment, thread_id, now=now)
        except UnknownWaitingApproval:
            pass  # no approval_expiry clock was ever started for this Thread; resume still succeeded.

        if result.status is RuntimeStatus.SUCCEEDED:
            pending_completions.resolve_pending(board_dir, deployment, thread_id)
            if subject and assistant:
                finished = run_finished_signal(run_id=result.logical_run_id, subject=subject,
                                                connection=deployment, assistant=assistant,
                                                status="success", cascade_depth=cascade_depth)
                route_signal(board_dir, finished, timeout=timeout,
                             now=datetime.fromtimestamp(now, tz=timezone.utc))
        elif result.status is RuntimeStatus.INTERRUPTED:
            # Paused again: release now, exactly like a fresh execute()'s own
            # interrupted result, and keep tracking the Thread so the next
            # resume still has this identity (#50 AC2/AC5).
            pending_completions.record_pending(
                board_dir, deployment, thread_id, result.invocation_id,
                assistant=assistant, subject=subject or "", cascade_depth=cascade_depth,
                concurrency_held=False, deployment_held=False, now=now)
        elif result.wait_outcome is ClientWaitOutcome.DEADLINE_REACHED:
            # Still genuinely queued/running past this call's own wait
            # budget: never release the claim out from under it (#50 AC1/AC2)
            # -- `router.sweep_pending` finishes observing it later.
            retain_deployment_claim = True
            pending_completions.record_pending(
                board_dir, deployment, thread_id, result.invocation_id,
                assistant=assistant, subject=subject or "", cascade_depth=cascade_depth,
                concurrency_held=False, deployment_held=True, now=now)
        elif result.wait_outcome is ClientWaitOutcome.TRANSPORT_ERROR:
            # The resume submission itself succeeded but polling its status
            # failed: ambiguous, so the claim is retained rather than
            # released out from under a Run that may still be executing.
            retain_deployment_claim = True
        # RuntimeStatus.FAILED/CANCELLED fall through: the claim releases and
        # no Signal is constructed -- a documented terminal disposition,
        # matching `route_signal`'s own `"execution_failed"` (#50).
        return SubmissionResult("resumed")
    finally:
        if not retain_deployment_claim:
            release_active(board_dir, deployment, connection, now=now)
