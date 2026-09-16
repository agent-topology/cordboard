"""Unit tests for the generic Aegra client's status and context contract.

These use a fake ``requests.Session.request`` instead of a live Aegra/Postgres
stack (that integrated path is covered by ``tests/test_aegra.py``): what is
measured here is status mapping and payload transport, which do not require a
real server to distinguish success from failure.
"""

import pytest
import requests

from cord_runtime.aegra_client import execute

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


def test_execute_success_returns_status_and_values(monkeypatch):
    calls = _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-1"}),
        "/threads/t-1/runs": FakeResponse(200, {"run_id": "r-1"}),
        "/threads/t-1/runs/r-1": [FakeResponse(200, {"status": "pending"}),
                                   FakeResponse(200, {"status": "success"})],
        "/threads/t-1/state": FakeResponse(200, {"values": {"output": "hello"}}),
    })
    result = execute(ENDPOINT, "minimal-graph", "subject-1", {"source": "hello"}, timeout=5)
    assert result == {"run_id": "r-1", "thread_id": "t-1", "status": "success",
                      "values": {"output": "hello"}}
    run_call = next(c for c in calls if c["url"].endswith("/threads/t-1/runs"))
    assert "context" not in run_call["json"]


def test_execute_interrupted_is_waiting_not_success_or_failure(monkeypatch):
    _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-2"}),
        "/threads/t-2/runs": FakeResponse(200, {"run_id": "r-2"}),
        "/threads/t-2/runs/r-2": FakeResponse(200, {"status": "interrupted"}),
    })
    result = execute(ENDPOINT, "minimal-graph", "subject-2", {}, timeout=5)
    assert result == {"run_id": "r-2", "thread_id": "t-2", "status": "waiting"}


def test_execute_wait_budget_elapsed_is_waiting_not_an_exception(monkeypatch):
    _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-3"}),
        "/threads/t-3/runs": FakeResponse(200, {"run_id": "r-3"}),
        "/threads/t-3/runs/r-3": FakeResponse(200, {"status": "running"}),
    })
    result = execute(ENDPOINT, "minimal-graph", "subject-3", {}, timeout=0)
    assert result == {"run_id": "r-3", "thread_id": "t-3", "status": "waiting"}


def test_execute_error_status_raises_without_identities_leaking_payload(monkeypatch):
    _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-4"}),
        "/threads/t-4/runs": FakeResponse(200, {"run_id": "r-4"}),
        "/threads/t-4/runs/r-4": FakeResponse(200, {"status": "error"}),
    })
    with pytest.raises(RuntimeError, match="did not succeed"):
        execute(ENDPOINT, "minimal-graph", "subject-4", {}, timeout=5)


def test_execute_unreachable_endpoint_raises_payload_free_diagnostic(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(RuntimeError, match="check service readiness"):
        execute(ENDPOINT, "minimal-graph", "subject-5", {"secret": "sh"}, timeout=5)


def test_execute_missing_subject_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Subject")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(ValueError, match="Subject"):
        execute(ENDPOINT, "minimal-graph", "  ", {})


def test_execute_transports_request_context_unread(monkeypatch):
    calls = _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-6"}),
        "/threads/t-6/runs": FakeResponse(200, {"run_id": "r-6"}),
        "/threads/t-6/runs/r-6": FakeResponse(200, {"status": "success"}),
        "/threads/t-6/state": FakeResponse(200, {"values": {}}),
    })
    context = {"tenant": "acme", "request_id": "req-1"}
    execute(ENDPOINT, "opaque-graph", "subject-6", {"items": []},
           request_context=context, timeout=5)
    run_call = next(c for c in calls if c["url"].endswith("/threads/t-6/runs"))
    assert run_call["json"]["context"] == context
    assert run_call["json"]["config"] == {"configurable": {"cord_subject": "subject-6"}}
