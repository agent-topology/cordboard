"""The Aegra adapter behind the ExecutionBackend boundary (ADR-0016 §2,
ADR-0017): the first backend, decomposing Aegra's public HTTP/SSE API into
``create_thread``/``submit_run``/``get_run``/``get_state``/``wait_for_run``
and exposing the common ``list_assistants``/``execute``/``status``/
``resume``/``cancel``/``watch`` contract on top.

``cord_runtime.aegra_client``'s free functions (``execute``/``resume``/
``cancel``/``describe_run``/``describe_assistant``/``watch_lifecycle``) are
unchanged and reused here as already-verified primitives for the operations
they already implement correctly (status/discovery/cancel/watch transport);
this module adds the decomposition (``create_thread``/``submit_run``/
``wait_for_run``/``get_state``) and the new RuntimeStatus/ClientWaitOutcome/
LogicalRunId vocabulary those free functions do not expose.
"""

from pathlib import Path
import time

import requests

from cord_runtime.aegra_client import (
    cancel as _client_cancel,
    describe_assistant as _client_describe_assistant,
    describe_run as _client_describe_run,
    watch_lifecycle as _client_watch_lifecycle,
)
from cord_runtime.backends.base import (
    ClientWaitOutcome,
    ExecutionResult,
    InvocationId,
    LogicalRunId,
    RuntimeStatus,
    ThreadId,
)
from cord_runtime.run_continuity import record_submission

_POLL_INTERVAL = 0.1

_RAW_STATUS = {
    "pending": RuntimeStatus.QUEUED,
    "running": RuntimeStatus.RUNNING,
    "interrupted": RuntimeStatus.INTERRUPTED,
    "success": RuntimeStatus.SUCCEEDED,
    "error": RuntimeStatus.FAILED,
    "cancelled": RuntimeStatus.CANCELLED,
}

_WAITING_RAW_STATUSES = ("pending", "running")


def _session_request(session: requests.Session, endpoint: str):
    def request(method, path, body=None):
        try:
            response = session.request(method, endpoint.rstrip("/") + path, json=body,
                                       timeout=10, allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise ValueError
            return response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("Aegra API request failed; check service readiness and input contract") from None
    return request


class AegraExecutionBackend:
    """The Aegra ExecutionBackend adapter.

    ``board_dir``/``deployment`` are optional: when both are given,
    ``execute``/``resume`` durably record the (Thread, Invocation) mapping
    through ``cord_runtime.run_continuity`` so ``logical_run_id`` survives a
    resume across process restarts. Without them, ``execute``'s
    ``logical_run_id`` still reflects the correct in-process value -- a fresh
    Thread's first Invocation is definitionally its own logical Run -- but
    ``resume``'s prior logical Run id cannot be looked up and comes back
    ``None``.
    """

    capabilities = frozenset({"list_assistants", "execute", "status", "resume", "cancel", "watch"})

    def __init__(self, endpoint: str, *, board_dir: Path | None = None, deployment: str | None = None):
        self.endpoint = endpoint
        self._board_dir = board_dir
        self._deployment = deployment

    def _request(self):
        session = requests.Session()
        session.trust_env = False
        return session, _session_request(session, self.endpoint)

    def create_thread(self) -> ThreadId:
        session, request = self._request()
        with session:
            return ThreadId(request("POST", "/threads", {})["thread_id"])

    def submit_run(self, thread_id: ThreadId, assistant: str, graph_input: dict, *,
                    configurable: dict, context: dict | None = None) -> InvocationId:
        session, request = self._request()
        with session:
            body = {"assistant_id": assistant, "input": graph_input,
                     "config": {"configurable": configurable}, "stream_mode": ["values"]}
            if context is not None:
                body["context"] = context
            return InvocationId(request("POST", f"/threads/{thread_id}/runs", body)["run_id"])

    def submit_resume(self, thread_id: ThreadId, assistant: str, resume_value) -> InvocationId:
        session, request = self._request()
        with session:
            body = {"assistant_id": assistant, "command": {"resume": resume_value}, "stream_mode": ["values"]}
            return InvocationId(request("POST", f"/threads/{thread_id}/runs", body)["run_id"])

    def get_run(self, thread_id: ThreadId, invocation_id: InvocationId) -> dict:
        """Raw ``GET .../runs/{invocation_id}`` result: identity, transported
        Subject, and the *unmapped* Aegra status string -- ``status()``/
        ``wait_for_run`` map it to ``RuntimeStatus``."""
        return _client_describe_run(self.endpoint, thread_id, invocation_id)

    def get_state(self, thread_id: ThreadId) -> dict:
        """Explicit, separately-governed output retrieval (ADR-0017 §3):
        never called by default from ``execute``/``resume``/``status``."""
        session, request = self._request()
        with session:
            return request("GET", f"/threads/{thread_id}/state")

    def wait_for_run(self, thread_id: ThreadId, invocation_id: InvocationId,
                      timeout: float) -> tuple[RuntimeStatus, ClientWaitOutcome]:
        """Poll until settled or ``timeout`` elapses.

        A deadline reached while still queued/running returns that exact
        status with ``DEADLINE_REACHED`` -- never ``INTERRUPTED`` or
        ``CANCELLED`` (AC1). An actual interrupt, success, or terminal
        failure returns immediately as a value, with ``COMPLETED``, never as
        an exception that would discard the Thread/Invocation handle. A
        transport failure mid-poll is likewise returned as a value --
        ``UNKNOWN``/``TRANSPORT_ERROR`` -- rather than raised, and is not
        retried.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                raw_status = self.get_run(thread_id, invocation_id)["status"]
            except RuntimeError:
                return RuntimeStatus.UNKNOWN, ClientWaitOutcome.TRANSPORT_ERROR
            if raw_status in _WAITING_RAW_STATUSES:
                if time.monotonic() >= deadline:
                    return _RAW_STATUS[raw_status], ClientWaitOutcome.DEADLINE_REACHED
                time.sleep(_POLL_INTERVAL)
                continue
            return _RAW_STATUS.get(raw_status, RuntimeStatus.UNKNOWN), ClientWaitOutcome.COMPLETED

    def status(self, thread_id: ThreadId, invocation_id: InvocationId) -> RuntimeStatus:
        return _RAW_STATUS.get(self.get_run(thread_id, invocation_id)["status"], RuntimeStatus.UNKNOWN)

    def list_assistants(self) -> list[dict]:
        session, request = self._request()
        with session:
            return request("GET", "/assistants").get("assistants", [])

    def _logical_run_id_for_execute(self, thread_id: ThreadId, invocation_id: InvocationId) -> LogicalRunId:
        if self._board_dir is not None and self._deployment is not None:
            return LogicalRunId(record_submission(self._board_dir, self._deployment, thread_id, invocation_id))
        return LogicalRunId(invocation_id)

    def _logical_run_id_for_resume(self, thread_id: ThreadId, invocation_id: InvocationId) -> LogicalRunId | None:
        if self._board_dir is not None and self._deployment is not None:
            return LogicalRunId(record_submission(self._board_dir, self._deployment, thread_id, invocation_id))
        return None

    def execute(self, assistant: str, subject: str, graph_input: dict, *,
                request_context: dict | None = None, timeout: float = 120,
                caused_by_run_id: str | None = None, cascade_depth: int = 0) -> ExecutionResult:
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("Subject must be a non-empty string")
        thread_id = self.create_thread()
        configurable = {"cord_subject": subject, "cord_cascade_depth": cascade_depth}
        if caused_by_run_id is not None:
            configurable["cord_caused_by_run_id"] = caused_by_run_id
        invocation_id = self.submit_run(thread_id, assistant, graph_input,
                                        configurable=configurable, context=request_context)
        status, wait_outcome = self.wait_for_run(thread_id, invocation_id, timeout)
        return ExecutionResult(
            logical_run_id=self._logical_run_id_for_execute(thread_id, invocation_id),
            invocation_id=invocation_id, thread_id=thread_id, assistant_id=assistant,
            status=status, wait_outcome=wait_outcome,
        )

    def resume(self, thread_id: ThreadId, assistant: str, resume_value, *, timeout: float = 120) -> ExecutionResult:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("Thread ID must be a non-empty string")
        if not isinstance(assistant, str) or not assistant.strip():
            raise ValueError("Assistant must be a non-empty string")
        invocation_id = self.submit_resume(thread_id, assistant, resume_value)
        status, wait_outcome = self.wait_for_run(thread_id, invocation_id, timeout)
        return ExecutionResult(
            logical_run_id=self._logical_run_id_for_resume(thread_id, invocation_id),
            invocation_id=invocation_id, thread_id=thread_id, assistant_id=assistant,
            status=status, wait_outcome=wait_outcome,
        )

    def cancel(self, thread_id: ThreadId, invocation_id: InvocationId) -> RuntimeStatus:
        _client_cancel(self.endpoint, thread_id, invocation_id)
        return RuntimeStatus.CANCELLED

    def watch(self, thread_id: ThreadId, invocation_id: InvocationId, *, timeout: float = 120):
        yield from _client_watch_lifecycle(self.endpoint, thread_id, invocation_id, timeout=timeout)

    # Alias kept for callers migrating off `aegra_client.describe_run` --
    # identical to `get_run`: identity, raw status string, transported Subject.
    describe_run = get_run

    def describe_assistant(self, assistant_id: str) -> dict:
        return _client_describe_assistant(self.endpoint, assistant_id)
