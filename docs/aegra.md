# Aegra integration and Slice 0 exit evidence (#9)

The existing minimal graph executes through Aegra 0.10.4 and one Postgres
16.10 service, then calls the DB-free LiteLLM proxy from #8. Aegra owns
execution and checkpoint persistence. The graph owns validation and escalation.
The Collector's append-only OTLP archive remains the execution record of origin.

[ADR-0013](decisions/0013-switchboard-boundary.md) makes model dependencies
private to each graph. The LiteLLM path below is one optional example. Its
real-provider smoke has not run and is no longer a Cordboard completion gate.
[Current boundary evidence](switchboard-boundary.md) also runs a different graph
with no model, Tier or gateway using the same public Aegra client and archive.
This local scope correction does not modify GitHub tracker state.

## Setup and reproduction

Run from the repository root. Install the two checksum-verified binaries in
[Archive setup](archive.md#setup) and start Docker. These tests need no real
provider key. The Docker image is pinned by its multi-platform digest in
[compose.yaml](../aegra/compose.yaml) and the test fixture.

```sh
uv sync --locked --group proxy --python 3.12
uv sync --locked --project aegra --python 3.12
docker pull postgres:16.10-alpine@sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297
uv run --locked --group proxy pytest -q tests/test_aegra.py
uv run --locked --group proxy pytest -q --basetemp .tools/slice0-evidence
```

`--basetemp` replaces that test-output directory on each run; use a new path if
preserving an earlier capture. Each gate-negative test gets a fresh archive.
Tests start actual Aegra, LiteLLM and Collector processes and a disposable
Postgres container with no shared user volume. They stop their own processes
and remove their database container afterwards. The controlled provider is a
tiny HTTP fixture, not a mocked Aegra executor or persistence layer.

The measurement is identity, state isolation, span parentage, declared outcomes,
and redaction. The main input is ten short completions across five Runs:

| Run | Model completions | Draft outcome | Attempts | Escalations |
| --- | --- | --- | --- | --- |
| 1 | wrong, wrong, wrong | failed | 3 | 1 |
| 2 | hello | passed | 1 | 0 |
| 3 | wrong, wrong, hello | passed | 3 | 1 |
| 4 | hello. | repaired | 1 | 0 |
| 5 | wrong, hello | passed | 2 | 0 |

The shared Subject is the opaque string `opaque subject label`. Five distinct
Run IDs, Thread IDs and traces prove that grouping does not share checkpoint
state. The first failed checkpoint is read again after all later Runs and is
unchanged. The integrated capture has exactly **40 spans**: five Runs, fifteen
Steps, ten Attempts and ten proxy model spans. Every non-root span has its
expected direct parent, matching trace ID and contained timestamps. The query
returns `[{"graph":"minimal-graph","node":"draft","count":2}]`; reading the same
OTLP twice still returns two. The fixed 56-day boundary and malformed-parent
tests from #6 run in the complete suite; no eight-week live run is needed.

After the complete suite, scan the actual integrated archive directly:

```sh
uv run --locked archive-check .tools/slice0-evidence/test_aegra_integrated_exit0/archive \
  --cli .tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin
uv run --locked archive-escalations .tools/slice0-evidence/test_aegra_integrated_exit0/archive \
  --reference-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Use the matching CLI path on other platforms. The query window is relative to
the supplied reference time; the live fixture is not expected to count after
it ages out of that window.

## Public execution and identity contracts

[aegra/aegra.json](../aegra/aegra.json) exports the existing graph through
[aegra_graph.py](../examples/aegra_graph.py), a public per-request async
context-manager factory. It receives server-pinned `configurable.run_id` and
`configurable.thread_id`. It opens the Cordboard Run span for the factory's
execution lifetime and closes it when Aegra finishes consuming the graph.
Schema and state inspection do not emit execution spans.

[aegra_client.py](../src/cord_runtime/aegra_client.py) uses only HTTP contracts:
`POST /threads`, `POST /threads/{thread_id}/runs`, polling
`GET /threads/{thread_id}/runs/{run_id}`, and reading
`GET /threads/{thread_id}/state`. Each invocation creates a fresh Thread. It
passes the Subject in `config.configurable.cord_subject`; the example also
receives it as graph input and rejects a mismatch. The client knows no graph
state fields. It does not automatically retry Run creation. A wait timeout
does not cancel the server Run.

For these independent executions, `cord.run.id` equals Aegra's returned
`run_id`. `execution.run` accepts that external identity or generates a fresh
UUID for standalone execution. Subject remains required but is not parsed or
normalized. This fixes the older runtime's unnecessary colon requirement and
preserved semantic convention 0.2.0 at implementation time. ADR-0013 now emits
0.3.0 with optional Tier; the query continues to accept those 0.2.0 archives.

`execute()` accepts an optional `request_context`, transported unread as the
API's top-level `context` (distinct from `config.configurable`); a graph-owned
bridge interprets it (#12). Its return always carries `status`: `"success"`
(with `values`), or `"waiting"` for a server-reported `"interrupted"` Run or
one still `"pending"`/`"running"` when the wait budget elapses — either way the
identities are returned, not lost to an exception, and a pause is never
reported as success or retried. A genuine failure (`"error"`/`"timeout"`
status, or an unreachable/non-2xx response) still raises `RuntimeError` with a
payload-free message. See [the `cord` CLI](cord-cli.md) for the command surface
built on this contract.

**Resume is a different contract.** Aegra 0.10.4 creates a new API Run ID for
every Run submission, including `command.resume` on an existing Thread. It
does not maintain ADR-0003's proposed 1:1 API-Run/Thread mapping across resume.
The [dated ADR correction](decisions/0003-subject-not-thread.md#정정--aegra의-api-run과-논리-run-2026-09-11-9)
separates an API invocation from a logical Run. This client exposes independent
execution only. Resume, interrupt continuation, worker recovery and joining
multiple API invocations into one logical trace are not implemented or claimed.

The host persists the graph's Steps in Postgres. The synchronous draft Attempt
subgraph explicitly uses `checkpointer=False`; Attempts are atomic inside the
draft Step and do not inherit an incompatible async checkpointer. An Aegra
checkpoint can restart a draft node; exactly-once model calls and partial
Attempt recovery are not claimed. Per-request instrumentation stays in a graph
closure, outside serialized API config and checkpoint metadata.

## Process and telemetry boundaries

Two locked Python environments are necessary. LiteLLM 1.87.0 pins Uvicorn
0.33.0; Aegra 0.10.4 requires Uvicorn >=0.36.0. Root `uv sync` installs only the
default `dev` group; this optional proxy example explicitly adds `--group proxy`. The separate
[Aegra project](../aegra/pyproject.toml) depends on the shared runtime wheel and
has its own lockfile, without the proxy group. No dependency constraint is
overridden. The resolved Aegra server uses Uvicorn 0.52.4 and
langgraph-checkpoint-postgres 3.1.2; both environments use LangGraph 1.2.11.

[aegra_server.py](../examples/aegra_server.py) launches a clean child interpreter
with an allowlist of Postgres and process settings. It disables Aegra's optional
OpenInference/OTel targets, console export, inherited LangSmith tracing and
Redis. Its graph factory emits through the existing in-process redacting
exporter only. Upstream console output is discarded before it can persist
payloads; errors from the launcher and API client use fixed diagnostics.
The proxy launcher independently enforces its existing settings contract.

There is one root in the Aegra graph process, three graph Step spans, explicit
sibling Attempt spans, and a W3C `traceparent` injected into each model HTTP
request. The proxy extracts that context and creates the matching Attempt's
direct child. There are no additional HTTP, database, LangChain or Aegra
auto-instrumentation spans. All emitted application telemetry from both
processes is checked; Postgres contributes checkpoint storage, not another
trace producer.

The main fixture injects a synthetic credential into the proxy model mapping.
Additional integrated tests inject it into Subject and verify graph-side
redaction, and inject a synthetic private-key identifier into the proxy to
verify whole-span suppression. That deliberate block yields five spans and no
model span, while execution still succeeds. It is kept separate from the
complete positive trace. Concurrent executions verify that Subject and parent
identities do not cross between Runs.

Fresh-archive wire tests submit missing/false stamps under both deployment
service names and require zero archived spans/bytes. These measure the shared
Collector gate, not a malicious producer forging a stamp. The full suite also
covers removed exporters, processing errors, invalid stamp types, envelope
redaction and block suppression from #5. Every integrated archive is checked
with the pinned CLI over raw and decoded OTLP; synthetic values and model
payloads must be absent. This guarantee concerns the telemetry archive.
Graph input/output and Subject are intentionally stored as checkpoint state in
Postgres; Postgres is not a second redacted telemetry archive.

## Manual execution

Start a fresh Collector archive and the validated proxy using
[the #8 instructions](model-proxy.md#real-provider-setup). Supply
`POSTGRES_PASSWORD` through the environment, then:

```sh
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_USER=postgres POSTGRES_DB=cordboard
docker compose -f aegra/compose.yaml up -d --wait
uv run --locked --project aegra python -m examples.aegra_server
```

The server binds to `127.0.0.1:2026`; `GET /health` must return 200. In another
terminal run `uv run --locked python -m examples.aegra_run`. It prints only
identities, outcome and Attempt count. Use `--subject` to supply a grouping
label. The server accepts `--port`, `--proxy` and `--collector` overrides.
Stop the server with Ctrl-C. `docker compose -f aegra/compose.yaml down` stops
the manual database while preserving its checkpoint volume.

The optional #8 `examples.provider_smoke` command checks the graph operator's
chosen provider configuration against a fresh archive. No operator mapping or
key was available during that implementation. It is not a prerequisite for
running other graphs or completing the platform boundary checks.

## Historical #9 evidence status

Verified on macOS arm64, CPython 3.12.14, Aegra 0.10.4, Postgres 16.10,
LiteLLM 1.87.0, Collector 0.148.0 and redact-secret 0.1.0b1.

| Acceptance / exit criterion | Evidence | Status |
| --- | --- | --- |
| Supplied Subject and complete hierarchy | 40 OTLP spans, exact API Run IDs, all direct parents and timestamps checked | Passed |
| Fresh Run/Thread state | Five distinct identities, expected attempt counts, old failed checkpoint unchanged | Passed |
| No unexplained positive orphans | Entire positive capture and concurrent traces traversed, including proxy spans | Passed |
| Known archive counts and duplicate delivery | Count 2, still 2 with duplicate OTLP input; #6 window/error tests | Passed |
| Secret absence and whole-span block | Graph and proxy injection; five spans after model block; CLI scans | Passed |
| Missing/false/unprocessed spans rejected | Fresh wire archives for both producers; #5 removal/error cases | Passed |
| Separate actual-provider compatibility | #8 harness exists; operator mappings/credentials absent | **Not run — optional example under ADR-0013** |

Historical results on 2026-09-11 (America/New_York; archive timestamps use UTC).
The command table uses current paths and the now-explicit optional proxy group:

| Command | Observed result |
| --- | --- |
| `uv sync --locked --group proxy --python 3.12` | Root resolution: 135 packages; 131 installed |
| `uv sync --locked --project aegra --python 3.12` | Aegra resolution: 89 packages; 86 installed |
| `uv run --locked --group proxy pytest -q tests/test_aegra.py` | **9 passed in 23.14s** |
| `uv run --locked --group proxy pytest -q --basetemp .tools/slice0-evidence` | **108 passed in 51.65s** |
| Integrated archive `archive-check` command above | `records=40 findings=False failure=False`, exit 0 |
| Integrated `archive-escalations`, reference `2026-09-12T01:31:48.357625+00:00` | `minimal-graph / draft / 2`, exit 0 |
| Separate actual-provider smoke | **Not run**; the then-named `CORD_PROVIDER_BASE`, `CORD_PROVIDER_KEY`, `CORD_FAST_MODEL`, `CORD_DEEP_MODEL` variables were absent |

These historical commands cover deterministic exit criteria. ADR-0013 supersedes
the former provider-credential completion gate. See the current boundary evidence
for the revised acceptance and new commands; the provider check has not passed.

## Upstream references

- [Public factory configuration](https://docs.aegra.dev/reference/configuration)
- [Aegra API 0.10.4 release](https://pypi.org/project/aegra-api/0.10.4/)
- [Pinned run creation](https://github.com/aegra/aegra/blob/v0.10.4/libs/aegra-api/src/aegra_api/services/run_preparation.py)
- [Pinned server configuration and factory lifetime](https://github.com/aegra/aegra/blob/v0.10.4/libs/aegra-api/src/aegra_api/services/langgraph_service.py)
