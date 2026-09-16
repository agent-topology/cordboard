"""The synthetic entity authorization boundary (#44).

`SyntheticEntityAuthBoundary` is the only in-repo `EntityAuthBoundary`
implementation, and only for tests -- a real entity's production
authorization stays entity-owned (#14's own decision, preserved as #44's Out
of scope). Each check below isolates exactly one failure reason so a
submission that fails for one cause is never misreported as failing for
another.
"""

from cord_runtime.entity_auth import AuthSubmission, SyntheticEntityAuthBoundary

GRANTS = {("aegra-local", "triage-graph"): frozenset({"alice"})}


def _submission(**overrides):
    fields = dict(deployment="aegra-local", assistant="triage-graph", thread_id="thread-1",
                  interrupt_id="interrupt-a", approver="alice", response_value=True, revision="chk-1")
    fields.update(overrides)
    return AuthSubmission(**fields)


def test_authorized_matching_submission_is_accepted():
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    decision = boundary.authorize(_submission(), current_revision="chk-1", still_pending=True)
    assert decision.accepted is True


def test_stale_interrupt_is_rejected_even_for_a_granted_approver():
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    decision = boundary.authorize(_submission(), current_revision="chk-1", still_pending=False)
    assert decision.accepted is False
    assert "stale" in decision.reason


def test_revision_mismatch_is_rejected():
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    decision = boundary.authorize(_submission(revision="chk-1"), current_revision="chk-2", still_pending=True)
    assert decision.accepted is False
    assert "revision" in decision.reason


def test_missing_revision_is_treated_as_a_mismatch_not_a_free_pass():
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    decision = boundary.authorize(_submission(revision=None), current_revision="chk-1", still_pending=True)
    assert decision.accepted is False


def test_ungranted_approver_is_rejected():
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    decision = boundary.authorize(_submission(approver="mallory"), current_revision="chk-1", still_pending=True)
    assert decision.accepted is False
    assert "unauthorized" in decision.reason


def test_a_ui_supplied_identity_alone_grants_nothing_without_a_matching_grant():
    boundary = SyntheticEntityAuthBoundary({})  # no grants registered at all
    decision = boundary.authorize(_submission(), current_revision="chk-1", still_pending=True)
    assert decision.accepted is False


def test_grant_is_scoped_to_deployment_and_assistant():
    boundary = SyntheticEntityAuthBoundary(GRANTS)
    decision = boundary.authorize(
        _submission(assistant="other-graph"), current_revision="chk-1", still_pending=True)
    assert decision.accepted is False
