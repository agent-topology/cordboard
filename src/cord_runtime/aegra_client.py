"""Fresh non-conversational executions through Aegra's public HTTP API."""

import time

import requests


def execute(endpoint: str, assistant: str, subject: str, graph_input: dict, *, timeout=120):
    """Return API identities and checkpointed values, without logging payloads.

    Each call creates a new Thread. Resume and API retries are deliberately not
    exposed: resubmitting a POST can create another execution.
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
        created = request("POST", path + "/runs", {
            "assistant_id": assistant, "input": graph_input,
            "config": {"configurable": {"cord_subject": subject}},
            "stream_mode": ["values"],
        })
        run_id = created["run_id"]
        deadline = time.monotonic() + timeout
        while True:
            current = request("GET", path + "/runs/" + run_id)
            if current["status"] == "success":
                values = request("GET", path + "/state")["values"]
                return {"run_id": run_id, "thread_id": thread_id, "values": values}
            if current["status"] not in ("pending", "running"):
                raise RuntimeError("Aegra execution did not succeed; inspect metadata and archive")
            if time.monotonic() >= deadline:
                raise TimeoutError("Aegra execution wait timed out; the run may still be active")
            time.sleep(.1)
