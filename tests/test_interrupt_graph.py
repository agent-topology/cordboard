"""End-to-end interrupt/resume through the real LangGraph public seam (#28).

No model, gateway, network, or Docker dependency: `InMemorySaver` stands in
for Aegra's checkpointer, and the logical Run identity is supplied by this
test, not inferred from any host-assigned API Run ID (#14 owns that mapping).
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from cord_runtime.execution import resume_run, run
from examples.interrupt_graph import build_graph


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def named(captured, name):
    return [s for s in captured.spans if s.name == name]


def test_interrupted_run_resumes_with_a_new_linked_step_span():
    provider = TracerProvider(resource=Resource({"service.name": "interrupt-fixture"}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    tracer = provider.get_tracer("interrupt-fixture")

    graph = build_graph(InMemorySaver())
    thread = {"configurable": {"thread_id": "issue-28"}}

    with run(tracer, "urn:cordboard:fixture:28", "fixture", graph_id="interrupt-fixture", run_id="r-28") as execution:
        continuation = execution.continuation
        paused = graph.invoke({}, {**thread, "configurable": {**thread["configurable"], "cord_active": execution}})

    assert "__interrupt__" in paused
    assert graph.get_state(thread).next == ("approve",)

    first, = named(captured, "step:approve")
    assert first.attributes["cord.outcome"] == "awaiting_approval"
    assert first.status.status_code is not StatusCode.ERROR
    resumed_from = format(first.context.span_id, "016x")

    with resume_run(tracer, continuation) as resumed:
        final = graph.invoke(
            Command(resume=True),
            {**thread, "configurable": {**thread["configurable"], "cord_active": resumed, "cord_resumed_from": resumed_from}},
        )

    assert final == {"approved": True}
    assert graph.get_state(thread).next == ()

    second = named(captured, "step:approve")[1]
    assert second.attributes["cord.resumed_from"] == resumed_from
    assert second.attributes["cord.outcome"] == "passed"
    assert second.context.trace_id == first.context.trace_id
    assert second.context.span_id != first.context.span_id
    assert second.status.status_code is not StatusCode.ERROR

    # Every span still carries the same Node/Graph/Subject identity Slice 0
    # relies on for grouping (ARCHITECTURE.md "Identity and execution records").
    for span in named(captured, "step:approve"):
        assert span.attributes["cord.node.name"] == "approve"
        assert span.attributes["cord.graph.id"] == "interrupt-fixture"
        assert span.attributes["cord.subject.id"] == "urn:cordboard:fixture:28"

    # One Run root, not one per invocation.
    assert len(named(captured, "run")) == 1

    provider.shutdown()
