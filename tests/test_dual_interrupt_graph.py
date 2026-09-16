"""Two distinguishable pending interrupts on one Thread, resolved by id (#44).

No model, gateway, network, or Docker dependency: `InMemorySaver` stands in
for Aegra's checkpointer, exactly like `test_interrupt_graph.py`. This proves
the runtime-interrupt-identity fact #44's contract rests on against the real
LangGraph public seam, not just the pinned source inspected while planning.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from cord_runtime.execution import resume_run, run
from examples.dual_interrupt_graph import build_graph


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def named(captured, name):
    return [s for s in captured.spans if s.name == name]


def _state_interrupts(graph, thread):
    found = []
    for task in graph.get_state(thread).tasks:
        for interrupt in task.interrupts:
            found.append(interrupt)
    return found


def test_two_parallel_branches_produce_two_distinct_interrupt_ids():
    counter = [0]
    graph = build_graph(InMemorySaver(), counter)
    thread = {"configurable": {"thread_id": "issue-44-ids"}}

    graph.invoke({}, thread)

    interrupts = _state_interrupts(graph, thread)
    assert len(interrupts) == 2
    ids = {i.id for i in interrupts}
    assert len(ids) == 2  # distinguishable, not a shared/ambiguous pause


def test_resuming_both_interrupts_by_id_charges_exactly_once():
    provider = TracerProvider(resource=Resource({"service.name": "dual-interrupt-fixture"}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    tracer = provider.get_tracer("dual-interrupt-fixture")

    counter = [0]
    graph = build_graph(InMemorySaver(), counter)
    thread = {"configurable": {"thread_id": "issue-44-resume"}}

    with run(tracer, "urn:cordboard:fixture:44", "fixture", graph_id="dual-interrupt-fixture", run_id="r-44") as execution:
        continuation = execution.continuation
        graph.invoke({}, {**thread, "configurable": {**thread["configurable"], "cord_active": execution}})

    interrupts = {i.id: i for i in _state_interrupts(graph, thread)}
    assert len(interrupts) == 2

    a_span, = named(captured, "step:approve_a")
    b_span, = named(captured, "step:approve_b")
    assert a_span.attributes["cord.outcome"] == "awaiting_approval"
    assert b_span.attributes["cord.outcome"] == "awaiting_approval"
    resumed_from = {
        "approve_a": format(a_span.context.span_id, "016x"),
        "approve_b": format(b_span.context.span_id, "016x"),
    }

    with resume_run(tracer, continuation) as resumed:
        final = graph.invoke(
            Command(resume={iid: True for iid in interrupts}),
            {**thread, "configurable": {**thread["configurable"], "cord_active": resumed,
                                        "cord_resumed_from": resumed_from}},
        )

    assert final == {"approve_a": True, "approve_b": True, "count": 1}
    assert graph.get_state(thread).next == ()
    assert counter[0] == 1

    second = named(captured, "step:approve_a")[1]
    assert second.attributes["cord.resumed_from"] == resumed_from["approve_a"]
    assert second.attributes["cord.outcome"] == "passed"
    assert second.context.trace_id == a_span.context.trace_id
    assert len(named(captured, "run")) == 1  # one Run root, not one per invocation

    provider.shutdown()
