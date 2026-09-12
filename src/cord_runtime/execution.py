"""Run -> Step -> sibling Attempt instrumentation, without graph business logic."""

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context

SEMCONV_VERSION = "0.2.0"


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

    @contextmanager
    def attempt(self, number: int, tier: str, outcome: AttemptOutcome):
        if type(outcome) is not AttemptOutcome:
            raise TypeError("expected AttemptOutcome")
        if type(number) is not int or number < 1 or not tier:
            raise ValueError("attempt needs a positive number and a tier")
        attributes = {**self.attributes, "cord.step.attempt": number,
                      "cord.tier": tier, "cord.outcome": outcome.value}
        # Explicit Step parent keeps Attempts siblings even in nested contexts.
        with self.tracer.start_as_current_span(
            "attempt", context=trace.set_span_in_context(self.span, Context()),
            attributes=attributes, record_exception=False,
        ) as span:
            try:
                yield span
            except BaseException:
                span.set_attribute("cord.outcome", AttemptOutcome.FAILED.value)
                raise


@dataclass(frozen=True)
class Run:
    tracer: trace.Tracer
    span: trace.Span
    attributes: dict

    @contextmanager
    def step(self, node: str, outcome: StepOutcome):
        if type(outcome) is not StepOutcome:
            raise TypeError("expected StepOutcome")
        if not node:
            raise ValueError("node is required")
        attributes = {**self.attributes, "cord.node.name": node}
        with self.tracer.start_as_current_span(
            "step:" + node,
            context=trace.set_span_in_context(self.span, Context()),
            attributes={**attributes, "cord.outcome": outcome.value},
            record_exception=False,
        ) as span:
            try:
                yield Step(self.tracer, span, attributes)
            except BaseException:
                span.set_attribute("cord.outcome", StepOutcome.FAILED.value)
                raise


@contextmanager
def run(tracer: trace.Tracer, subject: str, subject_type: str, *, graph_id: str,
        run_id: str | None = None):
    if not isinstance(subject, str) or not subject.strip() or not subject_type:
        raise ValueError("Subject and type are required")
    if not isinstance(graph_id, str) or not graph_id.strip():
        raise ValueError("explicit Graph identity is required")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("Run identity must be a non-empty string")
    attributes = {"cord.graph.id": graph_id, "cord.run.id": run_id or str(uuid4()), "cord.subject.id": subject,
                  "cord.subject.type": subject_type}
    with tracer.start_as_current_span(
        "run", context=Context(), record_exception=False,
        attributes={**attributes, "cord.semconv.version": SEMCONV_VERSION},
    ) as span:
        yield Run(tracer, span, attributes)
