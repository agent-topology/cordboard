from copy import deepcopy
import gzip
import json
from pathlib import Path
import subprocess
import sys

import pytest

from cord_runtime.archive_query import ArchiveError, query, reference_ns

FIXTURES = Path(__file__).parent / 'fixtures/escalations'
REFERENCE = reference_ns('2026-09-12T00:00:00Z')


def test_fixed_window_and_top_order():
    source = FIXTURES / 'window.otlp.jsonl'
    assert query([source], reference=REFERENCE) == json.loads((FIXTURES / 'expected.json').read_text())
    assert query([source], reference=REFERENCE, top=2) == json.loads((FIXTURES / 'top2.expected.json').read_text())
    assert query([source], reference=reference_ns('2027-01-01T00:00:00Z')) == []


def test_retransmission_gzip_partitions_and_order(tmp_path):
    lines = (FIXTURES / 'window.otlp.jsonl').read_text().splitlines()
    original = query([FIXTURES / 'window.otlp.jsonl'], reference=REFERENCE)
    first = json.loads(lines[0])
    first['resourceSpans'][0]['scopeSpans'][0]['spans'].reverse()
    (tmp_path / 'one.otlp.jsonl').write_text(json.dumps(first)+'\n')
    with gzip.open(tmp_path / 'two.otlp.jsonl.gz', 'wt') as f:
        f.write(lines[1]+'\n'+lines[1]+'\n')
    assert query([tmp_path], reference=REFERENCE) == original


def test_broken_parent_fixture():
    with pytest.raises(ArchiveError, match='invalid step parent hierarchy'):
        query([FIXTURES / 'broken-parent.otlp.jsonl'], reference=REFERENCE)


def write_mutation(tmp_path, mutate):
    record = json.loads((FIXTURES / 'window.otlp.jsonl').read_text().splitlines()[0])
    spans = record['resourceSpans'][0]['scopeSpans'][0]['spans']
    mutate(spans)
    path = tmp_path / 'invalid.otlp.jsonl'
    path.write_text(json.dumps(record)+'\n')
    return path


def change_attr(span, key, value):
    span['attributes'] = [a for a in span['attributes'] if a['key'] != key]
    if value is not None:
        span['attributes'].append({'key':key, 'value':{'stringValue':value}})


@pytest.mark.parametrize('mutation,reason', [
    (lambda s: s[0].pop('parentSpanId'), 'step parent is missing'),
    (lambda s: s[0].update(parentSpanId='f'*16), 'step parent is missing'),
    (lambda s: s[0].update(traceId='f'*32), 'parent hierarchy'),
    (lambda s: change_attr(s[0], 'cord.graph.id', None), 'explicit Graph'),
    (lambda s: change_attr(s[0], 'cord.graph.id', 'other'), 'identity mismatch'),
    (lambda s: change_attr(s[0], 'cord.node.name', 'other'), 'Node identity mismatch'),
    (lambda s: change_attr(s[0], 'cord.outcome', 'repaired'), 'Attempt outcome'),
    (lambda s: change_attr(s[0], 'cord.step.attempt', '1'), 'positive attempt number'),
    (lambda s: s[0].update(endTimeUnixNano='bad'), 'timestamp'),
    (lambda s: change_attr(next(x for x in s if x['name']=='run'), 'cord.semconv.version', '0.1.0'), 'semantic convention'),
    (lambda s: change_attr(next(x for x in s if x['name'].startswith('step:')), 'cord.outcome', 'escalated'), 'Step outcome'),
])
def test_malformed_records_fail_even_outside_window(tmp_path, mutation, reason):
    path = write_mutation(tmp_path, mutation)
    with pytest.raises(ArchiveError, match=reason):
        query([path], reference=reference_ns('2027-01-01T00:00:00Z'))


def test_conflicting_duplicate_fails(tmp_path):
    def mutate(spans):
        duplicate = deepcopy(spans[0])
        change_attr(duplicate, 'cord.outcome', 'escalated')
        spans.append(duplicate)
    with pytest.raises(ArchiveError, match='conflicting duplicate'):
        query([write_mutation(tmp_path, mutate)], reference=REFERENCE)


@pytest.mark.parametrize('content', ['', '{}\n', 'not-json\n', '{"resourceSpans":[]}\n'])
def test_empty_or_invalid_input(tmp_path, content):
    path = tmp_path/'bad.otlp.jsonl'
    path.write_text(content)
    with pytest.raises(ArchiveError):
        query([path], reference=REFERENCE)


def test_missing_paths_and_invalid_limit(tmp_path):
    for paths in [[], [tmp_path], [tmp_path/'missing']]:
        with pytest.raises(ArchiveError):
            query(paths, reference=REFERENCE)
    with pytest.raises(ArchiveError, match='positive'):
        query([FIXTURES/'window.otlp.jsonl'], reference=REFERENCE, top=0)


def test_cli_outputs_and_errors():
    command = [sys.executable, '-m', 'cord_runtime.archive_query']
    result = subprocess.run(command + [str(FIXTURES/'window.otlp.jsonl'), '--reference-time', '2026-09-12T00:00:00Z', '--top', '2'], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == json.loads((FIXTURES/'top2.expected.json').read_text())
    for source, time in [('broken-parent.otlp.jsonl', '2026-09-12T00:00:00Z'), ('window.otlp.jsonl', '2026-09-12')]:
        result = subprocess.run(command+[str(FIXTURES/source),'--reference-time',time],capture_output=True,text=True)
        assert result.returncode == 2 and not result.stdout and result.stderr



def test_checked_in_collector_capture_and_legacy_rejection():
    root = Path(__file__).parents[1]
    expected = json.loads((root/'examples/archive.graph-id.expected.json').read_text())
    assert query([root/'examples/archive.graph-id.sample.otlp.jsonl'],
                 reference=reference_ns(expected['reference_time'])) == expected['rows']
    with pytest.raises(ArchiveError, match='explicit Graph'):
        query([root/'examples/archive.sample.otlp.jsonl'], reference=REFERENCE)
