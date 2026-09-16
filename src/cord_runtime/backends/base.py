"""Identity types, closed status vocabularies, and the abstract contract
every ExecutionBackend implements (ADR-0016 §2, ADR-0017).

``RuntimeStatus`` describes what the execution is doing; ``ClientWaitOutcome``
describes why a wait call returned when it did. They are deliberately
separate closed lists so a client wait deadline can never be mistaken for an
interrupt or a cancellation, and so a real failure comes back as a value
with its identity intact instead of an exception that discards it.

``LogicalRunId``, ``InvocationId``, ``ThreadId`` and ``TraceId`` are distinct
identity spaces (ADR-0017 §1): ``LogicalRunId``/``InvocationId``/``ThreadId``
are control-plane bookkeeping known before any span exists; ``TraceId`` is
the graph's own OTel trace (``cord_runtime.execution.run``). They correlate
only through the Subject/Assistant/Thread a caller already carries, never by
shared value.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterator, NewType, Protocol

ThreadId = NewType("ThreadId", str)
InvocationId = NewType("InvocationId", str)
LogicalRunId = NewType("LogicalRunId", str)
TraceId = NewType("TraceId", str)


class RuntimeStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ClientWaitOutcome(Enum):
    """Why a wait call returned -- never conflated with ``RuntimeStatus``."""

    COMPLETED = "completed"
    DEADLINE_REACHED = "deadline_reached"
    TRANSPORT_ERROR = "transport_error"


@dataclass(frozen=True)
class ExecutionResult:
    """The default backend result: identity and status only.

    Output (the graph's checkpoint state) is never included here -- call the
    backend's own ``get_state``/output-retrieval operation explicitly
    (ADR-0017 §3). ``logical_run_id`` is ``None`` when the backend has no
    durable continuity store wired in and cannot determine it (e.g. a
    ``resume`` without ``board_dir``/``deployment`` context).
    """

    logical_run_id: LogicalRunId | None
    invocation_id: InvocationId
    thread_id: ThreadId
    assistant_id: str
    status: RuntimeStatus
    wait_outcome: ClientWaitOutcome | None = None


class ExecutionBackend(Protocol):
    """The minimal contract every execution backend implements.

    A backend that cannot support an operation omits it from
    ``capabilities`` and raises ``NotImplementedError`` if called anyway --
    no fake equivalence between backends (ADR-0016 §2).
    """

    capabilities: frozenset[str]

    def list_assistants(self) -> list[dict]: ...

    def execute(self, assistant: str, subject: str, graph_input: dict, *,
                request_context: dict | None = None, timeout: float = 120,
                caused_by_run_id: str | None = None, cascade_depth: int = 0) -> ExecutionResult: ...

    def status(self, thread_id: ThreadId, invocation_id: InvocationId) -> RuntimeStatus: ...

    def resume(self, thread_id: ThreadId, assistant: str, resume_value, *,
               timeout: float = 120) -> ExecutionResult: ...

    def cancel(self, thread_id: ThreadId, invocation_id: InvocationId) -> RuntimeStatus: ...

    def watch(self, thread_id: ThreadId, invocation_id: InvocationId, *,
              timeout: float = 120) -> Iterator[tuple[str, dict, str | None]]: ...
