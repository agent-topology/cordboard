# Architecture

## Status and source of truth

Cordboard has a [minimal executable graph](docs/minimal-graph.md) with
in-memory execution records (#4), plus a separate archive integration (#5):
`cord-runtime`, OTel instrumentation, redaction, and Collector persistence.
[Archive setup and evidence](docs/archive.md) describe the runnable archive
fixture. The [archive escalation query](docs/archive-query.md) now validates
complete Run trees and returns deterministic Graph/Node counts (#6). The
[LiteLLM integration](docs/model-proxy.md) connects the graph to the archive
through explicit Attempts and a DB-free proxy (#8). Controlled endpoint tests
pass; real-provider compatibility remains unverified without operator credentials.
The [Aegra integration](docs/aegra.md) executes that graph through the public
API with Postgres checkpoints and verifies the complete archive path (#9).
The [switchboard boundary](docs/decisions/0013-switchboard-boundary.md) makes
LiteLLM optional and removes provider credentials from platform acceptance.
The separate real-provider example remains unverified; this is not a passed check.
The [Langfuse comparison](docs/langfuse.md) exports through the Collector gate to
a pinned self-hosted instance and verifies the same archive cohort through its
Public API (#10), including model-free execution.
The [`cord` CLI](docs/cord-cli.md) adds `cord add`/`cord list`/`cord run` over
`aegra_client.execute`, with board-level connection storage, bounded exit
statuses, and a distinct waiting status for a paused or still-running Run (#12).
The remaining platform lifecycle commands (`cord new`, `cord up`'s drift
handling, `cord sync`) are still planned.

[Accepted ADRs](docs/decisions/DECISIONS.md) record decisions and their rationale.
Explicit corrections within an ADR take precedence over its older examples.
[docs/artifacts/cordboard.html](docs/artifacts/cordboard.html) summarizes the
design as of 2026-09-12, and
[docs/artifacts/cordboard-glossary.html](docs/artifacts/cordboard-glossary.html)
maps each shared word to its meaning in Cordboard documents (ADR-0002). Where
either disagrees with an ADR, the ADR wins. The
[upstream requirements](docs/decisions/cordboard-upstream-requirements.md) record
integration findings; conflicts with accepted decisions are identified below
rather than silently resolved. Dependency release statements in those documents
are dated observations, not a verified current compatibility matrix.

## Boundary and ownership

The platform knows a graph through its manifest, execution API, and spans. It
does not import graph business logic or understand graph-specific state fields,
prompts, tools, validation rules, model choices, or provider credentials. The graph's own process may import its code
to derive topology. This contract boundary is independent of process isolation.
The caller or an explicitly configured Rule selects the Graph/Assistant. Cordboard
connects that target and carries its API payload without interpreting the content
to select models or substitute another graph. A graph may use zero, one, or many
models without changing Cordboard configuration.

Two failure modes define the scope: a second monolith that must change for each
new graph, and a layer that adds nothing over using the underlying tools directly.

| Owner | Responsibility |
| --- | --- |
| Cordboard | Catalog, common execution vocabulary, topology/trace viewer, declarative Signal routing, graph lifecycle, approval inbox and expiry, archive integration |
| Graph | Business logic, state, deterministic validation, model selection, credentials, SDK/gateway configuration, retry and escalation decisions, interrupt placement, side-effect behavior |
| Aegra / LangGraph | Run execution, checkpointing, resume, cancellation, streaming, runtime recovery |
| Optional graph-owned gateway (e.g. LiteLLM) | Resolve the graph operator's model configuration; not a Cordboard prerequisite |
| OTel Collector | Enforce the redaction stamp and export accepted telemetry |
| Langfuse | Explore recorded execution through its Public API and UI |
| uv | Resolve and synchronize graph dependencies |
| [agent-topology](https://github.com/agent-topology/agent-topology) | Define and derive the framework-neutral topology format |
| [redact-secret](https://github.com/redact-secret/redact-secret) | Detect secrets and apply its public redaction policy API |

Cordboard does not own a new orchestration framework, dependency resolver,
telemetry query engine, evaluation platform, or production deployment system.

## Intended flow

```mermaid
flowchart LR
    S[Signal] --> R[Rule router and deduplication]
    R --> G[Graph hosted by Aegra]
    A[Approval inbox and expiry] --> G
    G --> T[Generated topology and Agent Card]
    T --> C[Catalog]
    C --> V[Viewer]
    G -->|Redacted OTLP| O[OTel Collector gate]
    O --> F[Append-only OTLP archive]
    O --> LF[Langfuse]
    LF -->|Public API| V
    G -->|Live SSE, later slice| V
```

The Graph is opaque here: model providers, tools and optional gateways are its
internal dependencies. Any additional telemetry producer follows the same redaction
contract. This shows the target system, not the minimum Slice 0 stack. The archive branch
comes first; Langfuse and the UI are introduced later.

## Identity and execution records

The definitions come from [ADR-0002](docs/decisions/0002-vocabulary.md),
[ADR-0003](docs/decisions/0003-subject-not-thread.md), and
[ADR-0008](docs/decisions/0008-outcome-split.md).

| Term | Meaning |
| --- | --- |
| Graph | Executable orchestration unit that accepts Runs and emits spans, and may publish a self-description; not a repository identity |
| Deployment | Addressable process hosting one or more Graphs |
| Assistant | Graph plus fixed configuration |
| Subject | Required opaque URI string identifying the external target of a Run |
| Run | One execution; one trace |
| Node | Static position in a Graph's topology |
| Step | One execution of a Node within a Run |
| Attempt | One try within a Step, recorded as a child span |
| Tier | Optional opaque graph-supplied annotation; no platform model mapping or ordering |
| Verdict | Graph-owned deterministic validation result, accompanied by violated rules |
| Outcome | Disposition of a Step or Attempt, using separate closed vocabularies |

For non-conversational graphs, a new Run gets a new Thread. Resume is intended
to continue the same logical Run and Thread; rerun starts fresh. Aegra 0.10.4
creates a new API Run ID even for resume. The Slice 0 client supports independent
executions only, mapping their API IDs to `cord.run.id`; logical continuation
across multiple API invocations is deferred (ADR-0003 correction).
Subject groups executions without
sharing checkpoint state and maps to Langfuse `session_id`. A conversational
graph may manage Thread reuse internally; the platform still groups by Subject.

```text
Run (trace)
└── Step (Node execution)
    ├── Attempt 1
    └── Attempt 2
        └── Optional graph-internal operation span
```

Step Outcomes are `passed`, `repaired`, `failed`, `awaiting_approval`, and
`halted`. Attempt Outcomes are `passed`, `failed`, and `escalated`.
`escalated` belongs only to Attempts and is declared by the graph when it chooses
a higher Tier; it is not inferred from changing provider model strings. The corrected
`fast → fast → deep` fixture records `failed → escalated → passed`: exactly one
escalation. A same-tier retry can succeed with no escalation. A deterministic
repair yields a `repaired` Step while retaining the original failed Attempt
and its violated rules.

## Generated discovery contracts

[ADR-0009](docs/decisions/0009-well-known-documents.md),
[ADR-0011](docs/decisions/0011-manifest-derivation.md), and
[ADR-0012](docs/decisions/0012-topology-spec-split.md) separate two documents a
Deployment may publish. Neither is required to connect or record a Graph
([ADR-0015](docs/decisions/0015-never-block-connection.md)): connecting needs a
reachable Deployment and the Graph the caller selects, and recording needs spans.
The topology manifest is optional input for the viewer.

- `/.well-known/agent-card.json`: the external A2A contract, with no Cordboard fields.
- `/.well-known/agent-topology.manifest.json`: internal structure in the
  independent topology format, exactly as the graph's producer emitted it.

The topology document always has a `graphs` array, including for one Graph.
This array convention does not redefine the A2A Agent Card format.

Cordboard does not describe graphs
([ADR-0014](docs/decisions/0014-no-graph-descriptors.md)). The graph process
calls `agent-topology` to derive structure from its compiled graph, and the graph
supplies its own display name (`compile(name=...)`). The document's
`graphs[].id` is an address inside that document, not an identity; a single-graph
document can keep the default `main`, and changing it changes `structureHash`.
Cordboard consumes the resulting JSON unchanged: it adds
no extension, rewrites no identity, and does not implement topology
introspection. Settings Cordboard needs in later slices describe connections —
deployments, triggers, Subject extraction, concurrency, approval expiry — never a
single graph. The upstream schema is authoritative for the precise wire format;
older `derived` wording in the ADRs is not a literal key.

`cord up` is intended to compare freshly derived structure with the published
structure hash and warn on drift without stopping startup. `cord sync` is the
explicit refresh path. Source-file hashes and Mermaid text are not the comparison
contract. JSON is authoritative; Mermaid is a rendering aid.

Subgraphs default to opaque nodes (`depth=0`). Per-graph `completeness.gaps`
and general `producerLimitations` must both be visible; a producer limitation
does not itself make every document incomplete. These limitations are warnings.

## Catalog validation

The catalog never refuses to connect a Graph
([ADR-0015](docs/decisions/0015-never-block-connection.md), replacing the rejection
rules of [ADR-0007](docs/decisions/0007-catalog-rejection.md)). What the manifest
can reveal is shown, not enforced:

| Manifest state | Catalog and viewer behavior |
| --- | --- |
| Absent | Connected and recorded normally; the viewer shows recorded execution without a topology |
| Present and valid | Topology drawn; recorded execution correlated against it |
| Present but schema-invalid | Document ignored with a warning; connection and recording unaffected |
| Drifted (R2) | Warning; the topology is shown as stale and recorded execution is not correlated against it |

A drifted picture is the harm ADR-0011 worried about, so the mitigation lives in
what the viewer displays rather than in a log line: a stale topology must never
look current.

R3 is a warning: an interrupt in a direct fan-out branch, judged from core
edge-kind and interrupt fields. It covers static interrupts only; dynamic
`interrupt()` calls are not visible in the topology and have no declaration path
(ADR-0014). Whether a fan-out runs all branches comes from `agent-topology`'s
experimental branch interpretation; when that is `unknown`, the warning says the
condition cannot be confirmed. Warnings need no override.

Missing Agent Cards, derivation gaps, producer limitations, and unreachable
Deployments remain warnings or status information. A new rejection rule would
have to describe the connection rather than how a Graph is built, and satisfy
ADR-0007's three conditions: document-only judgment, effectively no false
positives, and silent or irreversible harm if allowed.

## Model and telemetry paths

[ADR-0013](docs/decisions/0013-switchboard-boundary.md) supersedes the shared
model-gateway requirement in ADR-0004. Graphs own model selection and all related
configuration. Cordboard neither requires a model list nor inspects gateway
routing rules. The optional LiteLLM example lives in `examples/model_proxy`;
its `fast`/`deep` policy is not a platform contract.

Run semconv `0.3.0` allows Attempts without `cord.tier` or any `gen_ai.*` data.
The archive query also reads existing `0.2.0` records under their original Tier
requirement. It counts graph-declared escalations without knowing model identity,
Tier ordering, or the number of models the graph uses.

[ADR-0006](docs/decisions/0006-masking-enforcement.md) places secret processing
inside each telemetry-producing process, before telemetry leaves it. Successful
processing stamps `cord.redacted=true`. The Collector drops unstamped spans;
it is a gate, not another secret detector. Direct Langfuse callbacks would bypass
that gate and are prohibited. Full prompts, diffs, and model outputs are not
span attributes.

The redactor is the public PyO3 binding `redact-secret==0.1.0b1`, pinned in
`cord-runtime`, using the upstream default policy. The exporter encodes with the
public OTel encoder, scans the complete span envelope, and stamps only success.
A `block` finding suppresses the whole span even when sanitized text is returned;
RS-1/RS-2 record the verified integration.
Custom detectors and `redaction.extra_patterns` were abandoned (RS-4).
The implemented `archive-check` entrypoint wraps the pinned upstream CLI for
raw and decoded archive verification; `cord scan-archive` remains planned.

[ADR-0005](docs/decisions/0005-record-of-origin.md) makes date-partitioned,
append-only raw OTLP JSONL the record of origin. “Raw” describes the OTLP format,
not unredacted content. The archive receives only telemetry accepted by the gate.
Archive queries deduplicate retransmitted spans by `span_id`.

The escalation success criterion is answered directly from the archive. Slice
0 implements `archive-escalations`, a small Python query with fixed-time
fixtures and a verified Collector capture, without selecting a database engine.
It requires explicit `cord.graph.id` (Run semconv `0.2.0` or `0.3.0`), groups by Graph/Node,
and counts completed escalated Attempts in `(reference − 56 days, reference]`.
Old records without Graph identity fail with a re-emission diagnostic.
Langfuse is a second export destination in Slice 0.5. `langfuse-compare` reads
complete archived trace cohorts through the public v1 API of self-hosted 3.225.7,
waits for all observations with a deadline, and compares exact end-time counts.
Typed metadata and nanosecond strings are added only on the Langfuse branch;
the archive remains unchanged. Missing ingestion is distinct from an empty count.
Direct ClickHouse queries are temporary
investigation tools, not durable integration contracts.

## Isolation, routing, and approval

[ADR-0001](docs/decisions/0001-isolation-level.md) makes process isolation the
default per Graph, with explicit shared groups allowed. It is an operational
choice, not what keeps platform code independent of graph business logic.
Producer compatibility still constrains supported graph dependencies.

The design artifact proposes Signal sources including manual, file, schedule,
HTTP, and `run.finished`. Rules select an Assistant, map input, and extract a
Subject. The router deduplicates Signal IDs and applies concurrency policy on
`(Assistant, Subject)`; unmatched Signals must also be recorded.

Approval always carries `interrupt_id`. Graphs decide where to interrupt and
how to interpret the response; the platform owns the inbox and reminder/expiry
clock. Templates should separate interrupt nodes from side effects, because a
resumed node can execute again. Side-effect idempotency remains graph-owned.

Cross-graph composition is asynchronous through `run.finished`. A child Run
gets a new trace and records `cord.caused_by.run_id`; cascade depth uses
`cord.cascade.depth`. Synchronous composition stays inside a Graph as subgraphs.
Lazy startup is planned with routing in Slice 3, not assumed for the first slices.

## Open documentation issues

Review findings below are not new architecture decisions. Reconcile the relevant
sources before implementing the affected contracts; do not copy stale examples
as executable specifications.

| Finding | Sources and required follow-up |
| --- | --- |
| Wire examples span multiple topology revisions | ADR-0007/0011 retain literal `derived` references; ADR-0009/0012 use `structure`, and their Cordboard-extension examples are withdrawn by ADR-0014. ADR-0011 also mixes old source-hash/`derive.xray` text with structure-hash/`derive.depth` corrections. Validate against a pinned upstream schema before implementing. |
| R3 index summary is stale | The decision index's 2026-09-10 summary calls R3 a LangGraph-only rejection rule; ADR-0007 retracts the LangGraph-only claim on 2026-09-11, and ADR-0015 makes R3 a warning. |
| ADR-0003 predates ADR-0002's mapping rule | Both HTML artifacts were rebuilt on 2026-09-12 with definitions and mappings instead of banned words. ADR-0003 still retains a banned-word reference; read it through ADR-0002's mapping rule and `cord.cascade.depth`. |
| ADR-0010 is unwritten | The index labels the DeepAgents template decision Accepted but explicitly says no ADR file exists. The design direction is recorded, but its formal ADR remains pending. |

The [upstream requirements](docs/decisions/cordboard-upstream-requirements.md)
record each `agent-topology` finding against its measured `0.1.0b3` behavior and
where Cordboard actually uses it. After ADR-0014 and ADR-0015 none of them can
block a connection; they affect viewer and warning quality only. No
`agent-topology` package is an installed Cordboard dependency yet.
