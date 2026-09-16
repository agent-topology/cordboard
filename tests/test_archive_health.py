import gzip
import json
from pathlib import Path

import pytest

from cord_runtime.archive_health import HealthStatus, archive_health, read_active_spans
from cord_runtime.archive_query import ArchiveError, query, reference_ns
from conftest import CREDENTIAL

FIXTURES = Path(__file__).parent / "fixtures/escalations"
REFERENCE = reference_ns("2026-09-12T00:00:00Z")
COMPLETE_LINE = (FIXTURES / "window.otlp.jsonl").read_text().splitlines()[0]


def test_pending_when_no_archive_files(tmp_path):
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.PENDING
    assert health.span_count == 0
    assert health.incomplete_files == ()


def test_empty_when_file_exists_with_no_spans(tmp_path):
    (tmp_path / "archive-2026-01-01.otlp.jsonl").write_text("")
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.EMPTY
    assert health.span_count == 0


def test_incomplete_tail_tolerated_alongside_complete_spans(tmp_path):
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    truncated = COMPLETE_LINE[: len(COMPLETE_LINE) // 2]
    path.write_text(COMPLETE_LINE + "\n" + truncated)
    line_spans = len(json.loads(COMPLETE_LINE)["resourceSpans"][0]["scopeSpans"][0]["spans"])

    health = archive_health([tmp_path])
    assert health.status is HealthStatus.INCOMPLETE
    assert health.span_count == line_spans
    assert health.incomplete_files == (path.name,)
    assert health.detail is None

    spans, incomplete = read_active_spans([tmp_path])
    assert len(spans) == line_spans
    assert incomplete == (path.name,)


def test_incomplete_only_content_is_incomplete_not_failed(tmp_path):
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(COMPLETE_LINE[:10])
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.INCOMPLETE
    assert health.span_count == 0
    assert health.incomplete_files == (path.name,)


def test_corrupt_complete_record_still_fails(tmp_path):
    record = json.loads(COMPLETE_LINE)
    record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "not-hex"
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(json.dumps(record) + "\n")
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.FAILED
    assert health.span_count == 0
    assert "invalid OTLP trace/span identifier" in health.detail


def test_corrupt_earlier_line_is_not_tolerated_by_a_valid_tail(tmp_path):
    record = json.loads(COMPLETE_LINE)
    del record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(json.dumps(record) + "\n" + COMPLETE_LINE + "\n")
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.FAILED


def test_healthy_when_every_line_is_complete(tmp_path):
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(COMPLETE_LINE + "\n")
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.HEALTHY
    assert health.span_count > 0
    assert health.incomplete_files == ()


def test_replayed_spans_do_not_duplicate(tmp_path):
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(COMPLETE_LINE + "\n")
    once = archive_health([tmp_path]).span_count
    path.write_text(COMPLETE_LINE + "\n" + COMPLETE_LINE + "\n")
    twice = archive_health([tmp_path]).span_count
    assert once == twice


def test_gzip_partitions_are_always_read_strictly(tmp_path):
    record = json.loads(COMPLETE_LINE)
    del record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
    path = tmp_path / "archive-2026-01-01.otlp.jsonl.gz"
    with gzip.open(path, "wt") as handle:
        handle.write(json.dumps(record) + "\n")
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.FAILED
    assert health.incomplete_files == ()


def test_health_diagnostics_never_leak_archive_payload(tmp_path):
    record = json.loads(COMPLETE_LINE)
    record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
        {"key": "detail", "value": {"stringValue": CREDENTIAL}}
    )
    del record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(json.dumps(record) + "\n")
    health = archive_health([tmp_path])
    assert health.status is HealthStatus.FAILED
    assert CREDENTIAL not in health.detail


def test_original_growing_file_is_snapshotted_before_parsing(tmp_path):
    """The tolerant read must evaluate a fixed snapshot: appending more data
    to the source file after the trailing line was already set aside must
    not change what was already classified as HEALTHY."""
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(COMPLETE_LINE + "\n")
    spans_before, incomplete_before = read_active_spans([tmp_path])
    with open(path, "a") as handle:
        handle.write(COMPLETE_LINE[:20])
    assert incomplete_before == ()
    assert len(spans_before) > 0


def test_query_still_requires_strict_complete_reads(tmp_path):
    """archive_query.query (the escalation contract) is untouched by the new
    tolerant path -- an in-progress tail still fails it outright."""
    path = tmp_path / "archive-2026-01-01.otlp.jsonl"
    path.write_text(COMPLETE_LINE + "\n" + COMPLETE_LINE[:20])
    with pytest.raises(ArchiveError):
        query([tmp_path], reference=REFERENCE)
