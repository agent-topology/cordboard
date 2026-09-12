# Escalation query (#6)

`archive-escalations` answers the eight-week question from raw OTLP JSONL with
explicit Graph, Node, and count output. It uses the archive integration from #5;
it needs no database, provider service, or running Collector to read files.

```sh
uv sync --locked --python 3.12
uv run --locked archive-escalations tests/fixtures/escalations/window.otlp.jsonl \
  --reference-time 2026-09-12T00:00:00Z --top 2
```

Expected output (also [checked in](../tests/fixtures/escalations/top2.expected.json)):

```json
[
  {"graph": "alpha", "node": "draft", "count": 2},
  {"graph": "alpha", "node": "verify", "count": 1}
]
```

## Input and counting contract

- Inputs are one or more files or directories. Directories expand immediate
  `*.otlp.jsonl` and `*.otlp.jsonl.gz` files; gzip is supported. Each line is a
  native `resourceSpans → scopeSpans → spans` OTLP export envelope, with hex
  `traceId`/`spanId`/`parentSpanId` and typed OTLP attributes. One line can contain
  many spans. Receipt-date resource metadata does not determine execution time.
- Supply complete Run trees, including parents in adjacent date partitions.
  The query joins all files before counting, so arrival order does not matter.
  Missing or empty files/directories, invalid input, conflicting duplicate IDs,
  unsupported semantic versions, and broken hierarchy fail with exit 2 and no
  totals. Diagnostics identify the input file ordinal and line, without payload
  values. An archive with valid spans but no qualifying Attempts returns `[]`.
- `cord.graph.id` is an explicit, stable, caller-supplied identity from the graph's
  execution contract. `run(..., graph_id=...)` requires it and propagates it to
  Steps and Attempts. It is independent of Node name, repository, deployment,
  provider, and `service.name`. This does not settle upstream manifest name
  ownership. New Runs use `0.3.0` (ADR-0013); existing `0.2.0` archives remain readable.
- The old #5 [capture](../examples/archive.sample.otlp.jsonl) remains unchanged.
  It has no Graph identity and the query rejects it with a re-emission diagnostic.
  There is no inference or automatic rewrite of historical archives. Re-emit
  with the updated #5 fixture for the identity-aware contract.
- Each Attempt must have a direct Step parent in the same trace, and each Step
  a Run root. Graph/Run/Subject identities must match parents; Node must match
  between Step and Attempt. Closed Outcome vocabularies, positive Attempt number,
  and timestamps are validated, including outside the query window. Tier is
  optional in 0.3.0, required in historical 0.2.0, and a non-empty string when
  present. Neither version requires provider model metadata. The query never
  resolves Tier labels to models or compares their ordering.
  Ordinary model spans are ignored; provider model changes are never counted.
- Deduplicate by `span_id` across all inputs before aggregation. Identical span
  retransmissions count once even if resource receipt metadata differs; conflicting
  span contents (including a different trace) fail rather than choosing a winner.
- Count only Attempt `cord.outcome=escalated` with **completion time** in
  `(reference − 56 days, reference]`. Completion is when the recorded disposition
  becomes final. Compare integer nanoseconds; the reference is explicit,
  timezone-aware ISO-8601 (microsecond precision). Start times and archive receipt
  dates do not select the window.
- Group by `(cord.graph.id, cord.node.name)`. Emit positive counts only, sorted
  by descending count, then Graph and Node in ascending Unicode code-point order.
  `--top` defaults to 10 and must be positive. Exit 0 means a completed query.

This is a small in-memory Python query, not a query engine or a streaming service.
Validation establishes consistency of the supplied archive, not proof that all
telemetry was delivered. A completely missing Run cannot be detected from files.

## Small fixed-time fixture

[window.otlp.jsonl](../tests/fixtures/escalations/window.otlp.jsonl) has six Run
roots, six Steps, and 19 Attempts, plus one retransmitted Attempt. The measurement
is query correctness; the input is synthetic timestamps, not eight weeks of
live execution. Full expected results are in
[expected.json](../tests/fixtures/escalations/expected.json).

| Case | Expected contribution |
| --- | --- |
| `alpha/draft`, two `fast/failed → fast/escalated → deep/passed` Runs | 2 |
| `alpha/verify`, same sequence | 1; Node tie ordering |
| `beta/draft`, same Node name in another Graph | 1; Graph separation |
| `retry/draft`, fast failed then fast passed, different provider strings | 0 |
| `boundary/draft`, escalation completions at lower−1 ns, lower, reference, reference+1 ns | Only reference counts: 1 |
| First alpha escalation at lower+1 ns | Included in alpha's 2 |
| One alpha escalation retransmitted in a second envelope | No additional count |

The boundary Step spans the synthetic window and uses successive capability
aliases, ending in a passed Attempt. Parent spans enclose their Attempts.
Records arrive child-first. [broken-parent.otlp.jsonl](../tests/fixtures/escalations/broken-parent.otlp.jsonl)
changes one Attempt's parent to a sibling Attempt and must fail:

```sh
uv run --locked archive-escalations tests/fixtures/escalations/broken-parent.otlp.jsonl \
  --reference-time 2026-09-12T00:00:00Z
# exit 2: invalid step parent hierarchy; no JSON totals
```

## Actual #5 archive verification

[archive.graph-id.sample.otlp.jsonl](../examples/archive.graph-id.sample.otlp.jsonl)
is a fresh capture from `examples/archive_fixture.emit()` through the pinned
in-process redactor, Collector 0.148.0, and file exporter. Its five spans were
captured after adding explicit Graph identity; the old capture is preserved.
The [reference and expected result](../examples/archive.graph-id.expected.json)
are checked in. Reproduce the known-answer read with:

```sh
uv run --locked archive-escalations examples/archive.graph-id.sample.otlp.jsonl \
  --reference-time 2026-09-12T00:21:07.642241+00:00 --top 10
# [{"graph": "archive-fixture", "node": "draft", "count": 1}]
```

For a new capture, follow [Archive setup](archive.md#run-the-real-export-path),
run `uv run --locked python examples/archive_fixture.py`, stop the Collector,
and query the resulting directory with a reference time after emission. The
Collector test automates that exact path into a fresh temporary archive and
checks both the Python query and installed CLI against the known count of 1.

Commands actually run on 2026-09-11 America/New_York (capture uses UTC):

| Command | Result |
| --- | --- |
| `uv sync --locked` | Python 3.12.14; locked dependencies installed |
| SHA256 verification of both pinned binaries against release manifests | Passed |
| `uv run --locked pytest -q tests/test_archive_query.py` | 21 passed |
| `uv run --locked pytest -q` | 75 passed, including fresh Collector query integration and legacy rejection |
| `uv run --locked archive-check examples/archive.graph-id.sample.otlp.jsonl --cli .tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin` | 5 records; findings=False; failure=False; exit 0 |
| `git diff --check`; changed Markdown local file links; positive fixture parent intervals | Passed |

No npm, Cargo, or CI checks exist in this Python repository.
