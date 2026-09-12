"""An independent graph with private state and no model or gateway dependency."""

from contextlib import asynccontextmanager, nullcontext
import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from cord_runtime.execution import AttemptOutcome, StepOutcome, run
from cord_runtime.telemetry import RedactingOTLPExporter


class State(TypedDict):
    items: list[str]
    size: int


def build_graph(active=None):
    def count_items(state):
        with active.step("count", StepOutcome.PASSED) if active else nullcontext() as step:
            with step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext():
                return {"size": len(state["items"])}

    builder = StateGraph(State)
    builder.add_node("count", count_items)
    builder.add_edge(START, "count")
    builder.add_edge("count", END)
    return builder.compile()


@asynccontextmanager
async def graph(config: dict):
    values = config.get("configurable", {})
    if "run_id" not in values:
        yield build_graph()
        return
    provider = TracerProvider(resource=Resource({"service.name": "opaque-graph"}))
    provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(
        os.environ.get("CORD_COLLECTOR", "http://127.0.0.1:4318/v1/traces"))))
    try:
        with tracing_context(enabled=False), run(
            provider.get_tracer("opaque-graph"), values.get("cord_subject"), "fixture",
            graph_id="opaque-graph", run_id=values["run_id"],
        ) as active:
            yield build_graph(active)
    finally:
        provider.shutdown()
