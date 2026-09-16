"""Durable tracking for a Run whose completion outlives its submitting call (#50)."""

import pytest

from cord_runtime.pending_completions import (
    PendingCompletionError,
    get_pending,
    load_pending,
    record_pending,
    resolve_pending,
    update_pending,
)

NOW = 1_800_000_000.0


def test_record_and_get_pending_round_trips(tmp_path):
    record_pending(tmp_path, "aegra-local", "t-1", "r-1", assistant="graph-a", subject="urn:demo",
                    cascade_depth=0, concurrency_held=True, deployment_held=True, now=NOW)
    entry = get_pending(tmp_path, "aegra-local", "t-1")
    assert entry == {
        "deployment": "aegra-local", "thread_id": "t-1", "invocation_id": "r-1",
        "assistant": "graph-a", "subject": "urn:demo", "cascade_depth": 0,
        "concurrency_held": True, "deployment_held": True, "recorded_at": NOW,
    }


def test_get_pending_for_an_untracked_thread_is_none(tmp_path):
    assert get_pending(tmp_path, "aegra-local", "t-missing") is None


def test_update_pending_changes_only_the_given_fields(tmp_path):
    record_pending(tmp_path, "aegra-local", "t-1", "r-1", assistant="graph-a", subject="urn:demo",
                    cascade_depth=1, concurrency_held=True, deployment_held=True, now=NOW)
    updated = update_pending(tmp_path, "aegra-local", "t-1", invocation_id="r-2",
                              concurrency_held=False, now=NOW + 10)
    assert updated["invocation_id"] == "r-2"
    assert updated["concurrency_held"] is False
    assert updated["deployment_held"] is True  # untouched
    assert updated["assistant"] == "graph-a"  # identity untouched
    assert updated["cascade_depth"] == 1
    assert updated["recorded_at"] == NOW + 10


def test_update_pending_for_an_untracked_thread_is_a_noop_returning_none(tmp_path):
    assert update_pending(tmp_path, "aegra-local", "t-missing", concurrency_held=False, now=NOW) is None
    assert load_pending(tmp_path) == {}


def test_resolve_pending_removes_the_entry_and_returns_it(tmp_path):
    record_pending(tmp_path, "aegra-local", "t-1", "r-1", assistant="graph-a", subject="urn:demo",
                    cascade_depth=0, concurrency_held=True, deployment_held=True, now=NOW)
    resolved = resolve_pending(tmp_path, "aegra-local", "t-1")
    assert resolved["invocation_id"] == "r-1"
    assert get_pending(tmp_path, "aegra-local", "t-1") is None


def test_resolve_pending_of_an_untracked_thread_is_a_noop_returning_none(tmp_path):
    assert resolve_pending(tmp_path, "aegra-local", "t-missing") is None


def test_record_pending_rejects_empty_identity_fields(tmp_path):
    with pytest.raises(PendingCompletionError):
        record_pending(tmp_path, "", "t-1", "r-1", assistant="graph-a", subject="urn:demo",
                        cascade_depth=0, concurrency_held=True, deployment_held=True, now=NOW)
    with pytest.raises(PendingCompletionError):
        record_pending(tmp_path, "aegra-local", "t-1", "r-1", assistant="", subject="urn:demo",
                        cascade_depth=0, concurrency_held=True, deployment_held=True, now=NOW)


def test_record_pending_rejects_a_negative_cascade_depth(tmp_path):
    with pytest.raises(PendingCompletionError):
        record_pending(tmp_path, "aegra-local", "t-1", "r-1", assistant="graph-a", subject="urn:demo",
                        cascade_depth=-1, concurrency_held=True, deployment_held=True, now=NOW)


def test_two_threads_on_the_same_deployment_track_independently(tmp_path):
    record_pending(tmp_path, "aegra-local", "t-1", "r-1", assistant="graph-a", subject="urn:a",
                    cascade_depth=0, concurrency_held=True, deployment_held=True, now=NOW)
    record_pending(tmp_path, "aegra-local", "t-2", "r-2", assistant="graph-a", subject="urn:b",
                    cascade_depth=0, concurrency_held=True, deployment_held=True, now=NOW)
    resolve_pending(tmp_path, "aegra-local", "t-1")
    assert get_pending(tmp_path, "aegra-local", "t-1") is None
    assert get_pending(tmp_path, "aegra-local", "t-2") is not None
