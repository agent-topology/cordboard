"""Opt-in tests of real Collector -> self-hosted Langfuse -> public read API."""
import base64
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
import pytest
import yaml

from cord_runtime.aegra_client import execute
from cord_runtime.archive_check import check
from cord_runtime.archive_query import read_spans
from cord_runtime.langfuse_query import PublicReader, compare
from cord_runtime.telemetry import redact_request
from conftest import ROOT
from test_aegra import aegra, postgres
from test_archive_query import FIXTURES, REFERENCE
from test_collector import collector, send

pytestmark = [pytest.mark.collector, pytest.mark.langfuse,
              pytest.mark.skipif(not os.environ.get('CORD_LANGFUSE_TEST'), reason='set CORD_LANGFUSE_TEST=1 for the pinned local stack')]


@pytest.fixture
def config(tmp_path):
    base = yaml.safe_load((ROOT / 'collector/config.yaml').read_text())
    extra = yaml.safe_load((ROOT / 'collector/langfuse.yaml').read_text())
    for key in ('processors', 'exporters'):
        base[key].update(extra[key])
    base['service']['pipelines'].update(extra['service']['pipelines'])
    path = tmp_path / 'collector.yaml'
    path.write_text(yaml.safe_dump(base))
    return path


@pytest.fixture
def reader():
    value = PublicReader(os.environ.get('CORD_LANGFUSE_URL', 'http://127.0.0.1:3300'),
                         os.environ['LANGFUSE_PUBLIC_KEY'], os.environ['LANGFUSE_SECRET_KEY'])
    yield value
    value.close()


def fixture_requests():
    records = [json.loads(line) for line in (FIXTURES / 'window.otlp.jsonl').read_text().splitlines()]
    # Fresh identities prevent an earlier test's successfully ingested rows from
    # satisfying this run's completion check. Original fixture files stay immutable.
    trace_ids, span_ids = {}, {}
    for record in records:
        for resource in record['resourceSpans']:
            for scope in resource['scopeSpans']:
                for s in scope['spans']:
                    trace_ids.setdefault(s['traceId'], uuid4().bytes)
                    span_ids.setdefault(s['spanId'], uuid4().bytes[:8])
    model_free = set(sorted(trace_ids)[::2])
    for record in records:
        for resource in record['resourceSpans']:
            for scope in resource['scopeSpans']:
                for s in scope['spans']:
                    free = s['traceId'] in model_free
                    s['traceId'] = base64.b64encode(trace_ids[s['traceId']]).decode()
                    s['spanId'] = base64.b64encode(span_ids[s['spanId']]).decode()
                    if s.get('parentSpanId'):
                        s['parentSpanId'] = base64.b64encode(span_ids[s['parentSpanId']]).decode()
                    if s['name'] == 'attempt':
                        s['attributes'].append({'key': 'graph.verdict', 'value': {'stringValue': 'fixture judgment'}})
                        s['attributes'].append({'key': 'graph.violated_rules', 'value': {'arrayValue': {'values': [{'stringValue': 'fixture-rule'}]}}})
                    if free:
                        s['attributes'] = [a for a in s['attributes'] if a['key'] != 'cord.tier'
                                           and not a['key'].startswith('gen_ai.')]
                        for a in s['attributes']:
                            if a['key'] == 'cord.semconv.version':
                                a['value'] = {'stringValue': '0.3.0'}
        yield redact_request(ParseDict(record, ExportTraceServiceRequest()))


def test_dual_export_known_answer_and_gate(tmp_path, config, reader, cli):
    directory = tmp_path / 'archive'
    requests = list(fixture_requests())
    with collector(directory, config) as endpoint:
        for request in requests:
            send(endpoint, request)
            send(endpoint, request)  # real re-delivery to both branches
        # A separate trace with no successful stamp must reach neither branch.
        from test_telemetry import request as unprocessed
        rejected = unprocessed('ordinary')
        rejected_id = rejected.resource_spans[0].scope_spans[0].spans[0].trace_id.hex()
        send(endpoint, rejected)
    result = compare([directory], reader, reference=REFERENCE, wait=90)
    assert result['status'] == 'match', result
    assert result['archive'] == result['langfuse'] == json.loads((FIXTURES / 'expected.json').read_text())
    saved = read_spans([directory])
    assert any(s['attributes'].get('cord.outcome') == 'escalated' and 'cord.tier' not in s['attributes']
               for s, _ in saved.values())
    assert {s['attributes'].get('cord.semconv.version') for s, _ in saved.values() if s['name'] == 'run'} == {'0.2.0', '0.3.0'}
    assert all(s['traceId'] != rejected_id for s, _ in saved.values())
    rejected_rows = reader.get('observations', {'traceId': rejected_id, 'limit': 100}, time.monotonic() + 5)
    assert rejected_rows['data'] == []
    # Graph-owned metadata is carried as supplied, without interpreting rules.
    attempt = next(s for s, _ in saved.values() if s['name'] == 'attempt')
    raw = reader.get('observations/' + attempt['spanId'], {}, time.monotonic() + 5)
    assert raw['metadata']['cordboard']['graph.verdict'] == 'fixture judgment'
    assert raw['metadata']['cordboard']['graph.violated_rules'] == ['fixture-rule']
    root = next(s for s, _ in saved.values() if s['name'] == 'run')
    raw = reader.get('observations/' + root['spanId'], {}, time.monotonic() + 5)
    assert 'graph.verdict' not in raw['metadata']['cordboard']
    assert check([directory], cli) == 0


@pytest.mark.aegra
def test_live_model_free_aegra(tmp_path, config, reader, postgres, cli):
    directory = tmp_path / 'archive'
    with collector(directory, config) as target:
        with aegra(postgres, None, target, ROOT / 'aegra/opaque.json') as endpoint:
            result = execute(endpoint, 'opaque-graph', 'langfuse model-free subject', {'items': ['one']})
    assert result['values']['size'] == 1
    saved = read_spans([directory])
    assert len(saved) == 3
    assert all('cord.tier' not in s['attributes'] and not any(k.startswith('gen_ai.') for k in s['attributes'])
               for s, _ in saved.values())
    compared = compare([directory], reader, reference=time.time_ns(), wait=90)
    assert compared['status'] == 'match', compared
    assert compared['archive'] == compared['langfuse'] == []
    assert check([directory], cli) == 0


def test_failed_export_keeps_archive(tmp_path, config, monkeypatch):
    import socket
    # Reserve an unused local port without listening: no remote service involved.
    with socket.socket() as unavailable:
        unavailable.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{unavailable.getsockname()[1]}'
        monkeypatch.setenv('CORD_LANGFUSE_URL', url)
        settings = yaml.safe_load(config.read_text())
        settings['exporters']['otlphttp/langfuse']['retry_on_failure']['enabled'] = False
        config.write_text(yaml.safe_dump(settings))
        directory = tmp_path / 'archive'
        with collector(directory, config) as endpoint:
            for request in fixture_requests():
                send(endpoint, request)
        failed = PublicReader(url, 'fixture', 'fixture')
        try:
            result = compare([directory], failed, reference=REFERENCE, wait=.1, poll=.01)
        finally:
            failed.close()
    assert result['status'] == 'unavailable' and result['langfuse'] is None
    assert result['archive'] == json.loads((FIXTURES / 'expected.json').read_text())
