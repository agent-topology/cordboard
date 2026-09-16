"""Live/recorded reconciliation (#20): pure state, no transport."""

import pytest

from cord_runtime.live_reconciliation import (
    INGESTION_FAILED,
    INGESTION_PENDING,
    LIVE,
    LiveRun,
    reconcile,
)


def _run(run_id="r-1", thread_id="t-1", **kw):
    return LiveRun(run_id, thread_id, graph_id="g", assistant_id="a", subject="s", **kw)


def test_metadata_moves_pending_to_running():
    run = _run()
    run.apply("metadata", {"run_id": "r-1", "attempt": 1}, "r-1_event_0", now=0.0)
    assert run.status == "running"


def test_end_event_sets_status_and_completion_time():
    run = _run()
    run.apply("end", {"status": "success"}, "r-1_event_1", now=10.0)
    assert run.status == "success"
    assert run.completed_at == 10.0
    assert run.is_terminal


def test_error_event_sets_status_error_and_completion_time():
    run = _run()
    run.apply("error", {"error": "Error", "message": "execution failed"}, "r-1_event_1", now=5.0)
    assert run.status == "error"
    assert run.completed_at == 5.0


def test_replayed_duplicate_event_never_regresses_status_or_completion(monkeypatch=None):
    run = _run()
    run.apply("metadata", {}, "r-1_event_0", now=0.0)
    run.apply("end", {"status": "success"}, "r-1_event_1", now=10.0)
    # Reconnect replay resends the same two events verbatim (#20 AC3).
    run.apply("metadata", {}, "r-1_event_0", now=20.0)
    run.apply("end", {"status": "success"}, "r-1_event_1", now=20.0)
    assert run.status == "success"
    assert run.completed_at == 10.0  # not overwritten by the later replay


def test_out_of_order_older_event_after_newer_is_ignored():
    run = _run()
    run.apply("end", {"status": "success"}, "r-1_event_5", now=10.0)
    run.apply("metadata", {}, "r-1_event_0", now=20.0)  # stale/reordered
    assert run.status == "success"
    assert run.completed_at == 10.0


def test_event_with_no_id_is_always_applied_but_never_regresses_a_later_one():
    run = _run()
    run.apply("metadata", {}, None, now=0.0)
    assert run.status == "running"
    run.apply("end", {"status": "success"}, "r-1_event_9", now=1.0)
    assert run.status == "success"


@pytest.mark.parametrize("run_id", ["", "   "])
def test_live_run_requires_nonempty_run_id(run_id):
    with pytest.raises(ValueError, match="run_id"):
        LiveRun(run_id, "t-1")


@pytest.mark.parametrize("thread_id", ["", "   "])
def test_live_run_requires_nonempty_thread_id(thread_id):
    with pytest.raises(ValueError, match="thread_id"):
        LiveRun("r-1", thread_id)


def test_reconcile_drops_a_run_id_already_recorded():
    """#20 AC4: the durable record always replaces the temporary live one."""
    run = _run()
    run.apply("metadata", {}, "r-1_event_0", now=0.0)
    views = reconcile({"r-1": run}, {"r-1"}, now=1.0)
    assert views == {}


def test_reconcile_still_running_is_live_regardless_of_elapsed_time():
    run = _run()
    run.apply("metadata", {}, "r-1_event_0", now=0.0)
    views = reconcile({"r-1": run}, set(), now=10_000.0)
    assert views["r-1"]["source"] == LIVE
    assert views["r-1"]["seconds_since_completion"] is None
    assert views["r-1"]["aegra_status"] == "running"


def test_reconcile_just_completed_is_live_before_the_pending_threshold():
    run = _run()
    run.apply("end", {"status": "success"}, "r-1_event_1", now=100.0)
    views = reconcile({"r-1": run}, set(), now=102.0, ingestion_pending_after=5.0)
    assert views["r-1"]["source"] == LIVE


def test_reconcile_past_pending_threshold_is_ingestion_pending():
    """#20 AC2: a completed live Run stays visible while ingestion lags."""
    run = _run()
    run.apply("end", {"status": "success"}, "r-1_event_1", now=100.0)
    views = reconcile({"r-1": run}, set(), now=110.0,
                      ingestion_pending_after=5.0, ingestion_failed_after=300.0)
    view = views["r-1"]
    assert view["source"] == INGESTION_PENDING
    assert view["seconds_since_completion"] == 10.0


def test_reconcile_past_failed_threshold_is_ingestion_failed_not_endless_loading():
    """#20 AC5: a bounded ingestion failure is visible, not endless loading."""
    run = _run()
    run.apply("end", {"status": "success"}, "r-1_event_1", now=100.0)
    views = reconcile({"r-1": run}, set(), now=500.0,
                      ingestion_pending_after=5.0, ingestion_failed_after=300.0)
    assert views["r-1"]["source"] == INGESTION_FAILED


def test_reconcile_carries_identity_without_any_model_or_tier_field():
    """#20 AC1: reconciliation is model-free by construction."""
    run = _run()
    run.apply("end", {"status": "success"}, "r-1_event_1", now=0.0)
    view = reconcile({"r-1": run}, set(), now=0.0)["r-1"]
    assert set(view) == {"run_id", "thread_id", "graph_id", "assistant_id",
                         "subject", "aegra_status", "source", "seconds_since_completion"}
    assert "model" not in view and "tier" not in view
