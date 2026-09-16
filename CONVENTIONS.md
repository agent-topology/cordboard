# Conventions

## Current development surface

The runnable slices are `examples/minimal_graph.py` and the archive integration
in `src/cord_runtime`, `collector`, and `examples/archive_fixture.py`, plus the
[archive query](docs/archive-query.md), [LiteLLM integration](docs/model-proxy.md),
[Aegra integration](docs/aegra.md), and [Langfuse comparison](docs/langfuse.md). These use
Python 3.12, exact direct dependencies in `pyproject.toml`, and transitive pins
in `uv.lock`.

Run `uv sync --locked --python 3.12` and
`uv run --locked python -m examples.minimal_graph` from the repository root.
Service-free tests use `uv run --locked pytest -q -m "not collector and not aegra and not langfuse"`.
The complete suite uses `uv run --locked --group proxy pytest -q` after installing the pinned
binaries in [Archive setup](docs/archive.md), synchronizing the separate
`aegra` project and preparing Docker/Postgres per [Aegra setup](docs/aegra.md).
Root defaults include only the `dev` dependency group. Add `--group proxy` to
`uv sync` and `uv run` when running the optional LiteLLM example or its full suite. Aegra has a
separate lockfile because the two servers require incompatible Uvicorn versions.
Langfuse integration tests require the pinned local stack and `CORD_LANGFUSE_TEST=1`;
without that explicit opt-in they report skips. See its setup guide for the full
verification command and generated local project credentials.
See also the
[graph execution contract](docs/minimal-graph.md).

Keep graph-specific logic under `examples/`; do not promote it into generic
platform infrastructure. There is no npm build, Rust crate, or configured formatter.
[CI](.github/workflows/ci.yml) installs the `proxy` and `web-test` groups and
Chromium, runs service-free tests, builds a wheel, and checks installed assets.
Collector, Aegra and Langfuse integration tests remain environment-dependent.
[`cord add`/`cord list`/`cord run`](docs/cord-cli.md) are
executable (#12), as are [`cord rule add`/`cord signal
manual|file|schedule`](docs/signal-routing.md) (#16), `cord add
--launch`/`cord deployment sweep` (#17), and the platform-emitted
[`run.finished` cascade](docs/signal-routing.md#chaining-run-completions-runfinished-18)
`route_signal` chains automatically (#18). `cord sync`, `cord view`, and
[`cord serve`](docs/browser-viewer.md) are also executable; `cord new` and
`cord up` remain planned.

## Documentation and decisions

- Keep [ARCHITECTURE.md](ARCHITECTURE.md) focused on boundaries, ownership,
  contracts, and implementation status. Keep [README.md](README.md) a
  human-facing introduction and navigation guide, not an agent instruction source.
- Put agent execution guidance in [AGENTS.md](AGENTS.md). [CLAUDE.md](CLAUDE.md)
  delegates to it; avoid maintaining competing instructions.
- Keep project decisions in `docs/decisions/NNNN-short-title.md` and register
  them in [DECISIONS.md](docs/decisions/DECISIONS.md). Existing ADRs use Markdown
  metadata (`Status`, `Date`, `Deciders`, related ADRs), not `scope: workspace`
  front matter. Follow their structure: Context, Decision, Options Considered,
  Trade-off Analysis, Consequences, and Action Items, as relevant.
- Preserve decision history with explicit dated corrections. Update affected
  examples, action items, the index, and root summaries when a decision changes.
  Do not mark a planned action completed without evidence.
- Follow explicit ADR corrections over superseded prose. If sources conflict
  without a clear correction, record the conflict and resolve it before building
  the affected behavior. Existing conflicts are listed in
  [Architecture](ARCHITECTURE.md#open-documentation-issues).
- Keep the language and terminology of an existing document when editing it.
  Korean ADR prose and English technical identifiers already coexist here.
- Use relative links for repository documents. Distinguish planned output paths
  and HTTP endpoints from files that actually exist. Check links after moving or
  renaming documents.
- Preserve HTML artifacts as readable design references. Their historical
  examples do not override later ADR decisions.

## Names and persisted contracts

Follow [ADR-0002](docs/decisions/0002-vocabulary.md). Natural language is not
subject to a banned-word list. Add source qualifiers such as `otel.` or `lf.`
when context is ambiguous; preserve protocol fields such as `trace_id`,
`traceparent`, `span_id`, `session_id`, and `thread_id`.

Use precise names where they persist in data:

- `cord.graph.id`, `cord.run.id`, `cord.subject.id`, `cord.subject.type`
- `cord.node.name`, `cord.step.attempt`, `cord.tier`, `cord.outcome`
- `cord.signal.id`, `cord.cascade.depth`, `cord.caused_by.run_id`
- `cord.redacted`, `cord.semconv.version`

Borrow `gen_ai.*` names for concepts already covered by those conventions and
record `cord.semconv.version` on each Run root span. Changes to persisted keys
or closed Outcome vocabularies require an explicit decision and consideration
of previously archived data.

Node means definition; Step means execution; Attempt means a child span within
that execution. Keep Step and Attempt Outcome values separate as documented in
[Architecture](ARCHITECTURE.md#identity-and-execution-records). Keep Verdict
(validation judgment) separate from Outcome (execution disposition).

One repository per Graph is a scaffolding convention, not an identity or
registration constraint. A Deployment may host multiple Graphs.

## Implementation boundaries

- Platform logic consumes manifests, APIs, and spans. Do not import graph
  business logic or hard-code a specific graph's Node names or state fields into
  the catalog, router, or viewer. Domain-specific example graphs and fixtures
  should remain separate from generic platform behavior.
- Derive topology in the graph's own process through the public `agent-topology`
  API. Consume the resulting document as published: do not add sections, rewrite
  names or ids, or put Cordboard fields in it or in the A2A Agent Card. Treat
  published structure as generated output and use the explicit synchronization
  path to refresh it.
- Do not create per-graph Cordboard configuration. Facts about a graph come from
  the graph: `compile(name=...)`, `describe(graph_id=..., depth=...)`, and its
  Aegra deployment file. Cordboard settings, when a slice needs them, describe
  connections — deployments, triggers, Subject extraction, concurrency, expiry
  (ADR-0014).
- Use only public `agent-topology` and `redact-secret` APIs. Record missing
  upstream capabilities and reproductions in the
  [upstream requirements](docs/decisions/cordboard-upstream-requirements.md),
  rather than introducing local detector or topology-parser substitutes.
- Pin integration dependencies when code is introduced and verify their
  supported versions. Design-session version numbers are not a substitute for
  compatibility evidence.
- The caller selects the Graph/Assistant. Cordboard connects it through its API
  without interpreting graph payloads, prompts or model configuration. Models,
  provider keys, SDKs, gateway settings and retry/escalation policies belong to
  the graph.
- Record graph-declared outcomes. Tier and model metadata are optional; never
  infer escalation from model strings or Tier order. Keep optional gateway
  launchers and provider-specific tests in examples, outside `cord-runtime`.
- Preserve in-process redaction and the Collector gate. Do not add direct
  telemetry paths that bypass it, or put full prompts, diffs, and model output
  into span attributes.
- Keep mutable execution state scoped to the graph execution rather than shared
  middleware instances. Separate interrupts from side-effect nodes and make
  side effects idempotent.

## Verification

For documentation changes, check that every local reference exists, metadata
and examples agree with the cited decisions, and planned behavior is labeled.
Review HTML content stored in JavaScript as well as static markup when auditing
the artifacts. Use `git diff --check` for tracked changes; check new untracked
documents too, since Git's ordinary diff does not include them.

For implementation experiments, state what is being measured separately from
the input needed to measure it. Use the smallest fixture that can distinguish
success from failure. An eight-week query window does not require eight weeks
of live execution: use a small fixture with appropriate timestamps and known
counts to verify the window, then separately validate live integration.

Before proposing a command likely to take more than five minutes, inspect and
validate its inputs when doing so is materially cheaper. Start with focused
checks and expand only when failures or unresolved risks warrant it.

The design's important checks include graph independence, Run/Step/Attempt
parentage, archive query correctness and deduplication, rejection of unstamped
telemetry, absence of synthetic secrets in the archive, manifest drift detection,
and clean reruns versus state-preserving resumes. Add them with the slice that
introduces the behavior, not as a prerequisite infrastructure project.

## Repository workflow

Use Graft first for code discovery and impact analysis, then RTK to compress
command output as described in [AGENTS.md](AGENTS.md) and [RTK.md](RTK.md).
An empty or unindexed graph is a reason to read the relevant documents directly,
not evidence that the documents are absent.

No repository-local issue-resolution or PR-review skills are installed under
`.agents/skills/`. No project branch-prefix or commit-subject convention has
been established here. Do not inherit another project's workflow merely because
`workbench` appears as a design precedent. Use the current task's authorization
and preserve unrelated work.
