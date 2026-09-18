# agent-topology beta.5 adoption — 2026-09-18

beta.5 is a producer-only release built from source
[`dc03030`](https://github.com/agent-topology/agent-topology/commit/dc030305096444908b1ba19a3458a6d97c9cf90b).
Only `agent-topology-langgraph==0.1.0b5` and `@agent-topology/langgraph@0.1.0-beta.5`
are new. `agent-topology-spec` stays on the published `0.1.0b4`, with no
schema, canonical-model, document-format (`0.1`) or hash-algorithm (`1`) change.
Cordboard's runtime spec pin is therefore unchanged. This update moves the
dev-only producer pin from b4 to b5 for consumer regressions. No
`cord_runtime` source changed.

Release status as checked on 2026-09-18: the PyPI wheel and sdist were uploaded
2026-09-17T18:24Z, and Cordboard's lock records their digests. Upstream's
[release notes](https://github.com/agent-topology/agent-topology/blob/main/docs/releases/v0.1.0-beta.5.md)
describe the release as published. However, the GitHub prerelease was still a
draft, and the remote `v0.1.0-beta.5` tag did not exist yet. Links here
therefore point to the source commit, not the tag.

## Limits lifted for Cordboard

| Limit before beta.5 | beta.5 change | Cordboard result |
| --- | --- | --- |
| Repeated or permuted `add_edge(sources, target)` declarations produced duplicate join IDs. The independent validator rejected the document, so Cordboard could only report `invalid` and draw nothing | Both producers emit one join per `(sorted sources, target)` group and escape `\`/`+` in join-source segments (upstream #182, ADR 0015) | The document is valid, drawn and correlated. The layout shows one AND-join edge per source. Cordboard gets join edges from the public `derived_join_edges` and never parses join IDs, so escaped IDs need no change |
| A child called through a wrapper function (`child.invoke(...)` inside a node) was `identity-unavailable` at every depth. No depth could expose its structure | Python `declare_children(compiled, {node_id: child})` adds `declared-child-call` evidence. At positive depth, the child becomes its own `graphs[]` entry, referenced by `subgraphId` (upstream #179/#185, ADR 0014) | The existing explicit `graph_map` can select the child's structure. R3 warnings now cover static interrupts inside the declared child. The parent Node still matches its recorded Step |

Graphs without these shapes produce the same documents and hashes as with
beta.4. Documents from graphs that never used these shapes are not affected.

## Limits that remain

- **Recursive child rendering (Cordboard work,
  [#85](https://github.com/agent-topology/cordboard/issues/85)).** The viewer
  still shows one selected graph per mapping. Following `subgraphId` within
  the current document needs nothing more from upstream.
- **Child-call attribution (Cordboard work, closed,
  [#86](https://github.com/agent-topology/cordboard/issues/86),
  [ADR-0021](decisions/0021-nested-step-child-attribution.md)).** The archive
  contract now also allows a Step to nest under the parent Step that made a
  declared child-graph call (`cord_runtime.execution.Step.child`), under a new
  Run semconv `0.4.0`; `0.2.0`/`0.3.0` archives are read unchanged and cannot
  produce this shape. Attribution comes entirely from Cordboard's own recorded
  parent-Step parentage, combined with the parent Node's `subgraphId` in this
  same document (`web.presentation.run_topology`) -- never from upstream
  runtime identity: `declare_children` stays extraction-time metadata, and
  checking that a declaration is correct remains the entity's responsibility.
- **Real entity coverage.** campaign-agent adopted `declare_children` at all six
  wrapped call sites
  ([campaign-agent#63](https://github.com/milocosmopolitan/campaign-agent/pull/63)).
  No entity that Cordboard connects to serves those graphs yet: Omiologic
  registers no campaign graph and does not serve the well-known document
  ([real entity pilot](real-entity-pilot.md)). The real-consumer claim is
  upstream's evidence, not Cordboard's. The entity-side follow-up is
  [omiologic-aegra#60](https://github.com/milocosmopolitan/omiologic-aegra/issues/60).
- **Unchanged topology boundaries.** Dynamic `interrupt()` calls, approval
  validity and effects remain outside static topology. Sentinel and branch
  facts remain experimental.

## Producer-side facts, not Cordboard constraints

These facts describe how a graph produces its document. Cordboard consumes
whatever valid JSON a graph publishes and does not follow or enforce them.

- **LangGraph versions differ by language.** The Python producer supports
  LangGraph 1.2.10–1.2.11. The TypeScript producer supports LangGraph.js 1.4.*.
- **`declare_children` is Python-only in beta.5.** A TypeScript graph that
  wraps its child call cannot declare the relationship yet.
- **Join escaping covers only the source segment.** For example, an ID
  containing `:` is not covered. In Python, a join whose sources collide with
  LangGraph's own `+` channel naming still fails inside LangGraph before
  topology extraction.

## Operator migration

Nothing is required for graphs without repeated joins or wrapped children. An
entity that adopts `declare_children` keeps its depth-0 `structureHash`, because
only interpretation evidence changes. At positive depth, the document gains
child graphs and a new hash. As with any published change, Cordboard reports it
as stale and withholds correlation until an explicit refresh:

```sh
cord graph-map <alias> <document_graph_id> <graph_id>
cord sync <alias>
```

Map the parent (for example `main`) to correlate parent Steps. Map a child
address (for example `main:call`) only to inspect that child's structure.
Cordboard does not derive that address from execution identity.

## Verification

This check measures consumer compatibility with published beta.5 producer
output. It does not measure an external entity's execution. The input is the
smallest graph for each shape: three nodes with a permuted repeated join, and
a one-node parent that wraps a two-branch child with one static interrupt. It
needs no model, credentials, Docker, graph execution or real entity.

A side-by-side probe on the same inputs showed the lifted limits. beta.4 gave
two `join:a+b:sink` records (one validation error) and
`identity-unavailable` with one graph at depths 0 and 1. beta.5 gave one join
(valid), then `declared-child-call` at depth 0 and `main`/`main:call` at
depth 1.

[`tests/test_topology_beta5.py`](../tests/test_topology_beta5.py) calls the
installed public producer. It checks the valid single-join document through
fetch, correlation and layout, and that an undeclared wrapped child stays
opaque. For a declared child, it checks a resolvable child reference, a
confirmed R3 warning inside the child, no guessed correlation, explicit
selection of the child and parent, parent Step matching, and stale-until-sync
after adopting a declaration. The beta.4 replay in
[`tests/test_topology_beta4.py`](../tests/test_topology_beta4.py) also passes
unchanged under the b5 producer.

```sh
uv sync --locked
uv run --locked pytest -q tests/test_topology.py tests/test_topology_beta4.py tests/test_topology_beta5.py
uv run --locked --group proxy --group web-test pytest -q \
  -m 'not collector and not aegra and not langfuse'
```

Executed result: the topology tests passed **33 tests**. The full service-free
command passed **665 tests, 36 deselected** in 57.31s on Python 3.12, including
the four new cases. External Collector, Aegra and Langfuse suites, and
real-entity acceptance, were not run again for this change.
