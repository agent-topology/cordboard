# Collector delivery health and trustworthy archive ingestion (#45)

Follow-up to [#5](archive.md)/[#6](archive-query.md)/[#10](langfuse.md). Those
slices verify the gate and the escalation-query contract but give no way to
tell "nothing happened yet" apart from "the Collector is down" or "a file is
mid-write." This slice adds two small, read-only surfaces:

- `cord_runtime.archive_health` -- a sanitized, evidence-based classification
  of what an archive directory currently contains, tolerant of exactly one
  in-progress trailing write per file.
- `cord_runtime.collector_health` -- reachability of the Collector process
  itself, independent of whether any graph Run succeeded or failed.

Neither replaces [`archive-escalations`](archive-query.md), which stays the
strict record-of-origin reader: it still requires complete Run trees and
still fails outright on any invalid or in-progress file. This slice answers a
different question -- "can I trust what's here right now" -- not "what is the
eight-week escalation count."

## Vocabulary

`archive_health()` returns one `HealthStatus`:

| Status | Meaning |
| --- | --- |
| `PENDING` | No archive files exist yet for the given paths. Absence, not failure -- #45 AC1 requires a positive control before absence is read as anything stronger. |
| `EMPTY` | Archive files exist but hold no complete spans yet (freshly created, or the only content is an in-progress write). |
| `INCOMPLETE` | At least one complete span was read; the most recent file's trailing line looks like an append still in flight. Every earlier complete record still reads normally. |
| `FAILED` | A *complete* record failed to parse or violates the archive contract (`archive_query.ArchiveError`). A real corruption/loss signal, never concealed as `EMPTY`. |
| `HEALTHY` | At least one span read successfully and no file's tail looked incomplete. |

`check_collector_health()` returns `REACHABLE` or `UNAVAILABLE` from one
bounded HTTP probe of the Collector's own `health_check` extension --
`collector/config.yaml` enables it at `CORD_HEALTH_ENDPOINT` (default
`127.0.0.1:13133`), a supported operational interface distinct from the OTLP
receiver a producer exports to (see [Collector health check
extension](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/extension/healthcheckextension/README.md)).
`wait_for_collector_recovery()` polls it up to an explicit `timeout` and
returns whether reachability was observed within that bound; it never blocks
forever waiting for a Collector that does not come back.

`RedactingOTLPExporter.last_export` (`cord_runtime.telemetry.ExportOutcome`)
records the most recent export attempt's `delivered` flag and, on failure,
one fixed `reason`: `unreachable`, `http_error`, `rejected_spans`, or
`processing_failed`. `rejected_spans` (an integer count) is populated only
when the Collector's own OTLP `partial_success` response explicitly reported
one; it is never invented for the other reasons, matching the requirement not
to guess a rejection count the pinned Collector cannot itself supply.

All diagnostic strings above -- `ArchiveHealth.detail`, log messages, and
`ExportOutcome.reason` -- are fixed and value-free, the same discipline
[#5](archive.md#contracts-and-ownership) already applies to `ArchiveError`
and export-failure logging. None of them echo archive payload, a response
body, a header, or a URL.

## Active-file tolerance and no double-counting

`archive_query.read_spans` fails the whole read on any unparseable line,
including the Collector's own file exporter's trailing, not-yet-flushed
write -- correct for `archive-escalations`, unusable as a live health signal.
`archive_health.read_active_spans` snapshots each plain `.otlp.jsonl` file
(a `.gz` partition is already closed and is always read strictly) and, only
if its very last line fails to parse as JSON, sets that one line aside as
"in flight" rather than failing the read. Any other parse or contract
failure -- including the same syntax error on an earlier line, or a
syntactically complete but semantically invalid record -- still raises
`ArchiveError` exactly as `read_spans` does. This is a narrow, deliberate
distinction: an unparseable trailing line is definitionally incomplete (it
never finished arriving), while a complete line that is merely wrong is
still real corruption.

Retransmitted spans already deduplicate by `span_id` in `read_spans`
(ADR-0005); `archive_health`/`read_active_spans` inherit that unchanged, so a
replayed identical export is not double-counted (verified against a real
Collector in `test_replay_through_real_collector_does_not_duplicate_health_spans`).

## Usage

```sh
uv run --locked archive-health path/to/archive
```

```json
{
  "status": "healthy",
  "span_count": 5,
  "incomplete_files": [],
  "detail": null
}
```

An archive directory with nothing written yet:

```json
{
  "status": "pending",
  "span_count": 0,
  "incomplete_files": [],
  "detail": "no archive files present yet"
}
```

```python
from cord_runtime.collector_health import check_collector_health, wait_for_collector_recovery

check_collector_health("http://127.0.0.1:13133/")          # "reachable" | "unavailable"
wait_for_collector_recovery("http://127.0.0.1:13133/", timeout=30)  # bounded bool
```

## Verification

Commands run locally on 2026-09-16 (macOS arm64; sample timestamps use UTC):

| Command | Result |
| --- | --- |
| `uv sync --locked` | Python 3.12.14; locked dependencies installed |
| SHA256 verification of both pinned binaries against release manifests | Passed |
| `.tools/otelcol-contrib validate --config collector/config.yaml` (with `health_check` extension) | Exit 0 |
| `uv run --locked archive-health examples/archive.graph-id.sample.otlp.jsonl` | `status=healthy span_count=5` |
| `uv run --locked archive-health <fresh empty dir>` | `status=pending` |
| `uv run --locked pytest -q tests/test_archive_health.py` | 12 passed (synthetic tail/corruption/dedup/sanitization cases) |
| `uv run --locked pytest -q tests/test_telemetry.py` | 16 passed, including the four `ExportOutcome` reasons |
| `OTELCOL=.tools/otelcol-contrib uv run --locked pytest -q -m collector tests/test_collector.py` | 16 passed, including real stop/restart recovery, real-Collector unreachable-export, and real-Collector replay dedup |
| `OTELCOL=.tools/otelcol-contrib REDACT_SECRET_CLI=.tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin uv run --locked pytest -q -m "not aegra and not langfuse"` | 539 passed, 7 failed (pre-existing, unrelated: `tests/test_proxy.py` requires the optional `litellm[proxy]` dependency group, not installed here), 9 deselected |

`tests/test_aegra.py` (Docker Postgres, `--group proxy`, `--project aegra`)
and the opt-in Langfuse stack were not run for this change; the modified
`collector()` test fixture only adds a `.health` attribute to the existing
string endpoint object (`CollectorEndpoint(str)`), and every existing call
site keeps using it as a plain string, so this is a low-risk, unverified gap
rather than a claimed pass.

The measurement is health-classification correctness against small,
purpose-built fixtures (a truncated line, a semantically invalid complete
record, a replayed identical export, a real stop/restart), not throughput or
long-term uptime. `HEALTHY`/`FAILED`/`INCOMPLETE` classification and the
Collector reachability/recovery bound are exercised against the real pinned
Collector 0.148.0 binary, not only synthetic files.

## Out of scope

Replacing the archive with a database, direct ClickHouse reads, a full
observability platform, exactly-once telemetry promises, and wiring this
into `cord view` or any web surface -- this slice publishes the read-only
contract those would consume, not the UI itself.
