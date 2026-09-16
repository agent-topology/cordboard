"""Entity-owned authorization/submission port for approval responses (#44).

Cordboard never decides who may answer a runtime interrupt -- that authority
belongs to the Graph's owning entity (ARCHITECTURE.md "Isolation, routing,
and approval"; #14's own decision, preserved as #44's residual scope: "reuse
the owning entity's public authorization/resume boundary; do not copy its
Slack/Git validator into Cordboard"). ``EntityAuthBoundary`` is the narrow
seam a real entity implements; this module also ships exactly one synthetic,
in-repo implementation for tests, the same synthetic-first split ADR-0016
already uses for Aegra itself. A UI-supplied approver identity is never
itself the authority -- only this boundary's returned decision is.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AuthSubmission:
    """One operator's claimed answer to one specific runtime interrupt.

    ``revision`` is the checkpoint identity the approver observed when they
    read the interrupt (`cord_runtime.approval_inbox.WaitingInterrupt.revision`);
    it is compared against the Thread's current revision at authorization
    time, never trusted on its own.
    """

    deployment: str
    assistant: str
    thread_id: str
    interrupt_id: str
    approver: str
    response_value: Any
    revision: str | None


@dataclass(frozen=True)
class AuthDecision:
    accepted: bool
    reason: str


class EntityAuthBoundary(Protocol):
    """The public seam an owning entity implements against.

    ``current_revision``/``still_pending`` are supplied by the caller (freshly
    read from the Thread's own state), not fetched here, so a boundary
    implementation stays pure and network-free -- the same injected-dependency
    style `cord_runtime.approval_expiry` already uses for its clock.
    """

    def authorize(self, submission: AuthSubmission, *,
                  current_revision: str | None, still_pending: bool) -> AuthDecision: ...


class SyntheticEntityAuthBoundary:
    """A small, in-repo stand-in entity boundary for tests only (#44) -- never
    a production authorization implementation, per this issue's own Out of
    scope. Grants are a plain allow-list supplied by the caller, never read
    from any credential store: ``{(deployment, assistant): {approver, ...}}``.

    Checks run stale, then revision, then identity -- in that order, so a
    submission that fails for one reason is never misreported as failing for
    another (e.g. a stale interrupt is never reported "unauthorized" just
    because its claimed Assistant could not be resolved).
    """

    def __init__(self, grants: dict[tuple[str, str], frozenset[str]]):
        self._grants = grants

    def authorize(self, submission: AuthSubmission, *,
                  current_revision: str | None, still_pending: bool) -> AuthDecision:
        if not still_pending:
            return AuthDecision(False, "stale: interrupt is no longer pending")
        if submission.revision != current_revision:
            return AuthDecision(False, "revision mismatch: interrupt state has moved on")
        allowed = self._grants.get((submission.deployment, submission.assistant), frozenset())
        if submission.approver not in allowed:
            return AuthDecision(False, "unauthorized: approver has no grant for this Deployment/Assistant")
        return AuthDecision(True, "authorized")
