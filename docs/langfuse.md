# Langfuse comparison (#10)

The Collector exports accepted telemetry to the independent OTLP archive and a
self-hosted Langfuse instance. `langfuse-compare` reads the same complete Run
cohort through the Public API, checks identity and Cordboard metadata, and
compares eight-week Graph/Node escalation counts. The archive remains authoritative.
No model, Tier, provider account, Langfuse SDK callback, or enterprise feature is
required. This implements the refined #10 contract after #7; it fits one PR.

## Pinned public contracts

This integration targets **Langfuse 3.225.7**, not the current Cloud/v4 data model.
The web, worker, Postgres, Redis, ClickHouse and MinIO images are pinned by digest
in [the local Compose file](../langfuse/compose.yaml). Collector contrib remains
**0.148.0**. Existing Python dependencies and both uv lockfiles remain in force.
The v3 Observations API is a deliberate compatibility choice for this deployment;
upgrading to v4 requires a separate mapping and ingestion verification.

| Public interface | Use |
| --- | --- |
| `POST /api/public/otel/v1/traces` | Collector OTLP/HTTP export, Basic project-key authentication |
| `GET /api/public/observations?traceId=…&page=…&limit=100` | Read every observation of each archived trace, including all pages |
| `GET /api/public/traces/{traceId}` | Check Subject via `sessionId` |
| `GET /api/public/observations/{observationId}` | Inspect individual execution and optional graph metadata |

Contracts: [pinned v1 observations definition](https://github.com/langfuse/langfuse/blob/v3.225.7/fern/apis/server/definition/legacy/observations-v1.yml),
[pinned public response types](https://github.com/langfuse/langfuse/blob/v3.225.7/fern/apis/server/definition/commons.yml),
[official OTLP mapping](https://langfuse.com/integrations/native/opentelemetry),
and [upstream deployment template](https://github.com/langfuse/langfuse/blob/v3.225.7/docker-compose.yml).
The live checks below establish compatibility for these pins, not newer releases.
ClickHouse is an internal Langfuse dependency; Cordboard does not query it.

## Export mapping and isolation

Start the Collector with [config.yaml](../collector/config.yaml) followed by
[langfuse.yaml](../collector/langfuse.yaml). Both pipelines apply the same
`filter/redacted` gate. Only the Langfuse pipeline applies the vendor mapping;
the archive pipeline retains its original span envelope and UTC receipt partition.
The receiver remains local. There is no direct graph-to-Langfuse telemetry path.

| Execution record | Public representation |
| --- | --- |
| Run trace ID | `traceId`; the Run root is also a `SPAN` observation |
| Step / Attempt span ID and parent | `id`, `parentObservationId`, original `name` |
| `cord.subject.id` | `langfuse.session.id` → trace `sessionId` |
| Accepted span attributes | JSON metadata object `metadata.cordboard`, retaining types and original keys |
| Start / end nanoseconds | Decimal strings `metadata.cord_start_ns`, `metadata.cord_end_ns` |
| Optional graph Verdict/rules | Preserved in `metadata.cordboard` only when supplied; never interpreted |

The v3 catch-all metadata path stringifies non-string attributes. The adapter
therefore uses the documented JSON `langfuse.observation.metadata` field with a
separate `cordboard` object. It copies already-redacted values and adds no graph
judgments. Native observation timestamps have millisecond precision; the exact
OTLP times travel as strings to avoid rounding at the exclusive lower or inclusive
upper window boundary. Reads cross-check native timestamps within that precision.
These vendor fields are added only to the export copy, never to existing archives
or the execution semantic convention. All observations use `SPAN`; generation,
model and Tier fields are unnecessary. Original optional attributes remain visible.

Langfuse export uses a bounded in-memory queue and retries for up to 30 seconds.
It is not a durable delivery guarantee. A stalled destination can exhaust its
queue and cause receiver errors; archive writes already accepted remain available,
and retransmissions are deduplicated by the query. Closing the Collector may wait
for pending exports. Do not interpret an HTTP export acceptance as completed
Langfuse ingestion.

## Local deployment

Install the pinned Collector and scanner as described in [archive setup](archive.md).
Docker must be running. Only the web port is published, on loopback; the five
supporting services remain on the isolated Compose network. This is a local test
installation, without production availability, backup or scaling claims.

```sh
uv sync --locked --python 3.12
uv sync --locked --project aegra
uv run --locked python -m examples.langfuse_setup .tools/langfuse.env
docker compose --env-file .tools/langfuse.env -p cordboard-langfuse \
  -f langfuse/compose.yaml config --quiet
docker compose --env-file .tools/langfuse.env -p cordboard-langfuse \
  -f langfuse/compose.yaml up -d
curl --fail --max-time 5 http://127.0.0.1:3300/api/public/health
```

The setup command creates a mode-0600 environment file and refuses to overwrite
one. Reuse the file on subsequent starts. It generates only local service/project
credentials, never model-provider configuration. The file belongs under ignored
`.tools/`; do not commit it. The UI is at `http://localhost:3300`, with user
`operator@localhost.test` and the generated `LANGFUSE_USER_PASSWORD` in that file.
If the health request fails during startup, retry within a bounded startup budget
(e.g. 90 seconds); do not run comparison against an unready server.

Load the environment in the terminal that starts the Collector and comparison:

```sh
set -a
. .tools/langfuse.env
set +a
export CORD_LANGFUSE_URL=http://127.0.0.1:3300
export CORD_ARCHIVE_DIR="$PWD/archive/langfuse"
export TZ=UTC
mkdir -p "$CORD_ARCHIVE_DIR"
.tools/otelcol-contrib validate --config collector/config.yaml --config collector/langfuse.yaml
.tools/otelcol-contrib --config collector/config.yaml --config collector/langfuse.yaml
```

Emit the existing `examples.archive_fixture` in another terminal, or execute the
model-free `aegra/opaque.json` graph through the [existing client](aegra.md).
Stop the producer and Collector to close the archive before comparing. Supply an
explicit reference time appropriate to the records:

```sh
uv run --locked langfuse-compare archive/langfuse \
  --reference-time 2026-09-12T00:00:00Z --top 10 --wait 60
```

For a different local web port, set `CORD_LANGFUSE_PORT` before Compose startup
and use the corresponding origin in `CORD_LANGFUSE_URL` and the health request.
Stop this installation with the same Compose project name and `down`; its named
volumes persist. No existing Docker projects, databases or volumes are reused.

## Comparison semantics and failure behavior

Inputs must contain complete, closed Run trees, including parents outside the
query window. The comparator freezes these files before network access and
validates them with the archive contract. Existing 0.2.0 Runs retain their Tier
requirement; 0.3.0 accepts Tier-free Attempts. Conflicting archive duplicates,
missing parents, missing files and empty input are errors, not empty results.

The comparison cohort is **exactly the trace IDs in the supplied archive**, across
all of their Graphs. It does not silently include unrelated project traffic or
claim that an archive snapshot represents every execution ever sent. All pages
are read without start-time filters: an Attempt may start outside the window and
finish inside it. Completion requires every expected span, a finished observation,
and the trace record. Additional observations or inconsistent identities are a
mismatch. This checks received records, not hidden graph behavior or unrecorded Runs.

Both paths use `(reference − 56 days, reference]`, exact Attempt end times,
explicit `cord.outcome=escalated`, span-ID deduplication and parent/identity
validation. All Graph/Node counts are compared **before** top-N truncation;
output ordering is count descending, then Graph and Node ascending. Absence of an
escalation declaration means no observed escalation, not proof about graph internals.

| JSON `status` | Meaning |
| --- | --- |
| `match` | Complete cohort, matching execution fields/Subject and all counts; `[]` is legitimate only here |
| `mismatch` | Complete observations differ in execution fields, hierarchy, Subject or counts |
| `timeout` | Required observations/trace or finished records remained missing at the deadline |
| `unavailable` | Last read failed through transport, rate limiting or server error; bounded retries expired |
| `api_error` | Authentication, unsupported endpoint or other non-retryable HTTP response |
| `invalid_response` | Unsupported/malformed public mapping or pagination |

`archive` remains in every comparison result, even if the public read fails.
`langfuse: null` means no valid completed result, not zero. CLI exits 0 only for
`match`, 1 for comparison/read failures, 2 for invalid configuration/archive.
Requests use finite timeouts, no environment proxy or redirects; the polling
budget bounds retries and pagination. A request already in flight can consume its
remaining socket timeout. Diagnostics contain fixed statuses, not response bodies
or credentials. Missing mappings fail visibly; there is no ClickHouse fallback.

## Verification and handoff

Measurement: public mapping, window/filter/deduplication parity, ingestion
completion and archive availability. Inputs: the existing small known-answer
fixture (31 unique spans), fresh trace/span IDs, retransmissions, mixed 0.2/0.3
Runs, one rejected unstamped trace, and one live model-free Aegra Run (3 spans).
The static fixture supplies timestamps around the eight-week boundaries; there is
no eight-week live wait or large generated workload. Added graph-owned sample
Verdict metadata tests transport only, without defining a platform Verdict schema.

```sh
# Uses the generated local credentials loaded above.
export CORD_LANGFUSE_TEST=1
uv run --locked pytest -q tests/test_archive_query.py tests/test_langfuse_query.py
uv run --locked pytest -q tests/test_langfuse_integration.py
uv run --locked --group proxy pytest -q
```

`tests/test_langfuse_integration.py` requires the pinned local stack and explicitly
opts in through `CORD_LANGFUSE_TEST=1`; otherwise it reports skips. It reuses
`examples/opaque_graph.py`, `aegra/opaque.json`, the existing Aegra client, and
real Collector/scanner binaries. The failure test disables exporter retries only
in its temporary config and points to a reserved non-listening local port, proving
that a failed branch does not erase the archive. Unit tests cover delayed reads,
missing passing spans despite equal counts, empty completed counts, bad responses,
HTTP failures, pagination, metadata changes and invalid input before network access.

Known answer: `alpha/draft=2`, `alpha/verify=1`, `beta/draft=1`,
`boundary/draft=1`. Model-free live result: three accessible observations and no
observed escalations. Both archive scans pass.

Actual verification on 2026-09-11 (macOS arm64, Docker 28.3.3):

| Command / check | Result |
| --- | --- |
| `uv sync --locked --python 3.12` and `uv sync --locked --project aegra` | Both environments synchronized |
| Compose `config --quiet` and Collector `validate` with both configs | Exit 0 |
| `uv run --locked pytest -q tests/test_archive_query.py tests/test_langfuse_query.py` | 54 passed |
| `CORD_LANGFUSE_TEST=1 uv run --locked --group proxy pytest -q` with generated credentials loaded | **144 passed in 63.55 seconds**, no skips |
| Real dual export and public read | 31 unique spans, exact four-group known answer; Tier-free escalation included |
| Real Aegra model-free Run | Three observations, correct Subject, no model/Tier attributes, empty completed escalation result |
| Failed Langfuse exporter | Archive still returns the known answer; comparison reports `unavailable`, not zero |
| `langfuse-compare <fresh archive> --reference-time <UTC now> --wait 10` | CLI exit 0: `match`, 5/5 spans, `archive-fixture/draft=1` in both destinations |
| `archive-check <fresh archive> --cli <pinned scanner>` | Exit 0: 5 records, `findings=False`, `failure=False`; archive has no Langfuse mapping attributes |
| Local Markdown reference check | 100 relative file references, no missing targets |

The verification stack used the isolated Compose project `cordboard-issue10`;
other Docker projects were left untouched. No provider smoke, v4 compatibility,
production load test or hosted CI run is claimed by this historical evidence.
The current [CI workflow](../.github/workflows/ci.yml) excludes Langfuse integration tests.

Implementation entrypoints: [public reader/comparator](../src/cord_runtime/langfuse_query.py),
[shared archive query](../src/cord_runtime/archive_query.py),
[Collector branch](../collector/langfuse.yaml), [local setup](../examples/langfuse_setup.py),
[unit checks](../tests/test_langfuse_query.py), and
[live checks](../tests/test_langfuse_integration.py).
No separate Task boundary or unresolved public API workaround is needed.
