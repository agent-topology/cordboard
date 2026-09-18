"""LangChain callback handler that records Cordboard Run/Step spans for a
LangGraph graph without any change to that graph's own code (#89).

This is deliberately its own distribution, not a `cord-runtime` extra:
`cord-runtime` pins `redact-secret==0.1.0b1`/`opentelemetry-sdk==1.39.1`
exactly, which an entity that has already resolved a different pin of either
(e.g. `redact-secret==0.1.0b4`) cannot co-install alongside it. This package
therefore declares only ranged `opentelemetry-api`/`langchain-core`
dependencies and never constructs or installs a TracerProvider or exporter --
the entity keeps its own redacted provider (ADR-0006, #28) and passes this
handler an already configured `Tracer`.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, Status, StatusCode, TraceFlags, Tracer

try:  # pragma: no cover - exercised whenever langgraph shares the process
    from langgraph.errors import GraphInterrupt as _GraphInterrupt
except ImportError:  # pragma: no cover - this package never pins langgraph
    _GraphInterrupt = None

#: Matches `cord_runtime.execution.SEMCONV_VERSION` (#86, ADR-0021). Kept as
#: a literal, not an import, because this distribution must not depend on
#: `cord-runtime` (see module docstring). The main repo's
#: `tests/test_langgraph_callbacks.py` asserts the two stay equal.
SEMCONV_VERSION = "0.4.0"

#: LangGraph tags exactly one callback-manager child per Pregel task with
#: `graph:step:<superstep>` (`langgraph.pregel._algo._task`, `inherit=False`
#: so it never leaks onto anything the task's own body invokes further), and
#: nothing else produces this prefix -- routing/trigger functions carry
#: `seq:step:*` instead. This is the seam #89's own verified evidence relies on.
_STEP_TAG = re.compile(r"^graph:step:\d+$")

#: LangGraph's own checkpoint-namespace separators
#: (`langgraph._internal._constants.NS_SEP`/`NS_END`). Not documented public
#: API, but the mechanism #89's verified evidence uses to derive nesting
#: without graph code changes; a LangGraph release that changes these would
#: need re-verification, the same as any other producer contract tracked in
#: `docs/decisions/cordboard-upstream-requirements.md`.
_NS_SEP = "|"


def _span_id(span: Span) -> str:
    return format(span.get_span_context().span_id, "016x")


def _is_graph_interrupt(error: BaseException) -> bool:
    if _GraphInterrupt is not None:
        return isinstance(error, _GraphInterrupt)
    # Defensive duck-typed fallback: langgraph must already be importable in
    # any real entity process (it is what runs the graph this handler
    # observes), so this only matters if import ordering ever made the
    # try/import above run before langgraph was on sys.path.
    return any(cls.__module__ == "langgraph.errors" and cls.__name__ == "GraphInterrupt"
               for cls in type(error).__mro__)


@dataclass(frozen=True)
class RunContinuation:
    """Safe correlation data for reopening a Run's trace after a pause.

    Same five plain strings as `cord_runtime.execution.RunContinuation`
    (duplicated, not imported -- see module docstring). An entity persists
    this itself, outside the graph's own checkpointed state (the graph is
    never touched), keyed by whatever identity it already uses to find the
    right Thread again (e.g. `cord_subject` plus its own thread id).
    """

    trace_id: str
    run_span_id: str
    run_id: str
    subject: str
    subject_type: str
    graph_id: str


@dataclass(frozen=True)
class AwaitingApproval:
    """One Step this handler closed as `awaiting_approval` this invocation.

    An entity that wants `cord.resumed_from` linkage on the resumed Step
    must persist `span_id` (keyed by its own Thread identity, alongside the
    `RunContinuation`) and supply it back as `resumed_from=` on the
    `CordCallbackHandler` it attaches to the resuming invocation. See
    docs/langgraph-callbacks.md#resume for why this -- not automatic
    discovery from callback events alone -- is the verified mechanism.
    """

    span_id: str
    node: str
    checkpoint_ns: str


@dataclass
class _OpenStep:
    span: Span
    attributes: dict[str, Any]
    checkpoint_ns: str


class CordCallbackHandler(BaseCallbackHandler):
    """Maps LangGraph `graph:step:N` chain events to Cordboard Run/Step spans.

    Construct one instance per entity invocation and attach it fresh through
    `config["callbacks"]`; never reuse an instance across two invocations.
    Either supply `graph_id`/`subject`/`subject_type` (a new logical Run) or
    `resume=` (reopen a previously recorded Run's trace, ADR-0008) -- never
    both. Call `close()` (or use this handler as a context manager) once the
    invocation returns, so the Run span this handler may have opened is
    actually ended and exported; nothing here hooks LangGraph's own root
    invocation event to infer that automatically.
    """

    def __init__(
        self,
        tracer: Tracer,
        *,
        graph_id: str | None = None,
        subject: str | None = None,
        subject_type: str | None = None,
        run_id: str | None = None,
        resume: RunContinuation | None = None,
        resumed_from: str | None = None,
    ) -> None:
        super().__init__()
        if resume is not None:
            if graph_id is not None or subject is not None or subject_type is not None or run_id is not None:
                raise ValueError("resume= reopens an existing Run; it replaces graph_id/subject/subject_type/run_id")
        else:
            if not graph_id or not isinstance(graph_id, str):
                raise ValueError("graph_id is required unless resume= is supplied")
            if not subject or not isinstance(subject, str):
                raise ValueError("subject is required unless resume= is supplied")
            if not subject_type or not isinstance(subject_type, str):
                raise ValueError("subject_type is required unless resume= is supplied")
        self._tracer = tracer
        self._graph_id = graph_id
        self._subject = subject
        self._subject_type = subject_type
        self._run_id = run_id
        self._resume = resume
        self._pending_resumed_from = resumed_from

        self._lock = threading.Lock()
        self._closed = False
        self._run_span: Span | None = None
        self._run_attributes: dict[str, Any] | None = None
        self._by_run_id: dict[UUID, _OpenStep] = {}
        self._by_checkpoint_ns: dict[str, _OpenStep] = {}
        self.awaiting_approval_steps: list[AwaitingApproval] = []

    @property
    def continuation(self) -> RunContinuation:
        """This Run's `RunContinuation`, once at least one Step has opened."""
        if self._run_span is None or self._run_attributes is None:
            raise RuntimeError("no Step has opened yet; this Run has no continuation to persist")
        ctx = self._run_span.get_span_context()
        return RunContinuation(
            trace_id=format(ctx.trace_id, "032x"),
            run_span_id=format(ctx.span_id, "016x"),
            run_id=self._run_attributes["cord.run.id"],
            subject=self._run_attributes["cord.subject.id"],
            subject_type=self._run_attributes["cord.subject.type"],
            graph_id=self._run_attributes["cord.graph.id"],
        )

    def _ensure_run_locked(self) -> None:
        if self._run_span is not None:
            return
        if self._resume is not None:
            context = SpanContext(
                trace_id=int(self._resume.trace_id, 16),
                span_id=int(self._resume.run_span_id, 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            self._run_span = NonRecordingSpan(context)
            self._run_attributes = {
                "cord.graph.id": self._resume.graph_id,
                "cord.run.id": self._resume.run_id,
                "cord.subject.id": self._resume.subject,
                "cord.subject.type": self._resume.subject_type,
            }
            return
        attributes = {
            "cord.graph.id": self._graph_id,
            "cord.run.id": self._run_id or str(uuid4()),
            "cord.subject.id": self._subject,
            "cord.subject.type": self._subject_type,
        }
        self._run_span = self._tracer.start_span(
            "run", context=Context(), attributes={**attributes, "cord.semconv.version": SEMCONV_VERSION},
        )
        self._run_attributes = attributes

    @staticmethod
    def _step_tagged(tags: list[str] | None) -> bool:
        return any(_STEP_TAG.match(tag) for tag in (tags or ()))

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                        tags=None, metadata=None, **kwargs) -> None:
        if not self._step_tagged(tags):
            return
        metadata = metadata or {}
        node = metadata.get("langgraph_node")
        checkpoint_ns = metadata.get("langgraph_checkpoint_ns")
        if not node or not checkpoint_ns:
            return  # Tagged but not a Pregel task event; nothing to attach.
        with self._lock:
            if self._closed:
                return
            self._ensure_run_locked()
            parent_key = checkpoint_ns.rsplit(_NS_SEP, 1)[0] if _NS_SEP in checkpoint_ns else None
            parent_open = self._by_checkpoint_ns.get(parent_key) if parent_key is not None else None
            # LangGraph always finishes firing a parent task's own
            # on_chain_start before any subgraph task it calls can start, so
            # `parent_open` is missing only if that invariant broke; fall
            # back to the Run root rather than dropping the Step.
            parent_span = parent_open.span if parent_open is not None else self._run_span
            resumed_from = self._pending_resumed_from
            self._pending_resumed_from = None
            attributes = {**self._run_attributes, "cord.node.name": node}
            if resumed_from is not None:
                attributes["cord.resumed_from"] = resumed_from
            span = self._tracer.start_span(
                "step:" + node,
                context=trace.set_span_in_context(parent_span, Context()),
                attributes={**attributes, "cord.outcome": "passed"},
            )
            open_step = _OpenStep(span=span, attributes=attributes, checkpoint_ns=checkpoint_ns)
            self._by_run_id[run_id] = open_step
            self._by_checkpoint_ns[checkpoint_ns] = open_step

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs) -> None:
        with self._lock:
            open_step = self._by_run_id.pop(run_id, None)
            if open_step is not None:
                self._by_checkpoint_ns.pop(open_step.checkpoint_ns, None)
        if open_step is not None:
            open_step.span.end()

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        with self._lock:
            open_step = self._by_run_id.pop(run_id, None)
            if open_step is None:
                return
            self._by_checkpoint_ns.pop(open_step.checkpoint_ns, None)
            if _is_graph_interrupt(error):
                # ADR-0008: a controlled pause, not a failure. Leave the span
                # status non-error, matching `cord_runtime.execution`'s own
                # `_open_step` rule for the graph-code-instrumented path.
                open_step.span.set_attribute("cord.outcome", "awaiting_approval")
                self.awaiting_approval_steps.append(AwaitingApproval(
                    span_id=_span_id(open_step.span), node=open_step.attributes["cord.node.name"],
                    checkpoint_ns=open_step.checkpoint_ns,
                ))
            else:
                open_step.span.set_attribute("cord.outcome", "failed")
                open_step.span.set_status(Status(StatusCode.ERROR))
        open_step.span.end()

    def close(self) -> None:
        """End the Run span (if one opened) and any Step this instance never
        saw closed -- e.g. the host process crashed mid-node without a
        matching `on_chain_end`/`on_chain_error`. Idempotent; safe to call
        even if no `graph:step` event ever fired (ADR-0015: recording needs
        spans, so a Run with none is simply never recorded).
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            leaked = list(self._by_run_id.values())
            self._by_run_id.clear()
            self._by_checkpoint_ns.clear()
            run_span = self._run_span
        for open_step in leaked:
            open_step.span.set_attribute("cord.outcome", "failed")
            open_step.span.set_status(Status(StatusCode.ERROR))
            open_step.span.end()
        if run_span is not None:
            run_span.end()

    def __enter__(self) -> "CordCallbackHandler":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
