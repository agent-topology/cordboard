"""Board-level reminder/timeout/halt clock for waiting approvals (#15).

Every deadline is driven by an injected `now`, never a real clock or real
waiting; the halt action is a fake `halt()` callable, never a live Aegra
call (that HTTP contract is `aegra_client.cancel`'s own unit tests).
"""

import pytest

from cord_runtime.approval_expiry import (
    REMINDER_DUE,
    RESOLVED,
    TIMEOUT_DUE,
    WAITING,
    ApprovalExpiryError,
    UnknownWaitingApproval,
    evaluate,
    load_approval_expiry,
    mark_reminded,
    record_waiting,
    resolve_approved,
    resolve_timed_out,
)


def test_record_waiting_fixes_deadlines_from_the_injected_clock(tmp_path):
    record = record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                             now=1000.0, reminder_after=60.0, timeout_after=300.0)
    assert record["run_id"] == "r-1"
    assert record["waiting_since"] == 1000.0
    assert record["reminder_at"] == 1060.0
    assert record["timeout_at"] == 1300.0
    assert record["disposition"] is None


def test_controlled_clock_walks_waiting_to_reminder_to_timeout(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    assert evaluate(tmp_path, "aegra-local", "t-1", now=1000.0) == WAITING
    assert evaluate(tmp_path, "aegra-local", "t-1", now=1059.9) == WAITING
    assert evaluate(tmp_path, "aegra-local", "t-1", now=1060.0) == REMINDER_DUE
    assert evaluate(tmp_path, "aegra-local", "t-1", now=1299.9) == REMINDER_DUE
    assert evaluate(tmp_path, "aegra-local", "t-1", now=1300.0) == TIMEOUT_DUE


def test_evaluate_never_mutates_stored_state(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    evaluate(tmp_path, "aegra-local", "t-1", now=5000.0)
    stored = load_approval_expiry(tmp_path)["aegra-local"]["t-1"]
    assert stored["disposition"] is None
    assert stored["timeout_at"] == 1300.0


def test_restart_preserves_the_original_deadline_and_pending_identity(tmp_path):
    first = record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                            now=1000.0, reminder_after=60.0, timeout_after=300.0)
    # Simulate a process restart: same call, later `now`, same board dir.
    second = record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                             now=9000.0, reminder_after=60.0, timeout_after=300.0)
    assert second == first
    assert second["waiting_since"] == 1000.0
    assert second["timeout_at"] == 1300.0
    assert second["run_id"] == "r-1"


def test_mark_reminded_is_idempotent_and_leaves_the_deadline_untouched(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    once = mark_reminded(tmp_path, "aegra-local", "t-1", now=1060.0)
    twice = mark_reminded(tmp_path, "aegra-local", "t-1", now=1200.0)
    assert once["reminded"] is True
    assert once["reminded_at"] == 1060.0
    assert twice["reminded_at"] == 1060.0  # second call did not re-stamp it
    assert twice["timeout_at"] == 1300.0


def test_approval_arriving_before_timeout_resolves_and_is_reported(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    resolved = resolve_approved(tmp_path, "aegra-local", "t-1", now=1100.0)
    assert resolved["disposition"] == "resumed"
    assert resolved["resolved_at"] == 1100.0
    assert evaluate(tmp_path, "aegra-local", "t-1", now=9999.0) == RESOLVED


def test_timeout_calls_the_chosen_halt_action_and_records_it(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    calls = []
    resolved = resolve_timed_out(tmp_path, "aegra-local", "t-1", now=1300.0, halt=lambda: calls.append(1))
    assert calls == [1]
    assert resolved["disposition"] == "halted"
    assert resolved["resolved_at"] == 1300.0


def test_approval_race_a_concurrent_resume_wins_and_halt_is_never_called(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    resolve_approved(tmp_path, "aegra-local", "t-1", now=1290.0)
    halt_called = []
    resolved = resolve_timed_out(tmp_path, "aegra-local", "t-1", now=1300.0,
                                  halt=lambda: halt_called.append(1))
    assert halt_called == []  # never forges a rejection over an already-approved Run
    assert resolved["disposition"] == "resumed"


def test_disposition_race_first_writer_wins_exactly_one_durable_outcome(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)
    resolve_timed_out(tmp_path, "aegra-local", "t-1", now=1300.0, halt=lambda: None)
    late_approval = resolve_approved(tmp_path, "aegra-local", "t-1", now=1305.0)
    assert late_approval["disposition"] == "halted"  # the earlier disposition stands
    stored = load_approval_expiry(tmp_path)["aegra-local"]["t-1"]
    assert stored["resolved_at"] == 1300.0


def test_ambiguous_halt_transport_leaves_the_wait_undisposed(tmp_path):
    record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                    now=1000.0, reminder_after=60.0, timeout_after=300.0)

    def flaky_halt():
        raise RuntimeError("Aegra API request failed; check service readiness and input contract")

    with pytest.raises(RuntimeError, match="check service readiness"):
        resolve_timed_out(tmp_path, "aegra-local", "t-1", now=1300.0, halt=flaky_halt)
    stored = load_approval_expiry(tmp_path)["aegra-local"]["t-1"]
    assert stored["disposition"] is None  # not silently recorded as halted
    assert evaluate(tmp_path, "aegra-local", "t-1", now=1300.0) == TIMEOUT_DUE


def test_evaluate_unknown_thread_raises(tmp_path):
    with pytest.raises(UnknownWaitingApproval):
        evaluate(tmp_path, "aegra-local", "no-such-thread", now=1000.0)


def test_record_waiting_rejects_timeout_shorter_than_reminder(tmp_path):
    with pytest.raises(ApprovalExpiryError, match="timeout_after"):
        record_waiting(tmp_path, "aegra-local", "t-1", "r-1",
                        now=1000.0, reminder_after=300.0, timeout_after=60.0)


def test_record_waiting_rejects_empty_identity(tmp_path):
    with pytest.raises(ApprovalExpiryError, match="non-empty"):
        record_waiting(tmp_path, "aegra-local", "  ", "r-1",
                        now=1000.0, reminder_after=60.0, timeout_after=300.0)
