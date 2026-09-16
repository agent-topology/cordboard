# Redacted execution archive (#5)

This checkout implements the archive boundary with a small Python fixture. It
also contains the LangGraph example from prerequisite #4, which remains separate
from the archive fixture. The reusable
instrumentation accepts graph-owned outcomes; the fixture explicitly declares
`fast/failed → fast/escalated → deep/passed` without implementing graph routing.
The [LiteLLM integration](model-proxy.md) now connects the graph to this path.
[Aegra execution](aegra.md) now verifies this path with Postgres checkpoints.
[Langfuse comparison](langfuse.md) adds the second gated export and public read path.
DeepAgents, lifecycle commands, and CI remain future work.

## Setup

Verified on macOS arm64, Python 3.12.14. Python dependencies are in
[pyproject.toml](../pyproject.toml) and [uv.lock](../uv.lock):
`redact-secret==0.1.0b1`, OTel SDK/OTLP `1.39.1`, protobuf `6.33.6`, requests
`2.34.2`, and pytest `9.1.1`. No provider credential or Docker daemon is needed.

```sh
uv sync --locked
mkdir -p .tools
gh release download v0.148.0 --repo open-telemetry/opentelemetry-collector-releases \
  --pattern otelcol-contrib_0.148.0_darwin_arm64.tar.gz \
  --pattern opentelemetry-collector-releases_otelcol-contrib_checksums.txt --dir .tools
gh release download v0.1.0-beta.1 --repo redact-secret/redact-secret \
  --pattern redact-secret-0.1.0-beta.1-aarch64-apple-darwin \
  --pattern SHA256SUMS --dir .tools
```

Verify the downloaded assets before extracting or executing them:

```sh
uv run python - <<'PY'
from pathlib import Path
import hashlib
import tarfile
root = Path('.tools')
for name, manifest in [
    ('otelcol-contrib_0.148.0_darwin_arm64.tar.gz',
     'opentelemetry-collector-releases_otelcol-contrib_checksums.txt'),
    ('redact-secret-0.1.0-beta.1-aarch64-apple-darwin', 'SHA256SUMS'),
]:
    expected = next(line.split()[0] for line in (root / manifest).read_text().splitlines()
                    if line.split()[-1].lstrip('*') == name)
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
with tarfile.open(root / 'otelcol-contrib_0.148.0_darwin_arm64.tar.gz') as archive:
    archive.extract('otelcol-contrib', root, filter='data')
(root / 'redact-secret-0.1.0-beta.1-aarch64-apple-darwin').chmod(0o755)
PY
```

Observed SHA256 values:

| Asset | SHA256 |
| --- | --- |
| Collector Darwin arm64 archive | `9fa3074e075f49b3c82cca957cfa367f097b4b56e5583105613beb8ab5a6ffa1` |
| Redact Secret Darwin arm64 binary | `05b5062e29a135a89b113c519d7291988e1f475e2883037a4dfc3de9b3255843` |

Other platforms must choose the matching assets from the same releases, verify
their checksums, and set `OTELCOL` and `REDACT_SECRET_CLI` to absolute binary
paths for tests. Those platforms were not run in this change.

## Run the real export path

In one terminal, create a new archive and start the Collector:

```sh
mkdir -p archive
export CORD_ARCHIVE_DIR="$PWD/archive"
export TZ=UTC
.tools/otelcol-contrib validate --config collector/config.yaml
.tools/otelcol-contrib --config collector/config.yaml
```

`TZ=UTC` is required: the pinned OTTL `Now()` uses the process timezone. The
receiver binds to `127.0.0.1:4318`; `CORD_OTLP_ENDPOINT` can override the bind
address for local testing. Do not expose this unauthenticated receiver remotely.
The Collector's own `health_check` extension binds to `127.0.0.1:13133`
(`CORD_HEALTH_ENDPOINT` overrides it); [delivery health](delivery-health.md)
polls it to distinguish a Collector outage from silence.

In another terminal:

```sh
uv run python examples/archive_fixture.py
uv run archive-check archive \
  --cli .tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin
```

Allow the 100 ms file buffer to flush before scanning an active archive, or stop
the Collector with Ctrl-C first for a complete closed-file check. A real capture
of the five spans is checked in as
[archive.sample.otlp.jsonl](../examples/archive.sample.otlp.jsonl).

## Contracts and ownership

`execution.run(..., graph_id=...)` creates a fresh trace and UUID Run ID, requires
an explicit Graph identity and a non-empty opaque Subject and type, and stamps the Run root
with `cord.semconv.version=0.3.0` (ADR-0013; #6 introduced 0.2.0). An optional `run_id` accepts the host's
identity for independent Aegra executions (#9). Its Step and Attempt helpers inherit
Graph/Run/Subject identity. The earlier sample and #5 evidence below retain
their original `0.1.0` format; see the [query migration contract](archive-query.md)
for the separate fresh capture and known result. Steps carry `cord.node.name` and
a Step Outcome; Attempts additionally carry a positive `cord.step.attempt`
and an Attempt Outcome. Tier is an optional graph-supplied annotation in 0.3.0.
The query continues to accept 0.2.0 archives with Tier present. Explicit parents keep Attempts siblings. Optional Signal
and cascade attributes are absent from this manually invoked fixture. Full
prompts, diffs, and model outputs are not recorded by these helpers.

Install `SimpleSpanProcessor(RedactingOTLPExporter(...))` as the provider's only
export path. `telemetry.py` uses the public OTel `encode_spans()` and upstream
`scan_and_redact()` APIs, without changing SDK internals or upstream policy.
It processes strings throughout a copied OTLP span envelope: span/resource/scope
attributes and keys, arrays/maps, names, schema URLs, events, links, status,
trace state, and UTF-8 AnyValue bytes. Protocol trace/span IDs remain opaque.
Invalid UTF-8 bytes fail closed. Arbitrary encodings hidden inside ordinary
strings are not decoded; upstream detector coverage is not a complete DLP guarantee.

Existing `cord.redacted` attributes are discarded. Only successful processing
adds the boolean `true` stamp. Any `block` finding or processing error suppresses
the entire affected span, even when upstream returns sanitized text. A block in
shared resource/scope data suppresses every affected span. Other spans can remain,
so suppression may leave an orphan child; parentage is never fabricated.
Diagnostics contain fixed reasons, never exception text or payload values.

The exporter has a 10-second HTTP timeout, ignores environment proxies, follows
no redirects, and has no persistent queue/retry. Export failure is reported to
the SDK and through a fixed diagnostic; this slice does not promise durable
delivery during Collector downtime. Do not install an additional exporter or
direct Langfuse callback, which would bypass in-process processing.

[Collector configuration](../collector/config.yaml) drops every span whose stamp
is not boolean true, before partitioning or file export. OTTL errors propagate
instead of bypassing the gate. No debug exporter or payload logging is configured.
The gate checks cooperating local producers, not malicious forgery of the stamp.

## Append, partitioning, compression

The Collector sets `cord.archive.date` to its UTC receipt date, overwriting any
producer value. This is the only archive-routing metadata it adds. Span IDs,
timestamps, parentage, and the OTLP envelope remain in their native format.
`file/archive` groups by this attribute and writes
`archive-YYYY-MM-DD.otlp.jsonl` with `append: true`. Each line is an OTLP export
envelope, not necessarily one span. A retransmission can duplicate spans; queries
must deduplicate by `span_id` as ADR-0005 requires.

Restarting on the same date appends to the same file. The next UTC receipt date
uses another file, even for spans with old execution timestamps. No automatic
size rotation, deletion, or retention limit is configured.

The pinned file exporter cannot combine append with built-in rotation or
compression; its built-in compression is zstd, not gzip. To compress, **stop the
Collector first**, select only past UTC date files, and run `gzip` on those files
(for example, `gzip archive/archive-2026-09-10.otlp.jsonl`). Restart afterwards.
Do not compress today's file or replay a closed date by changing the Collector
clock. This prevents appending to a compressed partition. `archive-check` reads
both `.otlp.jsonl` and `.otlp.jsonl.gz`. It returns 0 for no findings, 1 for
findings (including upstream warn), and 2 for failures; failure takes precedence.
Missing/empty input is a failure, not evidence of a clean archive.

The checker scans raw JSON and decoded nested strings/AnyValue bytes through the
upstream CLI. Escaped newlines and base64 must not conceal a private-key fixture.
It emits counts and fixed status only. CLI input limits still apply: oversized
records fail the check rather than being silently skipped.

## Verification evidence

Commands run locally on 2026-09-11 (America/New_York; sample timestamps use UTC):

| Command / check | Result |
| --- | --- |
| `uv sync --locked` | Python 3.12.14; pinned public wheel installed |
| `.tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin --version` | `redact-secret 0.1.0-beta.1` |
| Collector `validate --config collector/config.yaml` with archive env set | Exit 0 |
| `uv run pytest -q` | 22 passed, including the actual Collector process and HTTP/file path |
| `uv run archive-check examples/archive.sample.otlp.jsonl --cli .tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin` | 5 records; findings=False; failure=False; exit 0 |

The measurement is gate/redaction correctness and archive structure, not
throughput or long-term uptime. Inputs are three tiny synthetic text fixtures
and one Run with one Step and three Attempts. The credential recipe follows
upstream `bindings/python/tests/test_api.py`; the PEM body is base64 of
`SYNTHETIC_ONLY_NOT_A_REAL_KEY`, not cryptographic key material.

Tests verify required attributes and exact raw OTLP parent IDs; success-only
stamping; blocks in multiple envelope locations; value-free failure diagnostics;
complete archived fixture scanning; and distinct fresh archives for missing,
false, string/integer stamps, removed redactor, processing failure, and block.
Append is checked across actual Collector restarts. Date separation is checked
with a second temporary config changing only the clock expression to a fixed
next date; the test does not claim a 24-hour live rollover. Closed-file gzip is
round-tripped and rescanned. The upstream full qualification matrix was not run.

## Verification after merging #4

With both dependency sets combined and pytest pinned to `9.1.1`,
`uv sync --locked` succeeded and `uv run --locked pytest -q` passed all 49 tests
(27 graph tests and 22 archive tests). The graph module still produced `hello`
with exactly one escalation. Graph and archive fixtures remain separate.

## Public references

- [Redact Secret release](https://github.com/redact-secret/redact-secret/releases/tag/v0.1.0-beta.1)
- [Pinned Python API](https://github.com/redact-secret/redact-secret/blob/v0.1.0-beta.1/bindings/python/README.md)
- [Pinned CLI contract](https://github.com/redact-secret/redact-secret/blob/v0.1.0-beta.1/crates/secret-scan-cli/README.md)
- [Collector file exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/exporter/fileexporter/README.md)
- [Collector filter processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/processor/filterprocessor/README.md)
- [ADR-0006 correction](decisions/0006-masking-enforcement.md#정정--공개-릴리스와-실제-경계-2026-09-11-5)
