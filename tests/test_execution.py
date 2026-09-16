"""Interrupt/resume span boundaries (ADR-0008 interrupt addendum, #28).

Uses a plain exception as the "pause" signal so these stay independent of any
graph framework; `tests/test_interrupt_graph.py` covers the real LangGraph
`interrupt()`/`Command(resume=...)` seam end to end.
"""

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode
import pytest

from cord_runtime.execution import AttemptOutcome, StepOutcome, resume_run, run, span_id


class Paused(Exception):
    pass


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


@pytest.fixture
def traced():
    provider = TracerProvider(resource=Resource({}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    tracer = provider.get_tracer("test")
    yield tracer, captured
    provider.shutdown()


def named(captured, name):
    return [s for s in captured.spans if s.name == name]


def test_interrupt_closes_attempt_then_step_as_awaiting_approval_without_error(traced):
    tracer, captured = traced
    with run(tracer, "urn:test:28", "fixture", graph_id="interrupt-fixture") as execution:
        with pytest.raises(Paused):
            with execution.step("approve", StepOutcome.PASSED, pause=(Paused,)) as step:
                with step.attempt(1, outcome=AttemptOutcome.PASSED):
                    raise Paused("awaiting human")

    step_span, = named(captured, "step:approve")
    attempt_span, = named(captured, "attempt")
    assert step_span.attributes["cord.outcome"] == StepOutcome.AWAITING_APPROVAL.value
    assert step_span.status.status_code is not StatusCode.ERROR
    # The graph's own declared outcome survives a pause; cord_runtime never
    # fabricates an Attempt-level disposition for it.
    assert attempt_span.attributes["cord.outcome"] == AttemptOutcome.PASSED.value
    assert attempt_span.status.status_code is not StatusCode.ERROR
    # Nesting alone guarantees the Attempt closes no later than its Step.
    assert attempt_span.end_time <= step_span.end_time


def test_resume_opens_new_step_span_linked_to_predecessor_on_same_trace(traced):
    tracer, captured = traced
    with run(tracer, "urn:test:28", "fixture", graph_id="interrupt-fixture") as execution:
        continuation = execution.continuation
        with pytest.raises(Paused):
            with execution.step("approve", StepOutcome.PASSED, pause=(Paused,)):
                raise Paused()

    first, = named(captured, "step:approve")
    previous_id = format(first.context.span_id, "016x")

    with resume_run(tracer, continuation) as resumed:
        with resumed.step("approve", StepOutcome.PASSED, resumed_from=previous_id):
            pass

    second = named(captured, "step:approve")[1]
    assert second.context.span_id != first.context.span_id
    assert second.context.trace_id == first.context.trace_id
    assert second.attributes["cord.resumed_from"] == previous_id
    assert second.attributes["cord.outcome"] == StepOutcome.PASSED.value
    # Resume never recreates a Run root: exactly one "run" span for both calls.
    assert len(named(captured, "run")) == 1


def test_controlled_pause_does_not_inflate_either_step_duration(traced, monkeypatch):
    tracer, captured = traced
    now = [1_000_000_000_000]

    def fake_time_ns():
        return now[0]

    monkeypatch.setattr("opentelemetry.sdk.trace.time_ns", fake_time_ns)

    with run(tracer, "urn:test:28", "fixture", graph_id="interrupt-fixture") as execution:
        continuation = execution.continuation
        with pytest.raises(Paused):
            with execution.step("approve", StepOutcome.PASSED, pause=(Paused,)):
                now[0] += 1_000_000  # 1ms of real work before the pause
                raise Paused()

    first, = named(captured, "step:approve")
    previous_id = format(first.context.span_id, "016x")

    now[0] += 999_999_999_999  # an unbounded human pause, injected without sleeping

    with resume_run(tracer, continuation) as resumed:
        with resumed.step("approve", StepOutcome.PASSED, resumed_from=previous_id):
            now[0] += 2_000_000  # 2ms of real work after resume

    second = named(captured, "step:approve")[1]
    assert first.end_time - first.start_time == 1_000_000
    assert second.end_time - second.start_time == 2_000_000


def test_real_failure_still_marks_failed_with_error_status(traced):
    tracer, _captured = traced
    with run(tracer, "urn:test:28", "fixture", graph_id="interrupt-fixture") as execution:
        with pytest.raises(RuntimeError):
            with execution.step("approve", StepOutcome.PASSED) as step:
                with step.attempt(1, outcome=AttemptOutcome.PASSED):
                    raise RuntimeError("boom")
    step_span, = [s for s in _captured.spans if s.name == "step:approve"]
    attempt_span, = [s for s in _captured.spans if s.name == "attempt"]
    assert step_span.attributes["cord.outcome"] == StepOutcome.FAILED.value
    assert step_span.status.status_code is StatusCode.ERROR
    assert attempt_span.attributes["cord.outcome"] == AttemptOutcome.FAILED.value
    assert attempt_span.status.status_code is StatusCode.ERROR


def test_span_id_is_the_hex_form_of_the_live_span_id(traced):
    tracer, captured = traced
    with run(tracer, "urn:test:28", "fixture", graph_id="interrupt-fixture") as execution:
        with execution.step("approve", StepOutcome.PASSED) as step:
            recorded = span_id(step.span)
    step_span, = named(captured, "step:approve")
    assert recorded == format(step_span.context.span_id, "016x")


@pytest.mark.parametrize("resumed_from", ["", "   "])
def test_resumed_from_must_be_a_non_empty_string(traced, resumed_from):
    tracer, _captured = traced
    with run(tracer, "urn:test:28", "fixture", graph_id="interrupt-fixture") as execution:
        with pytest.raises(ValueError, match="resumed_from"):
            with execution.step("approve", StepOutcome.PASSED, resumed_from=resumed_from):
                pass


# --- cascade identity on the Run root span (#18) ----------------------------

def test_a_root_run_defaults_cascade_depth_to_zero_with_no_caused_by(traced):
    tracer, captured = traced
    with run(tracer, "urn:test:18", "fixture", graph_id="graph-a"):
        pass
    root, = named(captured, "run")
    assert root.attributes["cord.cascade.depth"] == 0
    assert "cord.caused_by.run_id" not in root.attributes


def test_a_cascade_child_run_stamps_caused_by_and_depth_on_its_new_trace(traced):
    tracer, captured = traced
    with run(tracer, "urn:test:18", "fixture", graph_id="graph-b",
             run_id="child-run", caused_by_run_id="parent-run", cascade_depth=1):
        pass
    parent_root, = named(captured, "run")
    assert parent_root.attributes["cord.caused_by.run_id"] == "parent-run"
    assert parent_root.attributes["cord.cascade.depth"] == 1
    assert parent_root.attributes["cord.run.id"] == "child-run"


def test_run_rejects_blank_caused_by_run_id(traced):
    tracer, _captured = traced
    with pytest.raises(ValueError, match="caused_by_run_id"):
        with run(tracer, "urn:test:18", "fixture", graph_id="graph-b", caused_by_run_id="   "):
            pass


def test_run_rejects_negative_cascade_depth(traced):
    tracer, _captured = traced
    with pytest.raises(ValueError, match="cascade_depth"):
        with run(tracer, "urn:test:18", "fixture", graph_id="graph-b", cascade_depth=-1):
            pass
