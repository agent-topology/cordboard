from unittest.mock import Mock

from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
import pytest
import redact_secret

from cord_runtime.execution import AttemptOutcome, StepOutcome, run
from cord_runtime.telemetry import RedactingOTLPExporter, redact_request
from conftest import CREDENTIAL, PRIVATE_KEY


def request(text="ordinary text"):
    result = ExportTraceServiceRequest()
    resource = result.resource_spans.add()
    resource.resource.attributes.add(key="service.name").value.string_value = "fixture"
    span = resource.scope_spans.add().spans.add(name="fixture", trace_id=b"a" * 16, span_id=b"b" * 8)
    span.attributes.add(key="detail").value.string_value = text
    return result


def spans(req):
    return [s for r in req.resource_spans for scope in r.scope_spans for s in scope.spans]


@pytest.mark.parametrize("text,actions", [(CREDENTIAL, ["redact"]), (PRIVATE_KEY, ["block"]), ("ordinary text", [])])
def test_public_redactor(text, actions):
    result = redact_secret.scan_and_redact(text)
    assert [f.action for f in result.findings] == actions
    assert (result.text != text) == bool(actions)


def test_complete_envelope_and_input_immutability():
    original = request(CREDENTIAL)
    r = original.resource_spans[0]
    scope = r.scope_spans[0]
    span = scope.spans[0]
    r.resource.attributes.add(key="nested").value.kvlist_value.values.add(key="value").value.string_value = CREDENTIAL
    scope.scope.name = CREDENTIAL
    scope.scope.version = CREDENTIAL
    scope.schema_url = CREDENTIAL
    span.name = CREDENTIAL
    span.trace_state = "test=" + CREDENTIAL
    span.status.message = CREDENTIAL
    span.events.add(name=CREDENTIAL).attributes.add(key=CREDENTIAL).value.array_value.values.add().string_value = CREDENTIAL
    span.links.add(trace_id=b"c"*16, span_id=b"d"*8).attributes.add(key="bytes").value.bytes_value = CREDENTIAL.encode()
    span.attributes.add(key="cord.redacted").value.bool_value = True
    before = original.SerializeToString()
    safe = redact_request(original)
    assert len(spans(safe)) == 1
    assert CREDENTIAL not in MessageToJson(safe)
    assert CREDENTIAL.encode() not in safe.SerializeToString()
    assert original.SerializeToString() == before
    stamps = [a for a in spans(safe)[0].attributes if a.key == "cord.redacted"]
    assert len(stamps) == 1 and stamps[0].value.bool_value


@pytest.mark.parametrize("location", ["span", "resource", "event", "bytes"])
def test_block_suppresses_whole_span(location):
    req = request()
    resource = req.resource_spans[0]
    span = spans(req)[0]
    span.attributes.add(key="cord.redacted").value.bool_value = True
    if location == "resource":
        resource.resource.attributes.add(key="key").value.string_value = PRIVATE_KEY
    elif location == "event":
        span.events.add(name="exception").attributes.add(key="message").value.string_value = PRIVATE_KEY
    elif location == "bytes":
        span.attributes.add(key="key").value.bytes_value = PRIVATE_KEY.encode()
    else:
        span.name = PRIVATE_KEY
    assert not spans(redact_request(req))


def test_failure_does_not_stamp_or_leak(monkeypatch, caplog):
    req = request(CREDENTIAL)
    spans(req)[0].attributes.add(key="cord.redacted").value.bool_value = True
    monkeypatch.setattr(redact_secret, "scan_and_redact", Mock(side_effect=RuntimeError(CREDENTIAL)))
    assert not spans(redact_request(req))
    assert CREDENTIAL not in caplog.text
    assert "processing failed" in caplog.text


def test_undecodable_bytes_fail_closed():
    req = request()
    spans(req)[0].attributes.add(key="bytes").value.bytes_value = b"\xff"
    assert not spans(redact_request(req))


class Capture(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def test_sdk_parentage_outcomes_and_fresh_runs():
    provider = TracerProvider(resource=Resource({}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    tracer = provider.get_tracer("test")
    for _ in range(2):
        with run(tracer, "urn:test:5", "fixture", graph_id="archive-fixture") as execution:
            with execution.step("draft", StepOutcome.PASSED) as step:
                for n, tier, outcome in [(1,"fast",AttemptOutcome.FAILED),(2,"fast",AttemptOutcome.ESCALATED),(3,"deep",AttemptOutcome.PASSED)]:
                    with step.attempt(n, tier, outcome):
                        pass
                with pytest.raises(TypeError):
                    with step.attempt(4, "deep", StepOutcome.PASSED):
                        pass
            with pytest.raises(TypeError):
                with execution.step("bad", AttemptOutcome.ESCALATED):
                    pass
    roots = [s for s in captured.spans if s.name == "run"]
    assert len(roots) == 2 and roots[0].context.trace_id != roots[1].context.trace_id
    assert roots[0].attributes["cord.run.id"] != roots[1].attributes["cord.run.id"]
    for root in roots:
        assert root.parent is None and root.attributes["cord.semconv.version"] == "0.3.0"
        children = [s for s in captured.spans if s.parent and s.parent.span_id == root.context.span_id]
        assert len(children) == 1
        attempts = [s for s in captured.spans if s.parent and s.parent.span_id == children[0].context.span_id]
        assert [(s.attributes["cord.tier"],s.attributes["cord.outcome"]) for s in attempts] == [("fast","failed"),("fast","escalated"),("deep","passed")]
        assert all(s.attributes["cord.subject.id"] == "urn:test:5" for s in attempts)
        assert all(s.attributes["cord.graph.id"] == "archive-fixture" for s in [root, *children, *attempts])
    provider.shutdown()


def test_export_failure_is_value_free(caplog):
    exporter = RedactingOTLPExporter()
    exporter.session.post = Mock(side_effect=RuntimeError(CREDENTIAL))
    provider = TracerProvider(resource=Resource({}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    with provider.get_tracer("test").start_as_current_span("test"):
        pass
    assert exporter.export(captured.spans) == SpanExportResult.FAILURE
    assert CREDENTIAL not in caplog.text
    provider.shutdown()
    exporter.shutdown()



@pytest.mark.parametrize("graph_id", [None, "", "   "])
def test_run_requires_explicit_graph_identity(graph_id):
    provider = TracerProvider(resource=Resource({}))
    try:
        with pytest.raises(ValueError, match="Graph identity"):
            with run(provider.get_tracer("test"), "urn:test:6", "fixture", graph_id=graph_id):
                pass
    finally:
        provider.shutdown()
