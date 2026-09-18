from copy import deepcopy
import gzip
import json
from pathlib import Path
import subprocess
import sys

import pytest

from cord_runtime.archive_query import ArchiveError, query, reference_ns
from cord_runtime.execution import SEMCONV_VERSION

FIXTURES = Path(__file__).parent / 'fixtures/escalations'
REFERENCE = reference_ns('2026-09-12T00:00:00Z')
# run-1 (graph "alpha") in window.otlp.jsonl: run 0000000000000001, its Step
# "draft" 0000000000000002, Subject urn:test:6, trace 00..01.
RUN_1_TRACE_ID = '00000000000000000000000000000001'
RUN_1_RUN_SPAN_ID = '0000000000000001'
RUN_1_STEP_SPAN_ID = '0000000000000002'


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


@pytest.mark.parametrize('version', ['0.2.0', '0.3.0'])
def test_optional_tier_respects_archive_version(tmp_path, version):
    def mutate(spans):
        for span in spans:
            if span['name'] == 'run':
                change_attr(span, 'cord.semconv.version', version)
            change_attr(span, 'cord.tier', None)
            change_attr(span, 'gen_ai.request.model', None)
    path = write_mutation(tmp_path, mutate)
    if version == '0.2.0':
        with pytest.raises(ArchiveError, match='Attempt Tier'):
            query([path], reference=REFERENCE)
    else:
        # The exact same declared outcomes count without model or Tier data.
        original = json.loads((FIXTURES / 'window.otlp.jsonl').read_text().splitlines()[0])
        baseline = tmp_path / 'baseline.otlp.jsonl'
        baseline.write_text(json.dumps(original) + '\n')
        assert query([path], reference=REFERENCE) == query([baseline], reference=REFERENCE)


@pytest.mark.parametrize('tier', ['', '   '])
def test_optional_tier_still_validated_when_present(tmp_path, tier):
    def mutate(spans):
        for span in spans:
            if span['name'] == 'run':
                change_attr(span, 'cord.semconv.version', '0.3.0')
        change_attr(spans[0], 'cord.tier', tier)
    with pytest.raises(ArchiveError, match='Attempt Tier'):
        query([write_mutation(tmp_path, mutate)], reference=REFERENCE)


# --- nested Step under a declared child-graph call (ADR-0021, #86) ---------

def _add_step(spans, *, span_id, parent_id, node, graph_id='alpha', run_id='run-1',
              subject_type='fixture', subject_id='urn:test:6', outcome='passed',
              start=1784332799999999998, end=1784332800000000003, resumed_from=None):
    attrs = [
        {'key': 'cord.graph.id', 'value': {'stringValue': graph_id}},
        {'key': 'cord.run.id', 'value': {'stringValue': run_id}},
        {'key': 'cord.subject.id', 'value': {'stringValue': subject_id}},
        {'key': 'cord.subject.type', 'value': {'stringValue': subject_type}},
        {'key': 'cord.node.name', 'value': {'stringValue': node}},
        {'key': 'cord.outcome', 'value': {'stringValue': outcome}},
    ]
    if resumed_from is not None:
        attrs.append({'key': 'cord.resumed_from', 'value': {'stringValue': resumed_from}})
    spans.append({'traceId': RUN_1_TRACE_ID, 'spanId': span_id, 'parentSpanId': parent_id,
                  'name': f'step:{node}', 'startTimeUnixNano': str(start), 'endTimeUnixNano': str(end),
                  'attributes': attrs})


def _bump_run_1_to_current_semconv(spans):
    run_span = next(s for s in spans if s['spanId'] == RUN_1_RUN_SPAN_ID)
    change_attr(run_span, 'cord.semconv.version', SEMCONV_VERSION)


def test_nested_step_under_step_is_rejected_on_a_prior_semconv_version(tmp_path):
    # window.otlp.jsonl's run-1 predates #86 (cord.semconv.version 0.2.0):
    # a Step nested under a Step there is corruption, never new evidence.
    def mutate(spans):
        _add_step(spans, span_id='00000000000000f0', parent_id=RUN_1_STEP_SPAN_ID, node='child-a')
    path = write_mutation(tmp_path, mutate)
    with pytest.raises(ArchiveError, match='nested Step under Step requires current'):
        query([path], reference=REFERENCE)


def test_nested_step_under_step_is_accepted_under_current_semconv_version(tmp_path):
    def mutate(spans):
        _bump_run_1_to_current_semconv(spans)
        _add_step(spans, span_id='00000000000000f0', parent_id=RUN_1_STEP_SPAN_ID, node='child-a')
    path = write_mutation(tmp_path, mutate)
    query([path], reference=REFERENCE)  # does not raise


def test_nested_attempt_under_a_child_step_still_counts_escalation_by_its_own_node(tmp_path):
    def mutate(spans):
        _bump_run_1_to_current_semconv(spans)
        _add_step(spans, span_id='00000000000000f0', parent_id=RUN_1_STEP_SPAN_ID, node='child-a')
        spans.append({'traceId': RUN_1_TRACE_ID, 'spanId': '00000000000000f1', 'parentSpanId': '00000000000000f0',
                      'name': 'attempt', 'startTimeUnixNano': '1784332800000000000',
                      'endTimeUnixNano': '1784332800000000001',
                      'attributes': [
                          {'key': 'cord.graph.id', 'value': {'stringValue': 'alpha'}},
                          {'key': 'cord.run.id', 'value': {'stringValue': 'run-1'}},
                          {'key': 'cord.subject.id', 'value': {'stringValue': 'urn:test:6'}},
                          {'key': 'cord.subject.type', 'value': {'stringValue': 'fixture'}},
                          {'key': 'cord.node.name', 'value': {'stringValue': 'child-a'}},
                          {'key': 'cord.outcome', 'value': {'stringValue': 'escalated'}},
                          {'key': 'cord.step.attempt', 'value': {'intValue': 1}},
                      ]})
    path = write_mutation(tmp_path, mutate)
    baseline = query([FIXTURES / 'window.otlp.jsonl'], reference=REFERENCE)
    rows = query([path], reference=REFERENCE)
    assert {'graph': 'alpha', 'node': 'child-a', 'count': 1} in rows
    # The pre-existing, unrelated rows are unaffected by the added evidence.
    assert all(row in rows for row in baseline if row['node'] != 'child-a')


def test_nested_step_still_requires_matching_identity(tmp_path):
    def mutate(spans):
        _bump_run_1_to_current_semconv(spans)
        _add_step(spans, span_id='00000000000000f0', parent_id=RUN_1_STEP_SPAN_ID,
                  node='child-a', graph_id='other-graph')
    path = write_mutation(tmp_path, mutate)
    with pytest.raises(ArchiveError, match='identity mismatch'):
        query([path], reference=REFERENCE)


def test_nested_step_parented_on_an_attempt_is_an_invalid_hierarchy(tmp_path):
    def mutate(spans):
        _bump_run_1_to_current_semconv(spans)
        # 0000000000000003 is one of run-1's own Attempt spans.
        _add_step(spans, span_id='00000000000000f0', parent_id='0000000000000003', node='child-a')
    path = write_mutation(tmp_path, mutate)
    with pytest.raises(ArchiveError, match='invalid step parent hierarchy'):
        query([path], reference=REFERENCE)
