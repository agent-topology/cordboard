"""Replay-risk fixture: unsafe vs. safe side-effect placement around
interrupt()/resume (#15). Same real LangGraph public seam as
`test_interrupt_graph.py`: no model, gateway, network, or Docker dependency.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from cord_runtime.execution import resume_run, run
from examples.replay_counter_graph import build_safe_graph, build_unsafe_graph


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def named(captured, name):
    return [s for s in captured.spans if s.name == name]


def _tracer():
    provider = TracerProvider(resource=Resource({"service.name": "replay-fixture"}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    return provider, provider.get_tracer("replay-fixture"), captured


def test_unsafe_graph_double_charges_across_one_pause_and_resume():
    provider, tracer, captured = _tracer()
    counter = [0]
    graph = build_unsafe_graph(InMemorySaver(), counter)
    thread = {"configurable": {"thread_id": "issue-15-unsafe"}}

    with run(tracer, "urn:cordboard:fixture:15", "fixture", graph_id="replay-fixture",
             run_id="r-15-unsafe") as execution:
        continuation = execution.continuation
        paused = graph.invoke({}, {**thread, "configurable": {**thread["configurable"], "cord_active": execution}})
    assert "__interrupt__" in paused
    assert counter[0] == 1  # the paused invocation already ran the pre-interrupt effect once

    first, = named(captured, "step:approve_and_charge")
    resumed_from = format(first.context.span_id, "016x")

    with resume_run(tracer, continuation) as resumed:
        final = graph.invoke(
            Command(resume=True),
            {**thread, "configurable": {**thread["configurable"], "cord_active": resumed,
                                         "cord_resumed_from": resumed_from}},
        )

    assert final["approved"] is True
    # Replay risk demonstrated: one logical approval, but the pre-interrupt
    # effect ran twice -- once paused, once again when the node replayed on resume.
    assert counter[0] == 2
    provider.shutdown()


def test_safe_graph_charges_exactly_once_across_the_same_pause_and_resume():
    provider, tracer, captured = _tracer()
    counter = [0]
    graph = build_safe_graph(InMemorySaver(), counter)
    thread = {"configurable": {"thread_id": "issue-15-safe"}}

    with run(tracer, "urn:cordboard:fixture:15", "fixture", graph_id="replay-fixture",
             run_id="r-15-safe") as execution:
        continuation = execution.continuation
        paused = graph.invoke({}, {**thread, "configurable": {**thread["configurable"], "cord_active": execution}})
    assert "__interrupt__" in paused
    assert counter[0] == 0  # nothing runs before interrupt() in the safe graph

    first, = named(captured, "step:approve")
    resumed_from = format(first.context.span_id, "016x")

    with resume_run(tracer, continuation) as resumed:
        final = graph.invoke(
            Command(resume=True),
            {**thread, "configurable": {**thread["configurable"], "cord_active": resumed,
                                         "cord_resumed_from": resumed_from}},
        )

    assert final["approved"] is True
    assert counter[0] == 1
    charge_step, = named(captured, "step:charge")
    assert charge_step.attributes["cord.resumed_from"] == resumed_from
    provider.shutdown()
