"""Run -> Step -> sibling Attempt instrumentation, without graph business logic."""

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

SEMCONV_VERSION = "0.3.0"


def span_id(span: trace.Span) -> str:
    """Format a live span's own ID for persistence as a future ``cord.resumed_from``."""
    return format(span.get_span_context().span_id, "016x")


class StepOutcome(Enum):
    PASSED = "passed"
    REPAIRED = "repaired"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"
    HALTED = "halted"


class AttemptOutcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class Step:
    tracer: trace.Tracer
    span: trace.Span
    attributes: dict
    pause: tuple[type[BaseException], ...] = ()

    @contextmanager
    def attempt(self, number: int, tier: str | None = None,
                outcome: AttemptOutcome = AttemptOutcome.FAILED):
        """Record a try; optional Tier is an opaque graph-supplied annotation.

        Default to failed until the graph explicitly records success. Existing
        positional (number, tier, outcome) calls remain supported.
        """
        if type(outcome) is not AttemptOutcome:
            raise TypeError("expected AttemptOutcome")
        if type(number) is not int or number < 1:
            raise ValueError("attempt needs a positive number")
        if tier is not None and (not isinstance(tier, str) or not tier.strip()):
            raise ValueError("optional tier must be a non-empty string")
        attributes = {**self.attributes, "cord.step.attempt": number,
                      "cord.outcome": outcome.value}
        if tier is not None:
            attributes["cord.tier"] = tier
        # Explicit Step parent keeps Attempts siblings even in nested contexts.
        with self.tracer.start_as_current_span(
            "attempt", context=trace.set_span_in_context(self.span, Context()),
            attributes=attributes, record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except self.pause:
                # A controlled pause (e.g. the graph's own interrupt()) is not a
                # failure; leave whatever outcome the graph already declared and
                # let the enclosing Step close next, before this exception
                # continues upward to actually suspend the host run.
                raise
            except BaseException:
                span.set_attribute("cord.outcome", AttemptOutcome.FAILED.value)
                span.set_status(Status(StatusCode.ERROR))
                raise


@dataclass(frozen=True)
class RunContinuation:
    """Safe correlation data for reopening a Run's trace after a pause.

    Every field is a plain string, so a graph may persist this as ordinary
    checkpointed state; it carries no secrets and no upstream API Run ID.
    """

    trace_id: str
    run_span_id: str
    run_id: str
    subject: str
    subject_type: str
    graph_id: str


@dataclass(frozen=True)
class Run:
    tracer: trace.Tracer
    span: trace.Span
    attributes: dict

    @property
    def continuation(self) -> RunContinuation:
        context = self.span.get_span_context()
        return RunContinuation(
            trace_id=format(context.trace_id, "032x"),
            run_span_id=span_id(self.span),
            run_id=self.attributes["cord.run.id"],
            subject=self.attributes["cord.subject.id"],
            subject_type=self.attributes["cord.subject.type"],
            graph_id=self.attributes["cord.graph.id"],
        )

    @contextmanager
    def step(self, node: str, outcome: StepOutcome, *,
             resumed_from: str | None = None, pause: tuple[type[BaseException], ...] = ()):
        if type(outcome) is not StepOutcome:
            raise TypeError("expected StepOutcome")
        if not node:
            raise ValueError("node is required")
        if resumed_from is not None and (not isinstance(resumed_from, str) or not resumed_from.strip()):
            raise ValueError("resumed_from must be a non-empty span ID string")
        attributes = {**self.attributes, "cord.node.name": node}
        if resumed_from is not None:
            attributes["cord.resumed_from"] = resumed_from
        with self.tracer.start_as_current_span(
            "step:" + node,
            context=trace.set_span_in_context(self.span, Context()),
            attributes={**attributes, "cord.outcome": outcome.value},
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                yield Step(self.tracer, span, attributes, pause)
            except pause:
                # ADR-0008: interrupt() closes the Step here as awaiting_approval,
                # not an error. Resume opens a new Step span; it never reopens
                # this one, so the pause is never counted as this Step's duration.
                span.set_attribute("cord.outcome", StepOutcome.AWAITING_APPROVAL.value)
                raise
            except BaseException:
                span.set_attribute("cord.outcome", StepOutcome.FAILED.value)
                span.set_status(Status(StatusCode.ERROR))
                raise


@contextmanager
def run(tracer: trace.Tracer, subject: str, subject_type: str, *, graph_id: str,
        run_id: str | None = None, caused_by_run_id: str | None = None, cascade_depth: int = 0):
    """Open a new Run's trace.

    ``caused_by_run_id``/``cascade_depth`` (#18) record this Run's cascade
    identity -- which Run's `run.finished` Signal caused this one, and how
    many cascade hops deep it is (0: not a cascade child). Each call opens a
    brand-new trace; chaining never reopens a prior Run's trace the way
    ``resume_run`` does.
    """
    if not isinstance(subject, str) or not subject.strip() or not subject_type:
        raise ValueError("Subject and type are required")
    if not isinstance(graph_id, str) or not graph_id.strip():
        raise ValueError("explicit Graph identity is required")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("Run identity must be a non-empty string")
    if caused_by_run_id is not None and (not isinstance(caused_by_run_id, str) or not caused_by_run_id.strip()):
        raise ValueError("caused_by_run_id must be a non-empty string")
    if not isinstance(cascade_depth, int) or isinstance(cascade_depth, bool) or cascade_depth < 0:
        raise ValueError("cascade_depth must be a non-negative integer")
    attributes = {"cord.graph.id": graph_id, "cord.run.id": run_id or str(uuid4()), "cord.subject.id": subject,
                  "cord.subject.type": subject_type, "cord.cascade.depth": cascade_depth}
    if caused_by_run_id is not None:
        attributes["cord.caused_by.run_id"] = caused_by_run_id
    with tracer.start_as_current_span(
        "run", context=Context(), record_exception=False,
        attributes={**attributes, "cord.semconv.version": SEMCONV_VERSION},
    ) as span:
        yield Run(tracer, span, attributes)


@contextmanager
def resume_run(tracer: trace.Tracer, continuation: RunContinuation):
    """Reopen a Run's trace from persisted `RunContinuation` for a new Step.

    Never re-creates the "run" root span: the Run's one trace (ADR-0002) stays
    open only as address information, so a resumed Step attaches to the same
    trace_id without a second Run root and without requiring the original
    process, host Run, or in-memory Run span to still exist.
    """
    context = SpanContext(
        trace_id=int(continuation.trace_id, 16),
        span_id=int(continuation.run_span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    attributes = {
        "cord.graph.id": continuation.graph_id,
        "cord.run.id": continuation.run_id,
        "cord.subject.id": continuation.subject,
        "cord.subject.type": continuation.subject_type,
    }
    yield Run(tracer, NonRecordingSpan(context), attributes)
