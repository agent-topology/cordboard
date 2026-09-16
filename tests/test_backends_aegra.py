"""Unit tests for the Aegra ExecutionBackend adapter (#42, ADR-0017).

Fakes ``requests.Session.request`` exactly like ``tests/test_aegra_client.py``
-- no live Aegra/Postgres stack. What is measured here is the new contract:
RuntimeStatus vs. ClientWaitOutcome, identity-only default results, explicit
output retrieval, and the LogicalRunId/InvocationId/ThreadId distinction.
"""

import dataclasses

import pytest
import requests

from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.backends.base import ClientWaitOutcome, ExecutionResult, RuntimeStatus
from cord_runtime.run_continuity import logical_run

ENDPOINT = "http://127.0.0.1:9"  # never dialed; requests are faked below.


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _install(monkeypatch, script):
    """script maps a URL suffix to a FakeResponse or a list consumed in order."""
    calls = []

    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        calls.append({"method": method, "url": url, "json": json})
        for suffix, response in script.items():
            if url.endswith(suffix):
                return response.pop(0) if isinstance(response, list) else response
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def test_capabilities_declare_every_supported_operation():
    backend = AegraExecutionBackend(ENDPOINT)
    assert backend.capabilities == {"list_assistants", "execute", "status", "resume", "cancel", "watch"}


def test_execution_result_has_no_output_field():
    """AC4: the default result never carries checkpoint state -- there is no
    field to smuggle it into."""
    fields = {f.name for f in dataclasses.fields(ExecutionResult)}
    assert "values" not in fields
    assert fields == {"logical_run_id", "invocation_id", "thread_id", "assistant_id", "status", "wait_outcome"}


# --- wait_for_run: RuntimeStatus vs. ClientWaitOutcome ----------------------

def test_wait_for_run_deadline_reached_preserves_running_status(monkeypatch):
    _install(monkeypatch, {"/threads/t-1/runs/r-1": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "running"})})
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-1", "r-1", timeout=0)
    assert status == RuntimeStatus.RUNNING
    assert outcome == ClientWaitOutcome.DEADLINE_REACHED


def test_wait_for_run_deadline_reached_preserves_queued_status(monkeypatch):
    _install(monkeypatch, {"/threads/t-2/runs/r-2": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "pending"})})
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-2", "r-2", timeout=0)
    assert status == RuntimeStatus.QUEUED
    assert outcome == ClientWaitOutcome.DEADLINE_REACHED


def test_wait_for_run_actual_interrupt_is_not_a_deadline(monkeypatch):
    _install(monkeypatch, {"/threads/t-3/runs/r-3": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "interrupted"})})
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-3", "r-3", timeout=5)
    assert status == RuntimeStatus.INTERRUPTED
    assert outcome == ClientWaitOutcome.COMPLETED


def test_wait_for_run_terminal_failure_is_a_value_not_an_exception(monkeypatch):
    _install(monkeypatch, {"/threads/t-4/runs/r-4": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "error"})})
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-4", "r-4", timeout=5)
    assert status == RuntimeStatus.FAILED
    assert outcome == ClientWaitOutcome.COMPLETED


def test_wait_for_run_success(monkeypatch):
    _install(monkeypatch, {"/threads/t-5/runs/r-5": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "success"})})
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-5", "r-5", timeout=5)
    assert status == RuntimeStatus.SUCCEEDED
    assert outcome == ClientWaitOutcome.COMPLETED


def test_wait_for_run_unrecognized_raw_status_is_unknown_not_a_guess(monkeypatch):
    _install(monkeypatch, {"/threads/t-6/runs/r-6": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "something-new"})})
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-6", "r-6", timeout=5)
    assert status == RuntimeStatus.UNKNOWN
    assert outcome == ClientWaitOutcome.COMPLETED


def test_wait_for_run_ambiguous_transport_preserves_the_handle_without_retry(monkeypatch):
    """A connection failure mid-poll is a value (UNKNOWN/TRANSPORT_ERROR), not
    a raised exception that discards the Thread/Invocation identity, and is
    never retried -- exactly one GET is attempted."""
    calls = []

    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        calls.append(url)
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    backend = AegraExecutionBackend(ENDPOINT)
    status, outcome = backend.wait_for_run("t-7", "r-7", timeout=5)
    assert status == RuntimeStatus.UNKNOWN
    assert outcome == ClientWaitOutcome.TRANSPORT_ERROR
    assert len(calls) == 1


# --- status()/get_run()/get_state() ------------------------------------------

def test_status_maps_raw_aegra_status_to_runtime_status(monkeypatch):
    _install(monkeypatch, {"/threads/t-8/runs/r-8": FakeResponse(200, {
        "run_id": "r-8", "assistant_id": "opaque-graph", "status": "running",
        "config": {"configurable": {}},
    })})
    backend = AegraExecutionBackend(ENDPOINT)
    assert backend.status("t-8", "r-8") == RuntimeStatus.RUNNING


def test_get_state_is_the_only_path_to_output(monkeypatch):
    _install(monkeypatch, {"/threads/t-9/state": FakeResponse(200, {"values": {"output": "hi"}})})
    backend = AegraExecutionBackend(ENDPOINT)
    assert backend.get_state("t-9") == {"values": {"output": "hi"}}


# --- execute()/resume(): identity-only default result -----------------------

def test_execute_default_result_has_identity_and_status_only(monkeypatch):
    _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-10"}),
        "/threads/t-10/runs": FakeResponse(200, {"run_id": "r-10"}),
        "/threads/t-10/runs/r-10": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "success"}),
    })
    backend = AegraExecutionBackend(ENDPOINT)
    result = backend.execute("opaque-graph", "subject-10", {"source": "hi"}, timeout=5)
    assert result == ExecutionResult(
        logical_run_id="r-10", invocation_id="r-10", thread_id="t-10", assistant_id="opaque-graph",
        status=RuntimeStatus.SUCCEEDED, wait_outcome=ClientWaitOutcome.COMPLETED,
    )


def test_execute_missing_subject_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Subject")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    backend = AegraExecutionBackend(ENDPOINT)
    with pytest.raises(ValueError, match="Subject"):
        backend.execute("opaque-graph", "  ", {})


def test_execute_ambiguous_transport_during_wait_never_resubmits(monkeypatch):
    """A connection failure while waiting must not be papered over by
    resubmitting the POST -- that could execute the graph twice."""
    calls = []

    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        calls.append({"method": method, "url": url, "json": json})
        if url.endswith("/threads/t-10b/runs/r-10b"):
            raise requests.ConnectionError("refused")
        if url.endswith("/threads"):
            return FakeResponse(200, {"thread_id": "t-10b"})
        if url.endswith("/threads/t-10b/runs"):
            return FakeResponse(200, {"run_id": "r-10b"})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    backend = AegraExecutionBackend(ENDPOINT)
    result = backend.execute("opaque-graph", "subject-10b", {}, timeout=5)
    assert result.status == RuntimeStatus.UNKNOWN
    assert result.wait_outcome == ClientWaitOutcome.TRANSPORT_ERROR
    assert result.thread_id == "t-10b" and result.invocation_id == "r-10b"
    submission_calls = [c for c in calls if c["url"].endswith("/threads/t-10b/runs")]
    assert len(submission_calls) == 1


def test_execute_without_continuity_uses_the_invocation_as_its_own_logical_run(monkeypatch):
    """A fresh Thread's first Invocation is definitionally its own logical
    Run (ADR-0017 §1) even when no durable continuity store is wired in."""
    _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-11"}),
        "/threads/t-11/runs": FakeResponse(200, {"run_id": "r-11"}),
        "/threads/t-11/runs/r-11": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "interrupted"}),
    })
    backend = AegraExecutionBackend(ENDPOINT)
    result = backend.execute("opaque-graph", "subject-11", {})
    assert result.logical_run_id == "r-11" == result.invocation_id
    assert result.status == RuntimeStatus.INTERRUPTED
    assert result.wait_outcome == ClientWaitOutcome.COMPLETED


def test_execute_with_continuity_records_the_logical_run_durably(monkeypatch, tmp_path):
    _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-12"}),
        "/threads/t-12/runs": FakeResponse(200, {"run_id": "r-12"}),
        "/threads/t-12/runs/r-12": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "success"}),
    })
    backend = AegraExecutionBackend(ENDPOINT, board_dir=tmp_path, deployment="aegra-local")
    result = backend.execute("opaque-graph", "subject-12", {})
    assert result.logical_run_id == "r-12"
    assert logical_run(tmp_path, "aegra-local", "t-12") == {"run_id": "r-12", "api_run_ids": ["r-12"]}


def test_resume_without_continuity_cannot_determine_the_logical_run(monkeypatch):
    _install(monkeypatch, {
        "/threads/t-13/runs": FakeResponse(200, {"run_id": "r-13b"}),
        "/threads/t-13/runs/r-13b": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "success"}),
    })
    backend = AegraExecutionBackend(ENDPOINT)
    result = backend.resume("t-13", "opaque-graph", {"approved": True})
    assert result.invocation_id == "r-13b"
    assert result.logical_run_id is None


def test_resume_with_continuity_groups_under_the_original_logical_run(monkeypatch, tmp_path):
    """Aegra assigns a new API Run ID on resume (ADR-0003 correction); the
    logical Run stays the Thread's first Invocation."""
    from cord_runtime.run_continuity import record_submission
    record_submission(tmp_path, "aegra-local", "t-14", "r-14a")
    _install(monkeypatch, {
        "/threads/t-14/runs": FakeResponse(200, {"run_id": "r-14b"}),
        "/threads/t-14/runs/r-14b": FakeResponse(200, {"assistant_id": "opaque-graph", "status": "success"}),
    })
    backend = AegraExecutionBackend(ENDPOINT, board_dir=tmp_path, deployment="aegra-local")
    result = backend.resume("t-14", "opaque-graph", {"approved": True})
    assert result.invocation_id == "r-14b"
    assert result.logical_run_id == "r-14a"


def test_resume_missing_thread_id_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Thread ID")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    backend = AegraExecutionBackend(ENDPOINT)
    with pytest.raises(ValueError, match="Thread ID"):
        backend.resume("  ", "opaque-graph", "answer")


# --- cancel()/list_assistants()/watch() --------------------------------------

def test_cancel_returns_a_runtime_status_not_a_dict(monkeypatch):
    _install(monkeypatch, {"/threads/t-15/runs/r-15/cancel": FakeResponse(200, {})})
    backend = AegraExecutionBackend(ENDPOINT)
    assert backend.cancel("t-15", "r-15") == RuntimeStatus.CANCELLED


def test_list_assistants_returns_the_assistants_array(monkeypatch):
    _install(monkeypatch, {"/assistants": FakeResponse(200, {"assistants": [{"assistant_id": "a", "graph_id": "g"}]})})
    backend = AegraExecutionBackend(ENDPOINT)
    assert backend.list_assistants() == [{"assistant_id": "a", "graph_id": "g"}]


def test_list_assistants_defaults_to_empty_on_a_missing_key(monkeypatch):
    _install(monkeypatch, {"/assistants": FakeResponse(200, {})})
    backend = AegraExecutionBackend(ENDPOINT)
    assert backend.list_assistants() == []


def test_watch_yields_the_same_lifecycle_events_as_the_underlying_stream(monkeypatch):
    def fake_watch_lifecycle(endpoint, thread_id, run_id, *, timeout=120, reconnect_delay=0.5):
        yield "metadata", {}, "r-16_event_0"
        yield "end", {"status": "success"}, "r-16_event_1"

    monkeypatch.setattr("cord_runtime.backends.aegra._client_watch_lifecycle", fake_watch_lifecycle)
    backend = AegraExecutionBackend(ENDPOINT)
    events = list(backend.watch("t-16", "r-16", timeout=5))
    assert events == [("metadata", {}, "r-16_event_0"), ("end", {"status": "success"}, "r-16_event_1")]
