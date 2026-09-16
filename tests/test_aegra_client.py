"""Unit tests for the generic Aegra client's status and context contract.

These use a fake ``requests.Session.request`` instead of a live Aegra/Postgres
stack (that integrated path is covered by ``tests/test_aegra.py``): what is
measured here is status mapping and payload transport, which do not require a
real server to distinguish success from failure.
"""

import pytest
import requests

import cord_runtime.aegra_client as aegra_client
from cord_runtime.aegra_client import (
    cancel,
    describe_assistant,
    describe_run,
    execute,
    resume,
    stream_lifecycle,
    watch_lifecycle,
)

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
    assert run_call["json"]["config"] == {
        "configurable": {"cord_subject": "subject-6", "cord_cascade_depth": 0}
    }


def test_execute_transports_cascade_identity_when_declared(monkeypatch):
    """#18: a cascade child carries its causing Run and its own depth under
    the same unread `config.configurable` channel as `cord_subject`."""
    calls = _install(monkeypatch, {
        "/threads": FakeResponse(200, {"thread_id": "t-7"}),
        "/threads/t-7/runs": FakeResponse(200, {"run_id": "r-7"}),
        "/threads/t-7/runs/r-7": FakeResponse(200, {"status": "success"}),
        "/threads/t-7/state": FakeResponse(200, {"values": {}}),
    })
    execute(ENDPOINT, "opaque-graph", "subject-7", {}, timeout=5,
            caused_by_run_id="run-parent", cascade_depth=2)
    run_call = next(c for c in calls if c["url"].endswith("/threads/t-7/runs"))
    assert run_call["json"]["config"] == {
        "configurable": {
            "cord_subject": "subject-7",
            "cord_cascade_depth": 2,
            "cord_caused_by_run_id": "run-parent",
        }
    }


def test_resume_reuses_the_thread_and_gets_a_new_api_run_id(monkeypatch):
    calls = _install(monkeypatch, {
        "/threads/t-7/runs": FakeResponse(200, {"run_id": "r-7b"}),
        "/threads/t-7/runs/r-7b": FakeResponse(200, {"status": "success"}),
        "/threads/t-7/state": FakeResponse(200, {"values": {"approved": True}}),
    })
    result = resume(ENDPOINT, "t-7", "opaque-graph", {"approved": True}, timeout=5)
    assert result == {"run_id": "r-7b", "thread_id": "t-7", "status": "success",
                      "values": {"approved": True}}
    assert not any(c["url"].endswith("/threads") for c in calls)
    run_call = next(c for c in calls if c["url"].endswith("/threads/t-7/runs"))
    assert run_call["json"] == {"assistant_id": "opaque-graph",
                                "command": {"resume": {"approved": True}}, "stream_mode": ["values"]}


def test_resume_interrupted_again_is_waiting(monkeypatch):
    _install(monkeypatch, {
        "/threads/t-8/runs": FakeResponse(200, {"run_id": "r-8b"}),
        "/threads/t-8/runs/r-8b": FakeResponse(200, {"status": "interrupted"}),
    })
    result = resume(ENDPOINT, "t-8", "opaque-graph", "next-answer", timeout=5)
    assert result == {"run_id": "r-8b", "thread_id": "t-8", "status": "waiting"}


def test_resume_missing_thread_id_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Thread ID")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(ValueError, match="Thread ID"):
        resume(ENDPOINT, "  ", "opaque-graph", "answer")


def test_resume_missing_assistant_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for a blank Assistant")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(ValueError, match="Assistant"):
        resume(ENDPOINT, "t-7", "  ", "answer")


def test_resume_wrong_assistant_rejected_without_retry_or_payload(monkeypatch):
    """Mirrors Aegra 0.10.4's own rejection of a bad Assistant on resume
    (docs/decisions/0016-control-plane-and-testbed.md): the client neither
    retries the POST nor inspects the error body to recover."""
    calls = _install(monkeypatch, {
        "/threads/t-7/runs": FakeResponse(422, {"detail": "assistant not found"}),
    })
    with pytest.raises(RuntimeError, match="check service readiness"):
        resume(ENDPOINT, "t-7", "no-such-assistant", "answer", timeout=5)
    assert sum(1 for c in calls if c["url"].endswith("/threads/t-7/runs")) == 1


def test_cancel_requests_the_runs_own_public_cancel_action(monkeypatch):
    calls = _install(monkeypatch, {
        "/threads/t-9/runs/r-9/cancel": FakeResponse(200, {}),
    })
    result = cancel(ENDPOINT, "t-9", "r-9")
    assert result == {"run_id": "r-9", "thread_id": "t-9", "status": "halted"}
    call, = calls
    assert call["method"] == "POST"
    assert call["url"].endswith("/threads/t-9/runs/r-9/cancel")
    assert call["json"] is None


def test_cancel_never_submits_a_resume_command(monkeypatch):
    calls = _install(monkeypatch, {
        "/threads/t-10/runs/r-10/cancel": FakeResponse(200, {}),
    })
    cancel(ENDPOINT, "t-10", "r-10")
    assert not any(c["url"].endswith("/threads/t-10/runs") for c in calls)


def test_cancel_ambiguous_transport_raises_payload_free_diagnostic(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(RuntimeError, match="check service readiness"):
        cancel(ENDPOINT, "t-11", "r-11")


def test_cancel_missing_run_id_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Run ID")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(ValueError, match="Run ID"):
        cancel(ENDPOINT, "t-12", "  ")


def test_describe_run_returns_assistant_status_and_transported_subject(monkeypatch):
    _install(monkeypatch, {
        "/threads/t-13/runs/r-13": FakeResponse(200, {
            "run_id": "r-13", "assistant_id": "opaque-graph", "status": "running",
            "config": {"configurable": {"cord_subject": "subject-13"}},
        }),
    })
    result = describe_run(ENDPOINT, "t-13", "r-13")
    assert result == {"run_id": "r-13", "thread_id": "t-13", "assistant_id": "opaque-graph",
                      "status": "running", "subject": "subject-13"}


def test_describe_run_missing_thread_id_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Thread ID")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(ValueError, match="Thread ID"):
        describe_run(ENDPOINT, "  ", "r-13")


def test_describe_assistant_returns_graph_id(monkeypatch):
    _install(monkeypatch, {"/assistants/opaque-graph": FakeResponse(200, {"graph_id": "opaque-graph"})})
    assert describe_assistant(ENDPOINT, "opaque-graph") == {"graph_id": "opaque-graph"}


def test_describe_assistant_missing_id_fails_before_any_request(monkeypatch):
    def fake_request(self, method, url, json=None, timeout=None, allow_redirects=None):
        raise AssertionError("no request should be sent for an invalid Assistant ID")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(ValueError, match="Assistant ID"):
        describe_assistant(ENDPOINT, "  ")


class FakeStreamResponse:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _install_get(monkeypatch, responses):
    """responses: list of FakeStreamResponse, consumed one per call; records headers seen."""
    calls = []

    def fake_get(self, url, headers=None, stream=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        return responses.pop(0)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return calls


def test_stream_lifecycle_yields_only_lifecycle_events_and_skips_business_state(monkeypatch):
    lines = [
        "event: metadata", 'data: {"run_id":"r-14","attempt":1}', "id: r-14_event_0", "",
        "event: values", "data: {not valid json, never parsed}", "id: r-14_event_1", "",
        ": heartbeat", "",
        "event: end", 'data: {"status":"success"}', "id: r-14_event_2", "",
    ]
    _install_get(monkeypatch, [FakeStreamResponse(200, lines)])
    events = list(stream_lifecycle(ENDPOINT, "t-14", "r-14", timeout=5))
    assert events == [
        ("metadata", {"run_id": "r-14", "attempt": 1}, "r-14_event_0"),
        ("end", {"status": "success"}, "r-14_event_2"),
    ]


def test_stream_lifecycle_sends_last_event_id_header_when_resuming(monkeypatch):
    lines = ["event: end", 'data: {"status":"success"}', "id: r-15_event_3", ""]
    calls = _install_get(monkeypatch, [FakeStreamResponse(200, lines)])
    list(stream_lifecycle(ENDPOINT, "t-15", "r-15", last_event_id="r-15_event_2", timeout=5))
    assert calls[0]["headers"]["Last-Event-ID"] == "r-15_event_2"


def test_stream_lifecycle_omits_last_event_id_header_on_a_fresh_connection(monkeypatch):
    lines = ["event: end", 'data: {"status":"success"}', "id: r-16_event_0", ""]
    calls = _install_get(monkeypatch, [FakeStreamResponse(200, lines)])
    list(stream_lifecycle(ENDPOINT, "t-16", "r-16", timeout=5))
    assert "Last-Event-ID" not in calls[0]["headers"]


def test_stream_lifecycle_non_2xx_raises_payload_free_diagnostic(monkeypatch):
    _install_get(monkeypatch, [FakeStreamResponse(404, [])])
    with pytest.raises(RuntimeError, match="check service readiness"):
        list(stream_lifecycle(ENDPOINT, "t-17", "r-17", timeout=5))


def test_stream_lifecycle_unreachable_endpoint_raises_payload_free_diagnostic(monkeypatch):
    def fake_get(self, url, headers=None, stream=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    with pytest.raises(RuntimeError, match="check service readiness"):
        list(stream_lifecycle(ENDPOINT, "t-18", "r-18", timeout=5))


def test_stream_lifecycle_missing_run_id_fails_before_any_request(monkeypatch):
    def fake_get(self, url, headers=None, stream=None, timeout=None):
        raise AssertionError("no request should be sent for an invalid Run ID")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    with pytest.raises(ValueError, match="Run ID"):
        list(stream_lifecycle(ENDPOINT, "t-19", "  ", timeout=5))


def test_watch_lifecycle_reconnects_with_last_event_id_after_a_drop(monkeypatch):
    """A transport failure mid-stream (before a terminal event) is retried;
    the retry carries the last event id already seen, matching Aegra's own
    reconnect contract (#20 AC3: no duplicate, no lost progress)."""
    calls = []

    def fake_stream_lifecycle(endpoint, thread_id, run_id, *, last_event_id=None, timeout=120):
        calls.append(last_event_id)
        if last_event_id is None:
            yield "metadata", {}, "r-20_event_0"
            raise RuntimeError("Aegra stream request failed; check service readiness and input contract")
        assert last_event_id == "r-20_event_0"
        yield "end", {"status": "success"}, "r-20_event_1"

    monkeypatch.setattr(aegra_client, "stream_lifecycle", fake_stream_lifecycle)
    monkeypatch.setattr(aegra_client.time, "sleep", lambda seconds: None)
    events = list(watch_lifecycle(ENDPOINT, "t-20", "r-20", timeout=5, reconnect_delay=0))
    assert events == [
        ("metadata", {}, "r-20_event_0"),
        ("end", {"status": "success"}, "r-20_event_1"),
    ]
    assert calls == [None, "r-20_event_0"]


def test_watch_lifecycle_stops_after_a_terminal_event_without_reconnecting(monkeypatch):
    calls = []

    def fake_stream_lifecycle(endpoint, thread_id, run_id, *, last_event_id=None, timeout=120):
        calls.append(last_event_id)
        yield "end", {"status": "success"}, "r-21_event_0"

    monkeypatch.setattr(aegra_client, "stream_lifecycle", fake_stream_lifecycle)
    events = list(watch_lifecycle(ENDPOINT, "t-21", "r-21", timeout=5))
    assert events == [("end", {"status": "success"}, "r-21_event_0")]
    assert calls == [None]


def test_watch_lifecycle_gives_up_once_the_timeout_elapses(monkeypatch):
    def fake_stream_lifecycle(endpoint, thread_id, run_id, *, last_event_id=None, timeout=120):
        raise RuntimeError("Aegra stream request failed; check service readiness and input contract")
        yield  # pragma: no cover -- unreachable; makes this a generator function

    # deadline calc -> 0.0 (deadline=1.0); while-check -> 0.5 (enters loop);
    # inner timeout calc -> 2.0; post-failure deadline check -> 2.0 (expired).
    clock = iter([0.0, 0.5, 2.0])

    def fake_monotonic():
        return next(clock, 2.0)

    monkeypatch.setattr(aegra_client, "stream_lifecycle", fake_stream_lifecycle)
    monkeypatch.setattr(aegra_client.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(aegra_client.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="check service readiness"):
        list(watch_lifecycle(ENDPOINT, "t-22", "r-22", timeout=1, reconnect_delay=0))
