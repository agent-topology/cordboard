# cordboard

**The switchboard operator for your agent graphs.**

Cordboard is a planned local tool for connecting independently developed agent
graphs and keeping a common record of their execution. Like the operators at a
manual cord board, it connects and records; each graph owns its business logic.
The planned command is `cord`.

## Status

This repository contains a deterministic `fetch → draft → verify` LangGraph
example and a separate archive fixture with Run/Step/Attempt instrumentation,
in-process redaction, a Collector gate, and raw OTLP JSONL persistence.
The [LiteLLM integration](docs/model-proxy.md) now connects the graph and archive,
with deterministic proxy tests requiring no provider credentials. Real-provider
compatibility needs a separate smoke check. The remaining platform, including
`cord up`, is still planned.

```sh
uv sync --locked --python 3.12
uv run --locked python -m examples.minimal_graph
uv run --locked pytest -q -m "not collector"
```

See [the minimal graph contract](docs/minimal-graph.md) for graph fixtures and
[Archive setup and verification](docs/archive.md) for the Collector binaries,
archive commands, and full test suite. The [escalation query](docs/archive-query.md)
provides deterministic eight-week Graph/Node counts from the archive.

The project is intended for a single operator on a local laptop. Its design
aims to make the platform usable without a hosted platform account or paid
platform tier. Model-provider access is a separate integration concern.

## What it is intended to do

- Discover graphs through generated manifests and show their topology before
  any execution has occurred.
- Route external signals to configured graphs and coordinate human approvals.
- Record Runs, Steps, Attempts, and model-tier escalation in a common vocabulary.
- Keep an append-only OTLP archive as the record of origin, with Langfuse as an
  additional interface for exploration.
- Manage graph scaffolding, registration, dependencies, and local startup.

The central test is whether an unrelated second graph can be registered, run,
and inspected without changing platform code. Another is whether one query can
answer: “Across all graphs in the last eight weeks, which ten Nodes escalated
model Tier most often?” The archive query now verifies the latter with fixed-time
fixtures and a real Collector capture. Registration remains planned.

Cordboard builds around Aegra, LangGraph, LiteLLM, OpenTelemetry, and uv. It
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

Open either HTML file directly in a browser. They need no application server.
Some examples and terminology in these artifacts predate later ADR corrections;
the architecture document identifies the material differences.

## First implementation milestone

Slice 0 is a small end-to-end experiment: one graph, Run/Step/Attempt spans, an
OTel Collector, an archive, and a query with a known answer. LiteLLM and Aegra
then exercise the model and process boundaries. Langfuse follows in Slice 0.5;
the catalog, viewer, and CLI lifecycle arrive with the second graph in Slice 1.

Issue #4 establishes the graph contract and corrects the escalation examples.
Issue #5 verifies redaction release availability and the archive gate with a
separate fixture. Issue #8 connects the two through LiteLLM with graph-owned
escalation and in-process proxy redaction.
The existing slice descriptions are plans, not evidence that work has shipped.
