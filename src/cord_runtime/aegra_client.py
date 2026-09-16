"""Fresh non-conversational executions through Aegra's public HTTP API."""

import time

import requests


def _session_request(session, endpoint):
    def request(method, path, body=None):
        try:
            response = session.request(method, endpoint.rstrip("/") + path,
                                       json=body, timeout=10, allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise ValueError
            return response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("Aegra API request failed; check service readiness and input contract") from None
    return request


def _poll_until_settled(request, path, run_id, thread_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        current = request("GET", path + "/runs/" + run_id)
        status = current["status"]
        if status == "success":
            values = request("GET", path + "/state")["values"]
            return {"run_id": run_id, "thread_id": thread_id, "status": "success", "values": values}
        if status == "interrupted":
            return {"run_id": run_id, "thread_id": thread_id, "status": "waiting"}
        if status not in ("pending", "running"):
            raise RuntimeError("Aegra execution did not succeed; inspect metadata and archive")
        if time.monotonic() >= deadline:
            return {"run_id": run_id, "thread_id": thread_id, "status": "waiting"}
        time.sleep(.1)


def execute(endpoint: str, assistant: str, subject: str, graph_input: dict, *,
            request_context: dict | None = None, timeout=120,
            caused_by_run_id: str | None = None, cascade_depth: int = 0):
    """Return API identities and a bounded status, without logging payloads.

    Each call creates a new Thread -- a new trace for a #18 cascade child, not
    a resume of the Run that caused it. API retries are deliberately not
    exposed: resubmitting a POST can create another execution. To continue a
    Thread that came back ``"waiting"``, use ``resume()``.

    The returned ``status`` is one of ``"success"`` (``values`` present),
    ``"waiting"`` (an interrupted Run, or the wait budget elapsed while it was
    still pending/running; the Run may still be active — poll or resume with
    the returned identities), or an exception for a genuine failure.
    ``request_context`` is transported unread as the API's top-level
    ``context``, distinct from ``config.configurable``.

    ``caused_by_run_id``/``cascade_depth`` (#18) are transported the same way
    as ``cord_subject``, under ``config.configurable``, so a graph that reads
    them can stamp its own ``cord.caused_by.run_id``/``cord.cascade.depth``
    span attributes (`cord_runtime.execution.run`) when it opens its Run.
    Neither is interpreted here; a graph that ignores them still runs.
    """
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("Subject must be a non-empty string")
    with requests.Session() as session:
        session.trust_env = False
        request = _session_request(session, endpoint)
        thread_id = request("POST", "/threads", {})["thread_id"]
        path = f"/threads/{thread_id}"
        configurable = {"cord_subject": subject, "cord_cascade_depth": cascade_depth}
        if caused_by_run_id is not None:
            configurable["cord_caused_by_run_id"] = caused_by_run_id
        run_body = {
            "assistant_id": assistant, "input": graph_input,
            "config": {"configurable": configurable},
            "stream_mode": ["values"],
        }
        if request_context is not None:
            run_body["context"] = request_context
        run_id = request("POST", path + "/runs", run_body)["run_id"]
        return _poll_until_settled(request, path, run_id, thread_id, timeout)


def resume(endpoint: str, thread_id: str, resume_value, *, timeout=120):
    """Continue an existing Thread through LangGraph's public ``command.resume``.

    A different contract from ``execute()``: no Thread is created, and
    ``resume_value`` is transported unread, exactly like ``execute()``'s
    ``request_context``, since interpreting an operator's answer is the
    graph/entity's job, not this client's. Aegra 0.10.4 assigns a new API Run
    ID even for a resume submission (ADR-0003 correction); mapping that ID
    back to the logical Run it continues is `cord_runtime.run_continuity`'s
    job, not this client's.

    The returned ``status`` has the same meaning as ``execute()``'s.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Thread ID must be a non-empty string")
    with requests.Session() as session:
        session.trust_env = False
        request = _session_request(session, endpoint)
        path = f"/threads/{thread_id}"
        run_id = request("POST", path + "/runs",
                         {"command": {"resume": resume_value}, "stream_mode": ["values"]})["run_id"]
        return _poll_until_settled(request, path, run_id, thread_id, timeout)


def cancel(endpoint: str, thread_id: str, run_id: str, *, timeout=10) -> dict:
    """Request the run's own public cancellation -- the platform's chosen halt
    action for an expired approval wait (#15), never a forged graph decision.

    A different contract from ``resume()``: this never submits
    ``command.resume``, so a timeout can never be mistaken for an operator's
    answer. The ``requests`` failure this raises (via the shared session
    helper) is payload-free but means the cancellation's outcome is unknown
    -- ambiguous transport -- and callers must not treat it as a confirmed
    halt.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Thread ID must be a non-empty string")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Run ID must be a non-empty string")
    with requests.Session() as session:
        session.trust_env = False
        request = _session_request(session, endpoint)
        request("POST", f"/threads/{thread_id}/runs/{run_id}/cancel")
        return {"run_id": run_id, "thread_id": thread_id, "status": "halted"}
