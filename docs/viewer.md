# `cord view`: catalog, topology, and recorded execution (#13)

`cord view` is the generic catalog/topology/recorded-execution viewer: it
shows every Graph reachable through a registered connection or present in
recorded execution, its optional published topology, and its Runs, Subjects,
Steps, and Attempts. It consumes only public contracts already implemented
elsewhere and adds no new storage:

- **Graphs** come from `cord list`'s existing `GET /assistants` probe
  (`cord_runtime.cli._probe`), the same `graph_id` a graph stamps as
  `cord.graph.id` (ADR-0002/ADR-0003).
- **Topology** comes from `cord_runtime.topology.check_freshness`, unchanged
  ([ADR-0009](decisions/0009-well-known-documents.md),
  [ADR-0011](decisions/0011-manifest-derivation.md)).
- **Recorded execution** is read through the archive contract
  (`cord_runtime.archive_query.read_spans`, reusing its shared `role`/`parent`
  identity classification rather than a second parser); a `langfuse-compare`
  cohort uses the identical normalized span shape and can be read the same
  way once wired to a connection's archive path.

```sh
$ cord view --archive examples/archive.graph-id.sample.otlp.jsonl
Graph archive-fixture  (no connection, topology: no_connection)
  Subject fixture:urn:cordboard:fixture:5
    Run 17581de7-9734-4a01-9619-3d1a1fec89c8
      Step draft -> passed
        Attempt 1, tier=fast -> failed
        Attempt 2, tier=fast -> escalated
        Attempt 3, tier=deep -> passed
```

A registered connection with no manifest and no recorded Runs still renders:

```sh
$ cord add aegra-local http://127.0.0.1:2026
$ cord view
Graph minimal-graph  (aegra-local, reachable, topology: absent)
  no recorded Runs
```

`--json` prints the same catalog as one JSON object instead of text.
`cord view <alias>` limits the catalog to one registered connection.
`--archive` is repeatable and accepts the same files/directories as
`archive-escalations` (see [archive-query.md](archive-query.md)); without it,
`cord view` still renders every connected Graph with an empty Run list.

## Topology correlation and staleness

Recorded execution is only correlated against a topology's Node structure
when the fetch is confirmed current:

| `topology_status` | Meaning | Node structure shown | Correlated? |
| --- | --- | --- | --- |
| `absent` | No published document (404) | no | no |
| `unreachable` | Deployment unreachable at fetch time | no | no |
| `invalid` | Document present but schema-invalid | no | no |
| `stale` | Fresh fetch's `structureHash` differs from the last explicit `cord sync`/refresh | no | no |
| `current` | Valid, and unchanged since the last refresh (or no prior refresh exists to compare against) | yes, if unambiguous | yes |
| `not_checked` | A connection exists but this call did not probe its topology | no | no |
| `no_connection` | No registered connection names this Graph at all; it is known only from recorded execution | no | no |

A `stale` topology is deliberately never shown as current, and recorded
execution is never correlated against it, so a drifted picture cannot look
authoritative (ADR-0011/0015). Recorded execution itself is always shown
regardless of topology status — a missing, invalid, or stale manifest is a
viewer limitation, not a reason to hide what actually ran.

## The Deployment/Graph association gap

A topology document's `graphs[].id` is a document-local address, never a
`cord.graph.id` (ADR-0014); this codebase does not compare them. When a
Deployment's topology document publishes exactly one Graph, that Graph's
structure is unambiguous and `cord view` shows its Nodes. When a document
publishes more than one Graph, no explicit Deployment/Graph association
storage exists yet to pick the right one (see the issue's "Blockers and
handoff": *"Keep deployment/assistant identity, document-local graph address
and recorded `cord.graph.id` as explicit associations, not name-based
inference"*), so `cord view` shows the document is present and its
warnings/gaps, but withholds Node structure rather than guessing by name.
Resolving this needs an explicit per-connection mapping, which is out of
scope here.

## Unmatched execution evidence

A recorded Step whose `cord.node.name` is not among a correlated topology's
Node ids is kept, not dropped, and listed under `unmatched_node_names`. A
recorded `cord.graph.id` with no registered connection at all still renders
as its own Graph entry (`topology_status: "no_connection"`), preserving
evidence the catalog cannot otherwise explain.

## Warnings

`topology_warnings` combines, as plain strings: R3 fan-out/interrupt warnings
(`cord_runtime.topology.parallel_interrupt_warnings`), the document's
per-graph `completeness.gaps`, and its document-wide `producerLimitations`.
None of these block anything ([ADR-0007](decisions/0007-catalog-rejection.md)
replaced by [ADR-0015](decisions/0015-never-block-connection.md)).

## Out of scope

Live SSE (#20), the approval inbox (#14), graph editing, and detailed
subgraph expansion at positive depth. `cord view` reads through
`check_freshness`, never `cord sync`; the explicit refresh/snapshot path
remains a separate, still-unwired command (#11's CLI gap).
