"""Fresh non-conversational executions through Aegra's public HTTP API."""

import time

import requests


def execute(endpoint: str, assistant: str, subject: str, graph_input: dict, *,
            request_context: dict | None = None, timeout=120):
    """Return API identities and a bounded status, without logging payloads.

    Each call creates a new Thread. Resume and API retries are deliberately not
    exposed: resubmitting a POST can create another execution.

    The returned ``status`` is one of ``"success"`` (``values`` present),
    ``"waiting"`` (an interrupted Run, or the wait budget elapsed while it was
    still pending/running; the Run may still be active — poll or resume with
    the returned identities), or an exception for a genuine failure.
    ``request_context`` is transported unread as the API's top-level
    ``context``, distinct from ``config.configurable``.
    """
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("Subject must be a non-empty string")
    with requests.Session() as session:
        session.trust_env = False

        def request(method, path, body=None):
            try:
                response = session.request(method, endpoint.rstrip("/") + path,
                                           json=body, timeout=10, allow_redirects=False)
                if not 200 <= response.status_code < 300:
                    raise ValueError
                return response.json()
            except (requests.RequestException, ValueError):
                raise RuntimeError("Aegra API request failed; check service readiness and input contract") from None

        thread_id = request("POST", "/threads", {})["thread_id"]
        path = f"/threads/{thread_id}"
        run_body = {
            "assistant_id": assistant, "input": graph_input,
            "config": {"configurable": {"cord_subject": subject}},
            "stream_mode": ["values"],
        }
        if request_context is not None:
            run_body["context"] = request_context
        run_id = request("POST", path + "/runs", run_body)["run_id"]
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
