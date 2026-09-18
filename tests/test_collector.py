from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from google.protobuf.json_format import MessageToJson
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
import pytest
import requests

from cord_runtime.archive_check import check
from cord_runtime.archive_health import HealthStatus, archive_health
from cord_runtime.collector_health import REACHABLE, UNAVAILABLE, check_collector_health, wait_for_collector_recovery
from cord_runtime.execution import SEMCONV_VERSION, AttemptOutcome, StepOutcome, run
from cord_runtime.telemetry import UNREACHABLE as EXPORT_UNREACHABLE
from cord_runtime.telemetry import RedactingOTLPExporter, redact_request
from conftest import CREDENTIAL, PRIVATE_KEY, ROOT
from test_telemetry import Capture, request, spans

pytestmark = pytest.mark.collector


class CollectorEndpoint(str):
    """The OTLP export URL, as every existing caller already uses it
    directly, plus this same Collector's health-check URL (#45 AC2) as an
    attribute so no existing call site needs to change."""

    health: str


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def collector(directory, config=None):
    binary = Path(os.environ.get("OTELCOL", ROOT / ".tools/otelcol-contrib"))
    assert binary.is_file(), "pinned Collector required; see docs/archive.md"
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
    assert "0.148.0" in version.stdout
    port = free_port()
    health_port = free_port()
    directory.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CORD_ARCHIVE_DIR": str(directory),
           "CORD_OTLP_ENDPOINT": f"127.0.0.1:{port}",
           "CORD_HEALTH_ENDPOINT": f"127.0.0.1:{health_port}", "TZ": "UTC"}
    process = subprocess.Popen(
        [str(binary), "--config", str(config or ROOT / "collector/config.yaml")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                assert time.monotonic() < deadline, "Collector did not become ready"
                time.sleep(.05)
        endpoint = CollectorEndpoint(f"http://127.0.0.1:{port}/v1/traces")
        endpoint.health = f"http://127.0.0.1:{health_port}/"
        yield endpoint
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert process.returncode == 0
        assert CREDENTIAL.encode() not in output
        assert PRIVATE_KEY.encode() not in output


def send(endpoint, req):
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(endpoint, data=req.SerializeToString(),
                                headers={"Content-Type": "application/x-protobuf"}, timeout=5)
        assert response.status_code == 200


def archive_records(directory):
    return [json.loads(line) for path in directory.glob("*.otlp.jsonl")
            for line in path.read_text().splitlines()]


def archived_spans(directory):
    return [s for record in archive_records(directory) for r in record["resourceSpans"]
            for scope in r["scopeSpans"] for s in scope["spans"]]


def attributes(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span.get("attributes", [])}


def test_actual_sdk_export_and_whole_archive(tmp_path, cli):
    directory = tmp_path / "accepted"
    with collector(directory) as endpoint:
        provider = TracerProvider(resource=Resource({"service.name": "fixture", "detail": CREDENTIAL}))
        provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(endpoint)))
        tracer = provider.get_tracer("fixture")
        with run(tracer, "urn:test:5", "fixture", graph_id="archive-fixture") as execution:
            with execution.step("draft", StepOutcome.PASSED) as step:
                for n, tier, outcome in [(1,"fast",AttemptOutcome.FAILED),(2,"fast",AttemptOutcome.ESCALATED),(3,"deep",AttemptOutcome.PASSED)]:
                    with step.attempt(n, tier, outcome) as attempt:
                        attempt.set_attribute("nested", ["ordinary", CREDENTIAL])
                        attempt.add_event("fixture", {"detail": CREDENTIAL})
        with tracer.start_as_current_span("blocked", attributes={"detail": PRIVATE_KEY}):
            pass
        provider.shutdown()
        nested = request()
        spans(nested)[0].attributes.add(key="map").value.kvlist_value.values.add(key="bytes").value.bytes_value = CREDENTIAL.encode()
        send(endpoint, redact_request(nested))
    archived = archived_spans(directory)
    assert len(archived) == 6
    assert all(attributes(s)["cord.redacted"] is True for s in archived)
    root = next(s for s in archived if s["name"] == "run")
    step = next(s for s in archived if s["name"] == "step:draft")
    attempts = sorted((s for s in archived if s["name"] == "attempt"), key=lambda s: attributes(s)["cord.step.attempt"])
    assert not root.get("parentSpanId")
    assert step["parentSpanId"] == root["spanId"]
    assert all(s["parentSpanId"] == step["spanId"] for s in attempts)
    assert len({s["traceId"] for s in [root, step, *attempts]}) == 1
    assert attributes(root)["cord.semconv.version"] == SEMCONV_VERSION
    assert [(attributes(s)["cord.tier"],attributes(s)["cord.outcome"]) for s in attempts] == [("fast","failed"),("fast","escalated"),("deep","passed")]
    assert all(attributes(s)["cord.run.id"] == attributes(root)["cord.run.id"] for s in [step, *attempts])
    assert all(attributes(s)["cord.subject.id"] == "urn:test:5" for s in [root, step, *attempts])
    complete = b"".join(p.read_bytes() for p in directory.glob("*.otlp.jsonl"))
    assert CREDENTIAL.encode() not in complete and PRIVATE_KEY.encode() not in complete
    assert check([directory], cli) == 0


@pytest.mark.parametrize("stamp", [None, False, "true", 1])
def test_gate_rejects_unprocessed_stamps_in_fresh_archive(tmp_path, stamp):
    directory = tmp_path / "negative"
    req = request(CREDENTIAL)
    if stamp is not None:
        value = spans(req)[0].attributes.add(key="cord.redacted").value
        if type(stamp) is bool:
            value.bool_value = stamp
        elif type(stamp) is int:
            value.int_value = stamp
        else:
            value.string_value = stamp
    with collector(directory) as endpoint:
        send(endpoint, req)
    assert archived_spans(directory) == []
    assert all(not p.read_bytes() for p in directory.glob("*.otlp.jsonl"))


def test_removed_redactor_with_real_sdk_exporter(tmp_path):
    directory = tmp_path / "removed"
    with collector(directory) as endpoint:
        provider = TracerProvider(resource=Resource({"service.name": "fixture"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        with provider.get_tracer("fixture").start_as_current_span("unprocessed", attributes={"detail": CREDENTIAL}):
            pass
        provider.shutdown()
    assert archived_spans(directory) == []


def test_processing_failure_cannot_reach_archive(tmp_path, monkeypatch):
    import redact_secret
    directory = tmp_path / "processing-failure"
    def fail(_):
        raise RuntimeError(CREDENTIAL)
    monkeypatch.setattr(redact_secret, "scan_and_redact", fail)
    with collector(directory) as endpoint:
        provider = TracerProvider(resource=Resource({}))
        provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(endpoint)))
        with provider.get_tracer("test").start_as_current_span("unprocessed", attributes={"cord.redacted": True}):
            pass
        provider.shutdown()
    assert archived_spans(directory) == []


def test_block_cannot_reach_fresh_archive(tmp_path):
    directory = tmp_path / "blocked"
    with collector(directory) as endpoint:
        provider = TracerProvider(resource=Resource({}))
        provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(endpoint)))
        with provider.get_tracer("test").start_as_current_span("blocked") as span:
            span.add_event("nested", {"detail": PRIVATE_KEY})
        provider.shutdown()
    assert archived_spans(directory) == []


def test_append_restart_utc_partition_and_compression(tmp_path, cli):
    directory = tmp_path / "append"
    req = redact_request(request())
    # A producer-controlled partition is always overwritten by the Collector.
    req.resource_spans[0].resource.attributes.add(key="cord.archive.date").value.string_value = "1900-01-01"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with collector(directory) as endpoint:
        send(endpoint, req)
    path = directory / f"archive-{today}.otlp.jsonl"
    before = path.read_bytes()
    assert before and len(archived_spans(directory)) == 1
    with collector(directory) as endpoint:
        send(endpoint, req)
    assert path.read_bytes().startswith(before)
    assert len(archived_spans(directory)) == 2
    assert len(list(directory.glob("*.otlp.jsonl"))) == 1
    # Exercise two partition values without a 24-hour wait. Only the clock
    # expression changes in a temporary config; gate and exporter stay intact.
    next_config = tmp_path / "next-day.yaml"
    next_config.write_text((ROOT / "collector/config.yaml").read_text().replace(
        'FormatTime(Now(), "%Y-%m-%d")', '"2099-01-02"'))
    with collector(directory, next_config) as endpoint:
        send(endpoint, req)
    assert (directory / "archive-2099-01-02.otlp.jsonl").exists()
    assert len(archived_spans(directory)) == 3
    compressed = path.with_suffix(path.suffix + ".gz")
    with gzip.open(compressed, "wb") as output:
        output.write(path.read_bytes())
    assert gzip.decompress(compressed.read_bytes()) == path.read_bytes()
    assert check([compressed], cli) == 0


def test_archive_check_decodes_nested_payload_and_failure_precedence(tmp_path, cli):
    contaminated = request()
    spans(contaminated)[0].attributes.add(key="bytes").value.bytes_value = PRIVATE_KEY.encode()
    path = tmp_path / "unsafe.otlp.jsonl"
    path.write_text(MessageToJson(contaminated, indent=None) + "\n")
    assert check([path], cli) == 1
    invalid = tmp_path / "invalid.otlp.jsonl"
    invalid.write_bytes(b"\xff")
    assert check([path, invalid], cli) == 2
    assert check([tmp_path / "missing"], cli) == 2


def test_query_actual_archive_fixture(tmp_path):
    from examples.archive_fixture import emit
    from cord_runtime.archive_query import query, reference_ns

    directory = tmp_path / 'query'
    with collector(directory) as endpoint:
        emit(endpoint)
    reference = datetime.now(timezone.utc).isoformat()
    expected = [{'graph': 'archive-fixture', 'node': 'draft', 'count': 1}]
    assert query([directory], reference=reference_ns(reference)) == expected
    result = subprocess.run(
        [str(ROOT / '.venv/bin/archive-escalations'), str(directory),
         '--reference-time', reference, '--top', '10'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == expected


def test_collector_health_visible_independent_of_runtime(tmp_path):
    """#45 AC2: reachability comes from the Collector's own health endpoint,
    never inferred from whether a graph Run succeeded."""
    directory = tmp_path / "health"
    never_bound = free_port()
    assert check_collector_health(f"http://127.0.0.1:{never_bound}/", timeout=.5) == UNAVAILABLE
    with collector(directory) as endpoint:
        assert check_collector_health(endpoint.health, timeout=5) == REACHABLE
    # The context manager has already stopped this Collector.
    assert check_collector_health(endpoint.health, timeout=.5) == UNAVAILABLE


def test_collector_stop_restart_bounded_recovery(tmp_path):
    """#45 AC2: a stop is visible immediately, a bounded wait never reports a
    stopped Collector as recovered, and restarting on the same configured
    port is observed within the bound."""
    binary = Path(os.environ.get("OTELCOL", ROOT / ".tools/otelcol-contrib"))
    assert binary.is_file(), "pinned Collector required; see docs/archive.md"
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    port, health_port = free_port(), free_port()
    env = {**os.environ, "CORD_ARCHIVE_DIR": str(directory), "CORD_OTLP_ENDPOINT": f"127.0.0.1:{port}",
           "CORD_HEALTH_ENDPOINT": f"127.0.0.1:{health_port}", "TZ": "UTC"}
    health_url = f"http://127.0.0.1:{health_port}/"

    def start():
        return subprocess.Popen([str(binary), "--config", str(ROOT / "collector/config.yaml")],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    process = start()
    try:
        assert wait_for_collector_recovery(health_url, timeout=15, poll_interval=.05)
        process.terminate()
        process.wait(timeout=10)
        assert wait_for_collector_recovery(health_url, timeout=1, poll_interval=.1) is False
        process = start()  # restart on the very same configured port
        assert wait_for_collector_recovery(health_url, timeout=15, poll_interval=.05)
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_export_unreachable_reason_against_a_real_stopped_collector(tmp_path):
    """#45 AC2: delivery loss is reported with its own reason, not
    concealed behind a bare failure boolean, when the Collector this
    exporter is actually configured for is unreachable."""
    directory = tmp_path / "unreachable"
    with collector(directory) as endpoint:
        target = str(endpoint)
    # The context manager already stopped this Collector; nothing listens.
    exporter = RedactingOTLPExporter(target)
    provider = TracerProvider(resource=Resource({}))
    captured = Capture()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    with provider.get_tracer("test").start_as_current_span("test"):
        pass
    provider.shutdown()
    assert exporter.export(captured.spans) == SpanExportResult.FAILURE
    assert exporter.last_export.reason == EXPORT_UNREACHABLE
    exporter.shutdown()


def test_archive_health_pending_before_first_export_then_healthy(tmp_path):
    """#45 AC1/AC4: absence of files is PENDING, not a failure, and the
    same directory becomes HEALTHY once the real Collector's gated export
    lands -- evidence-based, not inferred. Uses a real SDK-exported span
    (real timestamps), not the raw `request()` fixture the redaction-only
    tests above use, since `archive_health` applies the full archive
    contract rather than only the gate."""
    directory = tmp_path / "fresh"
    directory.mkdir()
    assert archive_health([directory]).status is HealthStatus.PENDING
    with collector(directory) as endpoint:
        provider = TracerProvider(resource=Resource({"service.name": "health-fixture"}))
        provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(endpoint)))
        with provider.get_tracer("fixture").start_as_current_span("span"):
            pass
        provider.shutdown()
        deadline = time.monotonic() + 5
        while archive_health([directory]).status is not HealthStatus.HEALTHY:
            assert time.monotonic() < deadline, "archive did not become healthy after a real export"
            time.sleep(.05)
    assert archive_health([directory]).span_count == 1


def test_replay_through_real_collector_does_not_duplicate_health_spans(tmp_path):
    """#45 AC3: a retransmitted, identically-accepted export is not double
    counted by the health reader, exercised through the real gate/archive
    pipeline rather than a synthetic file."""
    directory = tmp_path / "replay-health"
    captured = Capture()
    provider = TracerProvider(resource=Resource({"service.name": "replay-fixture"}))
    provider.add_span_processor(SimpleSpanProcessor(captured))
    with provider.get_tracer("fixture").start_as_current_span("span"):
        pass
    provider.shutdown()
    req = redact_request(encode_spans(captured.spans))
    with collector(directory) as endpoint:
        send(endpoint, req)
        send(endpoint, req)
    health = archive_health([directory])
    assert health.status is HealthStatus.HEALTHY
    assert health.span_count == 1
