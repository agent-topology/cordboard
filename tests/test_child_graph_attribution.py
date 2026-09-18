"""End-to-end synthetic verification for #86 (ADR-0021).

The issue's own smallest synthetic fixture: a parent with one wrapped
child, calls it from two call sites, and retries one child Step. Recorded
through the public `cord_runtime.execution.Step.child` helper and replayed
through the real archive contract (`read_spans` -> `build_execution_tree`)
-- no model, Docker, or real entity is needed.
"""

import base64
import json

from google.protobuf.json_format import MessageToJson
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from cord_runtime.archive_query import query, read_spans
from cord_runtime.execution import AttemptOutcome, StepOutcome, run, span_id
from cord_runtime.viewer import build_execution_tree


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def _record_synthetic_graph():
    """A parent graph with a two-node child ("a" is the only child Node
    exercised here), called from two Nodes ("call-left"/"call-right"), with
    the "call-left" call site's child Step resumed once."""
    provider = TracerProvider(resource=Resource({}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    tracer = provider.get_tracer("test")

    with run(tracer, "urn:test:86", "fixture", graph_id="parent-graph", run_id="run-1") as execution:
        with execution.step("call-left", StepOutcome.PASSED) as left:
            with left.child("a", StepOutcome.AWAITING_APPROVAL) as first:
                pass
            first_span_id = span_id(first.span)
            with left.child("a", StepOutcome.PASSED, resumed_from=first_span_id) as retried:
                with retried.attempt(1, outcome=AttemptOutcome.PASSED):
                    pass
        with execution.step("call-right", StepOutcome.PASSED) as right:
            with right.child("a", StepOutcome.PASSED):
                pass
    provider.shutdown()
    return captured.spans, first_span_id


def _write_archive_file(tmp_path, sdk_spans):
    # protobuf JSON mapping base64-encodes `bytes` fields by default; the
    # real Collector's OTLP JSON exporter (and every existing fixture here)
    # instead uses lowercase hex for trace/span identifiers.
    def to_hex(value):
        return base64.b64decode(value).hex()

    envelope = json.loads(MessageToJson(encode_spans(sdk_spans)))
    for resource in envelope["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                span["traceId"] = to_hex(span["traceId"])
                span["spanId"] = to_hex(span["spanId"])
                if span.get("parentSpanId"):
                    span["parentSpanId"] = to_hex(span["parentSpanId"])
    path = tmp_path / "synthetic.otlp.jsonl"
    path.write_text(json.dumps(envelope) + "\n")
    return path


def test_synthetic_parent_and_wrapped_child_replays_with_attributed_call_sites(tmp_path):
    sdk_spans, first_child_span_id = _record_synthetic_graph()
    path = _write_archive_file(tmp_path, sdk_spans)

    # The full archive contract accepts this shape without error.
    query([path], reference=0)

    run = build_execution_tree(read_spans([path]))[0]
    assert run["graph_id"] == "parent-graph" and run["run_id"] == "run-1"
    by_node = {s["node"]: s for s in run["steps"]}
    assert set(by_node) == {"call-left", "call-right"}

    # Two call sites of the same child Node ("a") stay separately attributed:
    # distinct parent Steps, each with its own, distinct child_steps.
    left_children = by_node["call-left"]["child_steps"]
    right_children = by_node["call-right"]["child_steps"]
    assert [c["node"] for c in left_children] == ["a", "a"]
    assert [c["node"] for c in right_children] == ["a"]
    assert {c["span_id"] for c in left_children}.isdisjoint({c["span_id"] for c in right_children})

    # The retried child Step is linked to, and distinct from, the original.
    original, retried = left_children
    assert original["outcome"] == "awaiting_approval" and original["resumed_from"] is None
    assert retried["outcome"] == "passed" and retried["resumed_from"] == first_child_span_id
    assert retried["span_id"] != original["span_id"]
    assert [a["outcome"] for a in retried["attempts"]] == ["passed"]

    # The single, unrelated call site is unaffected by that resume.
    assert right_children[0]["resumed_from"] is None
    assert right_children[0]["repeated_execution"] is False
