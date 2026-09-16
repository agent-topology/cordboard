"""Fresh non-conversational executions through Aegra's public HTTP API."""

import json
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


def describe_run(endpoint: str, thread_id: str, run_id: str, *, timeout=10) -> dict:
    """Return one Run's own public identity: its Assistant and the Subject
    string it was submitted with, read back from ``GET .../runs/{run_id}``.

    Used to place a live Run in the viewer before any span exists for it --
    ``config.configurable.cord_subject`` is exactly the opaque string
    ``execute()``/``resume()`` transported unread; this call does not
    interpret it further. Raises the same payload-free ``RuntimeError`` as
    the rest of this client on an unreachable/non-2xx response.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Thread ID must be a non-empty string")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Run ID must be a non-empty string")
    with requests.Session() as session:
        session.trust_env = False
        request = _session_request(session, endpoint)
        run = request("GET", f"/threads/{thread_id}/runs/{run_id}")
        subject = (run.get("config") or {}).get("configurable", {}).get("cord_subject")
        return {"run_id": run_id, "thread_id": thread_id,
                "assistant_id": run["assistant_id"], "status": run["status"], "subject": subject}


def describe_assistant(endpoint: str, assistant_id: str, *, timeout=10) -> dict:
    """Return one Assistant's own public ``graph_id`` (`GET /assistants/{id}`).

    The same ``graph_id`` a graph stamps as ``cord.graph.id`` (ADR-0002/0003),
    letting a live Run (identified only by its Assistant) join the viewer's
    existing per-Graph catalog, exactly like `cord list`'s `/assistants` probe.
    """
    if not isinstance(assistant_id, str) or not assistant_id.strip():
        raise ValueError("Assistant ID must be a non-empty string")
    with requests.Session() as session:
        session.trust_env = False
        request = _session_request(session, endpoint)
        assistant = request("GET", f"/assistants/{assistant_id}")
        return {"graph_id": assistant["graph_id"]}


def _iter_sse_events(response):
    """Parse one raw SSE byte stream into ``(event, data_text, event_id)``
    frames, per the standard field/blank-line-terminated wire format Aegra's
    ``sse_starlette``-based endpoints emit. A comment line (Aegra's periodic
    ``: heartbeat`` keepalive) carries no field and is silently dropped."""
    event = None
    data_lines: list[str] = []
    event_id = None
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        if raw_line == "":
            if event is not None or data_lines:
                yield event or "message", "\n".join(data_lines), event_id
            event, data_lines, event_id = None, [], None
            continue
        if raw_line.startswith(":"):
            continue
        if raw_line.startswith("event:"):
            event = raw_line[len("event:"):].strip()
        elif raw_line.startswith("data:"):
            data_lines.append(raw_line[len("data:"):].strip())
        elif raw_line.startswith("id:"):
            event_id = raw_line[len("id:"):].strip()


_LIFECYCLE_EVENTS = frozenset({"metadata", "end", "error"})


def stream_lifecycle(endpoint: str, thread_id: str, run_id: str, *,
                      last_event_id: str | None = None, timeout: float = 120):
    """Yield one Run's public lifecycle events from Aegra's own reconnect-safe
    stream (``GET /threads/{thread_id}/runs/{run_id}/stream``), as
    ``(event, data, event_id)``.

    Only ``metadata`` (identity), ``end`` (terminal status) and ``error`` (a
    fixed, payload-free diagnostic) are yielded. ``values``/``updates``/
    ``messages*``/``debug`` frames carry graph business state and are
    dropped here, unparsed -- Cordboard's platform/graph boundary, not an
    optimization. Passing ``last_event_id`` (the last ``event_id`` a prior
    call yielded) asks Aegra to replay any events since that id before
    resuming live delivery, so a caller that persists it across a dropped
    connection sees neither a gap nor a duplicate (#20 AC3). This function
    makes exactly one HTTP connection; ``watch_lifecycle`` reconnects.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Thread ID must be a non-empty string")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Run ID must be a non-empty string")
    headers = {"Accept": "text/event-stream"}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    url = endpoint.rstrip("/") + f"/threads/{thread_id}/runs/{run_id}/stream"
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, headers=headers, stream=True, timeout=timeout) as response:
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(
                        "Aegra stream request failed; check service readiness and input contract")
                for event, data_text, event_id in _iter_sse_events(response):
                    if event not in _LIFECYCLE_EVENTS:
                        continue
                    try:
                        data = json.loads(data_text) if data_text else {}
                    except ValueError:
                        continue
                    yield event, data, event_id
    except requests.RequestException:
        raise RuntimeError("Aegra stream request failed; check service readiness and input contract") from None


def watch_lifecycle(endpoint: str, thread_id: str, run_id: str, *,
                     timeout: float = 120, reconnect_delay: float = 0.5):
    """Reconnect-safe generator over one Run's public lifecycle events.

    Wraps ``stream_lifecycle``: a transport-level drop before an ``end``/
    ``error`` event reopens the stream with the last ``event_id`` seen as
    ``Last-Event-ID``, so Aegra's own replay buffer fills the gap instead of
    the caller losing progress or fabricating a duplicate Run entry (#20).
    Stops once a terminal (``end``/``error``) event lands, or once
    ``timeout`` total seconds have elapsed since this call started.
    """
    deadline = time.monotonic() + timeout
    last_event_id = None
    saw_terminal = False
    while not saw_terminal and time.monotonic() < deadline:
        try:
            for event, data, event_id in stream_lifecycle(
                endpoint, thread_id, run_id, last_event_id=last_event_id,
                timeout=max(1.0, deadline - time.monotonic()),
            ):
                if event_id:
                    last_event_id = event_id
                yield event, data, event_id
                if event in ("end", "error"):
                    saw_terminal = True
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(reconnect_delay)
