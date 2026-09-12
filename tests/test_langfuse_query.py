from copy import deepcopy
import json
import time

import pytest

from cord_runtime.archive_query import ArchiveError, read_spans
from cord_runtime.langfuse_query import Pending, PublicReader, ReadError, compare, observation
from test_archive_query import FIXTURES, REFERENCE


class Reader:
    def __init__(self, spans):
        self.spans = spans
        self.calls = 0

    def trace(self, tid, deadline):
        self.calls += 1
        found = {sid: deepcopy(s) for sid, (s, _) in self.spans.items() if s['traceId'] == tid}
        return found, next(iter(found.values()))['attributes']['cord.subject.id']


@pytest.fixture
def source():
    return FIXTURES / 'window.otlp.jsonl'


def test_known_answer_complete_window_and_retransmissions(source):
    reader = Reader(read_spans([source, source]))
    result = compare([source, source], reader, reference=REFERENCE)
    assert result['status'] == 'match'
    assert result['archive'] == result['langfuse'] == json.loads((FIXTURES / 'expected.json').read_text())
    assert compare([source], reader, reference=REFERENCE, top=2)['archive'] == json.loads(
        (FIXTURES / 'top2.expected.json').read_text())


def test_empty_count_requires_complete_observations(source):
    reader = Reader(read_spans([source]))
    result = compare([source], reader, reference=REFERENCE + 100 * 86400 * 10**9)
    assert result['status'] == 'match' and result['langfuse'] == []
    def pending(*args):
        raise Pending
    reader.trace = pending
    result = compare([source], reader, reference=REFERENCE + 100 * 86400 * 10**9, wait=.01, poll=.001)
    assert result['status'] == 'timeout' and result['langfuse'] is None and result['archive'] == []


def test_delayed_then_complete(source):
    reader = Reader(read_spans([source]))
    original = reader.trace
    attempts = []
    def delayed(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise Pending
        return original(*args)
    reader.trace = delayed
    assert compare([source], reader, reference=REFERENCE, wait=1, poll=.001)['status'] == 'match'
    assert len(attempts) > 1


@pytest.mark.parametrize('status', ['unavailable', 'api_error', 'invalid_response'])
def test_failure_is_not_empty_and_archive_remains(source, status):
    reader = Reader({})
    def fail(*args):
        raise ReadError(status)
    reader.trace = fail
    result = compare([source], reader, reference=REFERENCE, wait=.01, poll=.001)
    assert result['status'] == status and result['langfuse'] is None and result['archive']


@pytest.mark.parametrize('field,value', [('cord.outcome', 'failed'), ('cord.run.id', 'wrong'),
                                        ('cord.node.name', 'wrong')])
def test_complete_but_changed_records_fail(source, field, value):
    spans = read_spans([source])
    target = next(s for s, _ in spans.values() if s['attributes'].get('cord.outcome') == 'escalated')
    target['attributes'][field] = value
    assert compare([source], Reader(spans), reference=REFERENCE)['status'] == 'mismatch'


def test_subject_mapping_checked(source):
    reader = Reader(read_spans([source]))
    original = reader.trace
    reader.trace = lambda *args: (original(*args)[0], 'wrong subject')
    assert compare([source], reader, reference=REFERENCE)['status'] == 'mismatch'


def test_missing_passing_span_is_pending_even_with_equal_counts(source):
    spans = read_spans([source])
    sid = next(sid for sid, (s, _) in spans.items() if s['attributes'].get('cord.outcome') == 'passed'
               and s['name'] == 'attempt')
    del spans[sid]
    result = compare([source], Reader(spans), reference=REFERENCE, wait=.01, poll=.001)
    assert result['status'] == 'timeout' and result['langfuse'] is None


def test_invalid_archive_precedes_network():
    reader = Reader({})
    with pytest.raises(ArchiveError):
        compare([FIXTURES / 'broken-parent.otlp.jsonl'], reader, reference=REFERENCE)
    assert reader.calls == 0


@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan')])
def test_wait_must_be_bounded(source, value):
    with pytest.raises(ValueError):
        compare([source], Reader({}), reference=REFERENCE, wait=value)


def row():
    return {'id': '0000000000000003', 'traceId': '0' * 31 + '1',
            'parentObservationId': '0000000000000002', 'name': 'attempt',
            'startTime': '2026-09-12T00:00:00.000Z', 'endTime': '2026-09-12T00:00:00.000Z',
            'metadata': {'cord_start_ns': str(REFERENCE), 'cord_end_ns': str(REFERENCE + 1),
                         'cordboard': {'cord.outcome': 'escalated', 'cord.step.attempt': 1}}}


def test_public_mapping_preserves_nanoseconds_without_model_fields():
    span = observation(row())
    assert span['endTimeUnixNano'] == REFERENCE + 1
    assert span['attributes'] == {'cord.outcome': 'escalated', 'cord.step.attempt': 1}


@pytest.mark.parametrize('mutation', [lambda r: r['metadata'].pop('cord_end_ns'),
                                     lambda r: r.update(id='invalid'),
                                     lambda r: r.update(endTime='2026-09-13T00:00:00Z')])
def test_malformed_mapping_is_not_empty(mutation):
    value = row(); mutation(value)
    with pytest.raises(ReadError, match='invalid_response'):
        observation(value)


def test_public_pagination_and_subject(monkeypatch):
    reader = PublicReader('http://localhost:3300', 'fixture', 'fixture')
    first = row(); second = row(); second['id'] = '0000000000000004'
    calls = []
    def get(path, params, deadline):
        calls.append((path, params))
        if path.startswith('traces/'):
            return {'id': first['traceId'], 'sessionId': 'subject'}
        return {'data': [first if params['page'] == 1 else second],
                'meta': {'page': params['page'], 'totalPages': 2, 'totalItems': 2}}
    monkeypatch.setattr(reader, 'get', get)
    spans, subject = reader.trace(first['traceId'], time.monotonic() + 1)
    assert len(spans) == 2 and subject == 'subject'
    assert [p['page'] for _, p in calls[:2]] == [1, 2]
    assert not any('fromStartTime' in p for _, p in calls)
    reader.close()


@pytest.mark.parametrize('code,status', [(401, 'api_error'), (403, 'api_error'), (404, 'api_error'), (302, 'api_error'),
                                        (429, 'unavailable'), (500, 'unavailable')])
def test_http_failure_diagnostics_exclude_payload(monkeypatch, code, status):
    reader = PublicReader('http://localhost:3300', 'fixture', 'fixture')
    class Response:
        status_code = code
        text = 'must not appear'
    def get(*args, **kwargs):
        assert kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(reader.session, 'get', get)
    with pytest.raises(ReadError, match='^' + status + '$'):
        reader.get('observations', {}, time.monotonic() + 1)
    reader.close()


def test_extra_span_in_another_trace_cannot_be_hidden_by_id_overwrite(source):
    reader = Reader(read_spans([source]))
    tids = sorted({s['traceId'] for s, _ in reader.spans.values()})
    original = reader.trace
    last = next(s for s, _ in reader.spans.values() if s['traceId'] == tids[-1])
    def extra(tid, deadline):
        found, subject = original(tid, deadline)
        if tid == tids[0]:
            shadow = deepcopy(last)
            shadow['traceId'] = tid
            found[shadow['spanId']] = shadow
        return found, subject
    reader.trace = extra
    assert compare([source], reader, reference=REFERENCE)['status'] == 'mismatch'
