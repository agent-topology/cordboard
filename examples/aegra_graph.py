"""Aegra's public per-request context-manager factory for the existing graph."""

from contextlib import asynccontextmanager
import os

from langsmith import tracing_context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from cord_runtime.execution import run
from cord_runtime.telemetry import RedactingOTLPExporter
from examples.minimal_graph import build_graph
from examples.proxy_graph import ProxyModel


@asynccontextmanager
async def graph(config: dict):
    model = ProxyModel(os.environ.get("EXAMPLE_PROXY", "http://127.0.0.1:4000"))
    values = config.get("configurable", {})
    # Aegra also calls factories for schema/state inspection, without a Run.
    if "run_id" not in values:
        yield build_graph(model)
        return
    provider = TracerProvider(resource=Resource({"service.name": "cordboard-aegra"}))
    provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(
        os.environ.get("CORD_COLLECTOR", "http://127.0.0.1:4318/v1/traces"))))
    try:
        with tracing_context(enabled=False), run(
            provider.get_tracer("cordboard.aegra"), values.get("cord_subject"),
            "fixture", graph_id="minimal-graph", run_id=values["run_id"],
            caused_by_run_id=values.get("cord_caused_by_run_id"),
            cascade_depth=values.get("cord_cascade_depth", 0),
        ) as active:
            # A closure stays within this execution; no SDK object enters the
            # API config or Postgres checkpoint metadata.
            yield build_graph(model, active_run=active)
    finally:
        provider.shutdown()
