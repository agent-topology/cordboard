"""End-to-end verification for #89: a LangChain callback handler records
Cordboard Run/Step spans for a LangGraph graph with zero graph-code changes.

`examples/callback_child_graph.py` and `examples/callback_interrupt_graph.py`
import nothing from `cord_runtime`; every span recorded below comes only
from `cord_langgraph_callbacks.CordCallbackHandler`, attached externally
through `config["callbacks"]`, exactly as an entity (not the graph) would.
"""

import base64
import json

from agent_topology.langgraph import describe
from google.protobuf.json_format import MessageToJson
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode
import pytest

from cord_langgraph_callbacks import CordCallbackHandler, SEMCONV_VERSION
from cord_runtime.archive_query import query, read_spans
from cord_runtime.execution import SEMCONV_VERSION as RUNTIME_SEMCONV_VERSION
from cord_runtime.telemetry import redact_request
from cord_runtime.topology import check_freshness
from cord_runtime.viewer import build_execution_tree, correlate_topology
from cord_runtime.web.presentation import run_topology

from examples.callback_child_graph import build_graph as build_child_graph
from examples.callback_interrupt_graph import build_graph as build_interrupt_graph
from test_topology import publisher


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def _provider():
    provider = TracerProvider(resource=Resource({"service.name": "callback-fixture"}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    return provider, captured


def named(captured, name):
    return [s for s in captured.spans if s.name == name]


def _write_archive_file(tmp_path, request, name="synthetic.otlp.jsonl"):
    # protobuf JSON mapping base64-encodes `bytes` fields by default; the
    # real Collector's OTLP JSON exporter (and every existing fixture here)
    # instead uses lowercase hex for trace/span identifiers.
    def to_hex(value):
        return base64.b64decode(value).hex()

    envelope = json.loads(MessageToJson(request))
    for resource in envelope["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                span["traceId"] = to_hex(span["traceId"])
                span["spanId"] = to_hex(span["spanId"])
                if span.get("parentSpanId"):
                    span["parentSpanId"] = to_hex(span["parentSpanId"])
    path = tmp_path / name
    path.write_text(json.dumps(envelope) + "\n")
    return path


def test_semconv_version_matches_cord_runtime():
    """This package cannot depend on `cord-runtime` (see handler.py's module
    docstring for why), so the version literal is duplicated; catch drift."""
    assert SEMCONV_VERSION == RUNTIME_SEMCONV_VERSION


def test_two_level_graph_records_nested_run_and_steps_and_passes_archive_contract(tmp_path):
    provider, captured = _provider()
    tracer = provider.get_tracer("callback-fixture")
    graph = build_child_graph()

    handler = CordCallbackHandler(tracer, graph_id="callback-child-fixture",
                                   subject="urn:cordboard:fixture:89", subject_type="fixture", run_id="run-89a")
    graph.invoke({}, {"callbacks": [handler], "configurable": {"thread_id": "89a"}})
    handler.close()
    provider.shutdown()

    run_span, = named(captured, "run")
    prepare_span, = named(captured, "step:prepare")
    collect_span, = named(captured, "step:collect")
    gather_span, = named(captured, "step:gather")

    assert run_span.attributes["cord.graph.id"] == "callback-child-fixture"
    assert run_span.attributes["cord.run.id"] == "run-89a"
    assert run_span.attributes["cord.semconv.version"] == SEMCONV_VERSION

    # "prepare" and "collect" are top-level Steps, parented directly on Run.
    assert prepare_span.parent.span_id == run_span.context.span_id
    assert collect_span.parent.span_id == run_span.context.span_id
    # "gather" is the declared child graph's own Node, nested under "collect"
    # -- not a sibling of it -- purely from checkpoint-namespace nesting.
    assert gather_span.parent.span_id == collect_span.context.span_id
    assert gather_span.parent.span_id != run_span.context.span_id

    for span in (prepare_span, collect_span, gather_span):
        assert span.attributes["cord.outcome"] == "passed"
        assert span.status.status_code is not StatusCode.ERROR
        assert span.attributes["cord.graph.id"] == "callback-child-fixture"
        assert span.attributes["cord.subject.id"] == "urn:cordboard:fixture:89"
        assert span.start_time < span.end_time

    # The existing in-process redactor (ADR-0006) accepts and stamps every
    # span. A live Collector process is environment-dependent (this repo's
    # `collector`-marked tests already isolate that leg); this exercises the
    # same public `redact_secret` binding and OTel encoding the Collector
    # gate itself runs, without requiring the separate pinned CLI binary.
    accepted = redact_request(encode_spans(captured.spans))
    stamps = [attr for resource in accepted.resource_spans for scope in resource.scope_spans
              for span in scope.spans for attr in span.attributes if attr.key == "cord.redacted"]
    assert len(stamps) == len(captured.spans)
    assert all(attr.value.bool_value for attr in stamps)

    path = _write_archive_file(tmp_path, accepted)
    query([path], reference=0)  # the full archive contract accepts this shape unmodified

    run_record, = build_execution_tree(read_spans([path]))
    assert run_record["graph_id"] == "callback-child-fixture" and run_record["run_id"] == "run-89a"
    by_node = {s["node"]: s for s in run_record["steps"]}
    assert set(by_node) == {"prepare", "collect"}
    assert by_node["prepare"]["child_steps"] == []
    child, = by_node["collect"]["child_steps"]
    assert child["node"] == "gather" and child["outcome"] == "passed"


def test_viewer_overlays_the_recorded_child_step_under_its_topology_node(tmp_path):
    """AC4: a document shaped like omiologic's `channel_concept` (one real
    node calling a declared child graph, depth 1) overlays the callback
    handler's recorded nested Step under the resolved child Node -- reusing
    the existing, unchanged `#86` overlay (`web.presentation.run_topology`);
    nothing here is new overlay logic, only new evidence feeding it.
    """
    provider, captured = _provider()
    tracer = provider.get_tracer("callback-fixture")
    graph = build_child_graph()
    handler = CordCallbackHandler(tracer, graph_id="callback-child-fixture",
                                   subject="urn:cordboard:fixture:89c", subject_type="fixture", run_id="run-89c")
    graph.invoke({}, {"callbacks": [handler], "configurable": {"thread_id": "89c"}})
    handler.close()
    provider.shutdown()

    accepted = redact_request(encode_spans(captured.spans))
    path = _write_archive_file(tmp_path, accepted)
    run_record, = build_execution_tree(read_spans([path]))

    document = describe(graph, graph_id="callback-child-fixture", depth=1)
    with publisher({"status": 200, "body": json.dumps(document).encode()}) as endpoint:
        freshness = check_freshness(tmp_path / "snapshot", endpoint)
    result = correlate_topology(freshness, "callback-child-fixture", {"callback-child-fixture": "callback-child-fixture"})
    assert result["correlated"]

    diagram = run_topology(result["structure"], run_record, subgraphs=result["subgraphs"])
    by_id = {node["id"]: node for node in diagram["nodes"]}
    assert by_id["collect"]["status"] == "passed"
    child_calls = [c for c in diagram["child_calls"] if c["node"] == "collect"]
    assert len(child_calls) == 1
    gather = next(n for n in child_calls[0]["diagram"]["nodes"] if n["id"] == "gather")
    assert gather["status"] == "passed"


def test_interrupt_closes_step_as_awaiting_approval_with_non_error_status():
    provider, captured = _provider()
    tracer = provider.get_tracer("callback-fixture")
    graph = build_interrupt_graph(InMemorySaver())
    thread = {"configurable": {"thread_id": "89b"}}

    handler = CordCallbackHandler(tracer, graph_id="callback-interrupt-fixture",
                                   subject="urn:cordboard:fixture:89b", subject_type="fixture", run_id="run-89b")
    paused = graph.invoke({}, {**thread, "callbacks": [handler]})
    handler.close()
    provider.shutdown()

    assert "__interrupt__" in paused
    assert graph.get_state(thread).next == ("approve",)

    first, = named(captured, "step:approve")
    assert first.attributes["cord.outcome"] == "awaiting_approval"
    assert first.status.status_code is not StatusCode.ERROR
    approval, = handler.awaiting_approval_steps
    assert approval.node == "approve"
    assert approval.span_id == format(first.context.span_id, "016x")


def test_resume_links_cord_resumed_from_across_two_handler_instances():
    """AC2's second half: an entity that persists `RunContinuation` and the
    interrupted Step's span id across the call boundary (not through the
    graph's own checkpointed state -- the graph is never touched) can supply
    both back to a fresh handler for the resuming invocation and get a
    correctly linked `cord.resumed_from`, still inside the same trace."""
    provider, captured = _provider()
    tracer = provider.get_tracer("callback-fixture")
    graph = build_interrupt_graph(InMemorySaver())
    thread = {"configurable": {"thread_id": "89d"}}

    first_handler = CordCallbackHandler(tracer, graph_id="callback-interrupt-fixture",
                                         subject="urn:cordboard:fixture:89d", subject_type="fixture",
                                         run_id="run-89d")
    graph.invoke({}, {**thread, "callbacks": [first_handler]})
    first_handler.close()

    approval, = first_handler.awaiting_approval_steps
    continuation = first_handler.continuation

    second_handler = CordCallbackHandler(tracer, resume=continuation, resumed_from=approval.span_id)
    final = graph.invoke(Command(resume=True), {**thread, "callbacks": [second_handler]})
    second_handler.close()
    provider.shutdown()

    assert final == {"approved": True}
    assert graph.get_state(thread).next == ()

    first, second = named(captured, "step:approve")
    assert second.attributes["cord.resumed_from"] == approval.span_id
    assert second.attributes["cord.outcome"] == "passed"
    assert second.context.trace_id == first.context.trace_id
    assert second.context.span_id != first.context.span_id
    assert second.status.status_code is not StatusCode.ERROR

    # Exactly one Run root across both invocations (ADR-0008): resume never
    # re-creates it.
    assert len(named(captured, "run")) == 1


def test_untagged_chain_events_are_ignored():
    """Routing/trigger functions and any other non-node chain activity carry
    no `graph:step:N` tag (`seq:step:*` instead, per #89's own evidence) and
    must never be recorded as a Step."""
    provider, captured = _provider()
    tracer = provider.get_tracer("callback-fixture")
    handler = CordCallbackHandler(tracer, graph_id="g", subject="s", subject_type="t")
    handler.on_chain_start({}, {}, run_id="11111111-1111-1111-1111-111111111111",
                            tags=["seq:step:0"], metadata={"langgraph_node": "route", "langgraph_checkpoint_ns": "route:1"})
    handler.close()
    provider.shutdown()
    assert captured.spans == []


def test_constructor_rejects_mixing_fresh_and_resumed_run_identity():
    provider, _ = _provider()
    tracer = provider.get_tracer("callback-fixture")
    with pytest.raises(ValueError):
        CordCallbackHandler(tracer, graph_id="g", subject="s", subject_type="t",
                             resume=object())
    with pytest.raises(ValueError):
        CordCallbackHandler(tracer)
    provider.shutdown()
