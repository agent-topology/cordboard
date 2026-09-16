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

## Live runs (#20)

`cord view --watch <alias>:<thread_id>:<run_id>` (repeatable) follows one
Run's Aegra SSE stream (`GET /threads/{thread_id}/runs/{run_id}/stream`,
Aegra 0.10.4's reconnect-safe join endpoint) and shows it alongside recorded
execution, until it reaches a terminal status or `--watch-timeout` elapses
(default 120s).

Only public identities and lifecycle status are read: `cord_runtime.
aegra_client.stream_lifecycle`/`watch_lifecycle` yield exactly the `metadata`
(identity), `end` (terminal status) and `error` (a fixed diagnostic) SSE
frames; `values`/`updates`/`messages*`/`debug` frames carry graph business
state and are dropped unread. A Run's Assistant is resolved to its `graph_id`
(`describe_assistant`, the same public `/assistants` identity `cord list`
already reads) before the Run can be placed in the catalog; one that can't be
resolved is omitted with a diagnostic, never guessed. `cli.py`'s `--watch`
path (`_watch_live_run`, #42) reads all three (`describe_run`,
`describe_assistant`, and the SSE stream as `watch`) through
`cord_runtime.backends.aegra.AegraExecutionBackend` rather than importing
`aegra_client` directly, though the backend still reuses these same
already-verified `aegra_client` functions underneath; behavior is unchanged.

`cord_runtime.live_reconciliation.reconcile` turns that lifecycle state into
one of three display sources, all model-free (no Tier/model field is ever
read):

| Source | Meaning |
| --- | --- |
| `live` | Still running, or completed less than `ingestion_pending_after` (5s) ago |
| `ingestion_pending` | Completed live, but no recorded counterpart has landed yet |
| `ingestion_failed` | Completed live more than `ingestion_failed_after` (300s) ago, still unrecorded |

A live Run whose `run_id` already has a recorded counterpart (from `--archive`
or elsewhere) is dropped from `live_runs` entirely -- the durable record
always replaces the temporary live one, never rendered alongside it.
Reconnects rely on Aegra's own monotonic per-Run event ids
(`{run_id}_event_{sequence}`, its `Last-Event-ID` contract): a replayed or
duplicate event never regresses a Run's status or completion time, so a
dropped connection neither duplicates a Run entry nor loses progress.

`cord view`'s single-shot render only follows a Run for as long as one
invocation runs; there is no standing daemon or background poller. A Graph
not otherwise named by a connection or recorded execution is still created to
hold its live Runs, exactly like a recorded-only Graph (`topology_status:
"no_connection"`).

## Out of scope

The approval inbox (#14), graph editing, and detailed subgraph expansion at
positive depth. `cord view` reads through `check_freshness`, never
`cord sync`; the explicit refresh/snapshot path remains a separate,
still-unwired command (#11's CLI gap).
