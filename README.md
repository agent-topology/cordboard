# cordboard

**The switchboard operator for your agent graphs.**

Cordboard is a planned local tool for connecting independently developed agent
graphs and keeping a common record of their execution. Like the operators at a
manual cord board, it connects and records. The caller chooses the graph; each
graph owns its business logic, models, credentials and tools. Cordboard does not
need to know which models a graph uses, or whether it uses a model at all.
The command is `cord`; [`cord add`/`cord list`/`cord run`](docs/cord-cli.md)
connect and invoke an existing Deployment. The rest of its lifecycle is planned.

## Status

This repository contains a deterministic `fetch → draft → verify` LangGraph
example and a separate archive fixture with Run/Step/Attempt instrumentation,
in-process redaction, a Collector gate, and raw OTLP JSONL persistence.
An optional [LiteLLM example](docs/model-proxy.md) connects one graph and archive,
with deterministic proxy tests requiring no provider credentials. Real-provider
compatibility remains an unrun example check, not a platform prerequisite.
[`cord add`/`cord list`/`cord run`](docs/cord-cli.md) connect and invoke an
existing Deployment without a manifest or model settings. The remaining
platform, including `cord up`, is still planned.

The [internal dependency review (2026-09-15)](docs/internal-dependencies.md)
documents the implemented workflow core, campaign/Git graphs and Omiologic Aegra
deployment. Their API, topology and telemetry integration with Cordboard remains
separate work; the review identifies the concrete remaining gaps.

```sh
uv sync --locked --python 3.12
uv run --locked python -m examples.minimal_graph
uv run --locked pytest -q -m "not collector"
```

See [the minimal graph contract](docs/minimal-graph.md) for graph fixtures and
[Archive setup and verification](docs/archive.md) for the Collector binaries,
archive commands, and full test suite. The [escalation query](docs/archive-query.md)
provides deterministic eight-week Graph/Node counts from the archive.
[Langfuse setup and comparison](docs/langfuse.md) verify those counts through the
public read API of a pinned self-hosted instance, including model-free execution.

The project is intended for a single operator on a local laptop. Its design
aims to make the platform usable without a hosted platform account or paid
platform tier. Model-provider access is a separate integration concern.

## What it is intended to do

- Discover graphs through generated manifests and show their topology before
  any execution has occurred.
- Route external signals to configured graphs and coordinate human approvals.
- Record Runs, Steps, Attempts, and graph-declared outcomes in a common vocabulary.
- Keep an append-only OTLP archive as the record of origin, with Langfuse as an
  additional interface for exploration.
- Manage graph scaffolding, registration, dependencies, and local startup.

The central test is whether an unrelated second graph can be registered, run,
and inspected without changing platform code. Another is whether one query can
answer: “Across all graphs in the last eight weeks, which ten Nodes escalated
model Tier most often?” The archive query now verifies the latter with fixed-time
fixtures and a real Collector capture. Registration remains planned.

Cordboard builds around execution APIs, OpenTelemetry, and uv, with Aegra and
LangGraph as the current execution integration. LiteLLM is an optional graph
dependency. Cordboard
consumes topology from the independent `agent-topology` project and delegates
secret detection to `redact-secret`. DeepAgents is an optional graph-template
choice. Production deployment, multitenancy, billing, a marketplace, and a
replacement orchestration framework are outside the design's scope.

## Explore the design

| Document | Purpose |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Intended components, ownership, contracts, and unresolved design conflicts |
| [Conventions](CONVENTIONS.md) | How to change the project and keep its contracts and documentation consistent |
| [Decision index](docs/decisions/DECISIONS.md) | Accepted ADRs, rationale, and implementation prerequisites |
| [Visual design](docs/artifacts/cordboard.html) | Interactive overview of the original design and proposed slices |
| [Cross-system glossary](docs/artifacts/cordboard-glossary.html) | Interactive vocabulary reference |
| [Upstream requirements](docs/decisions/cordboard-upstream-requirements.md) | Findings and open questions for topology and redaction dependencies |
| [Internal dependency review](docs/internal-dependencies.md) | Six repository snapshots, implemented contracts, release distinctions and remaining integration gaps |

Open either HTML file directly in a browser. They need no application server.
Some examples and terminology in these artifacts predate later ADR corrections;
the architecture document identifies the material differences.

## First implementation milestone

Slice 0 is a small end-to-end experiment: graphs, Run/Step/Attempt spans, an
OTel Collector, an archive, and a query with a known answer. Aegra exercises
the process boundary; the LiteLLM example exercises an optional model path.
Slice 0.5 adds the verified Langfuse export and comparison. Slice 1 begins with
[connecting and running an existing Deployment through `cord`](docs/cord-cli.md)
(#12); the catalog, custom viewer, and the rest of the CLI lifecycle remain planned.

Issue #4 establishes the graph contract and corrects the escalation examples.
Issue #5 verifies redaction release availability and the archive gate with a
separate fixture. Issue #8 connects the two through LiteLLM with graph-owned
escalation and in-process proxy redaction. [Issue #9's Aegra integration](docs/aegra.md)
adds Postgres execution and verifies all emitted span parents, fresh state,
archive counts and redaction. [ADR-0013](docs/decisions/0013-switchboard-boundary.md)
removes model configuration from the platform contract. A separate graph executes
without models or a proxy through the same client and archive.
[Boundary verification and acceptance changes](docs/switchboard-boundary.md)
record the current scope; real-provider compatibility is still unverified.
The existing slice descriptions are plans, not evidence that work has shipped.
