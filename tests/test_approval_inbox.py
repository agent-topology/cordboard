"""Waiting-Run discovery and authorized resume across Deployments (#44).

A `FakeBackend` stands in for `AegraExecutionBackend`, injected through the
same `backend_factory` seam production code uses -- no network, Docker, or
real Aegra process, matching this repo's existing style for backend-facing
modules (`test_deployment_lifecycle.py`, `test_router.py`).
"""

import pytest

from cord_runtime import response_dedupe
from cord_runtime.approval_expiry import evaluate as expiry_evaluate
from cord_runtime.approval_expiry import load_approval_expiry
from cord_runtime.approval_inbox import ApprovalInboxError, discover_waiting, submit_response
from cord_runtime.backends.base import RuntimeStatus
from cord_runtime.connections import add_connection
from cord_runtime.entity_auth import SyntheticEntityAuthBoundary
from cord_runtime.run_continuity import record_submission

NOW = 1_800_000_000.0


class FakeBackend:
    def __init__(self, *, status=RuntimeStatus.INTERRUPTED, run_info=None, state=None,
                 resume_error=None, status_error=None, state_error=None):
        self._status = status
        self._run_info = run_info or {"assistant_id": "triage-graph", "subject": "urn:cordboard:fixture:44"}
        self._state = state if state is not None else {
            "tasks": [{"interrupts": [{"id": "interrupt-a", "value": {"question": "approve?"}}]}],
            "checkpoint": {"checkpoint_id": "chk-1"},
        }
        self._resume_error = resume_error
        self._status_error = status_error
        self._state_error = state_error
        self.resume_calls = []

    def status(self, thread_id, invocation_id):
        if self._status_error:
            raise self._status_error
        return self._status

    def get_run(self, thread_id, invocation_id):
        return self._run_info

    def get_state(self, thread_id):
        if self._state_error:
            raise self._state_error
        return self._state

    def resume(self, thread_id, assistant, resume_value):
        self.resume_calls.append((thread_id, assistant, resume_value))
        if self._resume_error:
            raise self._resume_error


def _factory(backends: dict):
    def factory(endpoint, *, board_dir=None, deployment=None):
        return backends[deployment]
    return factory


def _seed(board_dir, alias="aegra-local", endpoint="http://aegra.example", thread_id="thread-1",
          api_run_id="run-1"):
    add_connection(board_dir, alias, endpoint)
    record_submission(board_dir, alias, thread_id, api_run_id)
    return alias, thread_id


# --- discovery ---

def test_discover_waiting_exposes_full_identity_for_one_pending_interrupt(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    found = discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW,
                              backend_factory=_factory({alias: backend}))
    assert len(found) == 1
    entry = found[0]
    assert entry.deployment == alias
    assert entry.assistant == "triage-graph"
    assert entry.subject == "urn:cordboard:fixture:44"
    assert entry.logical_run_id == "run-1"
    assert entry.thread_id == thread_id
    assert entry.interrupt_id == "interrupt-a"
    assert entry.revision == "chk-1"


def test_discover_waiting_distinguishes_two_pending_interrupts_on_one_thread(tmp_path):
    alias, thread_id = _seed(tmp_path)
    state = {
        "tasks": [
            {"interrupts": [{"id": "interrupt-a", "value": {"question": "a?"}}]},
            {"interrupts": [{"id": "interrupt-b", "value": {"question": "b?"}}]},
        ],
        "checkpoint": {"checkpoint_id": "chk-1"},
    }
    backend = FakeBackend(state=state)
    found = discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW,
                              backend_factory=_factory({alias: backend}))
    ids = {entry.interrupt_id for entry in found}
    assert ids == {"interrupt-a", "interrupt-b"}


def test_discover_waiting_excludes_threads_that_are_not_interrupted(tmp_path):
    alias, _ = _seed(tmp_path)
    backend = FakeBackend(status=RuntimeStatus.RUNNING)
    found = discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW,
                              backend_factory=_factory({alias: backend}))
    assert found == []


def test_discover_waiting_starts_the_approval_expiry_clock(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW,
                      backend_factory=_factory({alias: backend}))
    assert expiry_evaluate(tmp_path, alias, thread_id, now=NOW) == "waiting"


def test_rediscovery_after_a_simulated_restart_preserves_the_original_deadline(tmp_path):
    """`discover_waiting` calls `record_waiting` on every scan; nothing here
    holds process memory, so a second scan -- standing in for one after a
    process restart between pause and claim (#44 AC5) -- must not grant a
    still-waiting Thread a fresh clock."""
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    factory = _factory({alias: backend})
    discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW, backend_factory=factory)
    discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW + 500, backend_factory=factory)
    record = load_approval_expiry(tmp_path)[alias][thread_id]
    assert record["waiting_since"] == NOW


def test_an_unreachable_deployment_is_skipped_not_raised(tmp_path):
    ok_alias, ok_thread = _seed(tmp_path, alias="aegra-ok", endpoint="http://ok.example", thread_id="thread-ok")
    down_alias, _ = _seed(tmp_path, alias="aegra-down", endpoint="http://down.example", thread_id="thread-down")
    backends = {
        ok_alias: FakeBackend(),
        down_alias: FakeBackend(status_error=RuntimeError("unreachable")),
    }
    found = discover_waiting(tmp_path, reminder_after=60, timeout_after=300, now=NOW,
                              backend_factory=_factory(backends))
    assert len(found) == 1
    assert found[0].deployment == ok_alias


# --- submission ---

GRANTS = {("aegra-local", "triage-graph"): frozenset({"alice"})}


def test_submit_response_to_an_unknown_deployment_raises(tmp_path):
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    with pytest.raises(ApprovalInboxError):
        submit_response(tmp_path, deployment="nope", thread_id="t", interrupt_id="i",
                         approver="alice", response_value=True, revision="chk-1",
                         auth_boundary=boundary, now=NOW, backend_factory=_factory({}))


def test_authorized_response_resumes_and_records_disposition(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "resumed"
    assert backend.resume_calls == [(thread_id, "triage-graph", True)]
    claim = response_dedupe.get_claim(tmp_path, alias, thread_id, "interrupt-a")
    assert claim["outcome"] == response_dedupe.RESUMED


def test_unauthorized_approver_is_rejected_and_never_reaches_resume(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="mallory", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "rejected"
    assert backend.resume_calls == []
    assert response_dedupe.get_claim(tmp_path, alias, thread_id, "interrupt-a")["outcome"] == response_dedupe.REJECTED


def test_a_ui_supplied_identity_alone_is_never_treated_as_authority(tmp_path):
    # No grants registered anywhere -- an approver claiming to be "alice" in
    # the request is not, by itself, authority.
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    boundary = SyntheticEntityAuthBoundary({})
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "rejected"
    assert backend.resume_calls == []


def test_stale_interrupt_no_longer_present_is_rejected(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend(state={"tasks": [], "checkpoint": {"checkpoint_id": "chk-1"}})
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "rejected"
    assert "stale" in result.reason
    assert backend.resume_calls == []


def test_revision_mismatched_submission_is_rejected(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()  # current checkpoint is "chk-1"
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-0-stale",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "rejected"
    assert "revision" in result.reason
    assert backend.resume_calls == []


def test_a_duplicate_response_to_the_same_interrupt_is_refused_before_transport(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend()
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    first = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                             approver="alice", response_value=True, revision="chk-1",
                             auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    second = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW + 1, backend_factory=_factory({alias: backend}))
    assert first.status == "resumed"
    assert second.status == "duplicate"
    assert len(backend.resume_calls) == 1  # never resubmitted


def test_an_ambiguous_resume_transport_failure_is_recorded_unknown_not_retried(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend(resume_error=RuntimeError("ambiguous transport"))
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "unknown"
    assert response_dedupe.get_claim(tmp_path, alias, thread_id, "interrupt-a")["outcome"] == response_dedupe.UNKNOWN

    # A second call for the same interrupt is still a duplicate claim -- an
    # UNKNOWN outcome is never treated as safe to silently resubmit.
    again = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                             approver="alice", response_value=True, revision="chk-1",
                             auth_boundary=boundary, now=NOW + 1, backend_factory=_factory({alias: backend}))
    assert again.status == "duplicate"
    assert len(backend.resume_calls) == 1


def test_an_unreadable_thread_state_is_recorded_unknown(tmp_path):
    alias, thread_id = _seed(tmp_path)
    backend = FakeBackend(state_error=RuntimeError("unreachable"))
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    result = submit_response(tmp_path, deployment=alias, thread_id=thread_id, interrupt_id="interrupt-a",
                              approver="alice", response_value=True, revision="chk-1",
                              auth_boundary=boundary, now=NOW, backend_factory=_factory({alias: backend}))
    assert result.status == "unknown"
    assert backend.resume_calls == []
