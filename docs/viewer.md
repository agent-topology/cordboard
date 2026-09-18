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
| `ambiguous` | This `graph_id` is advertised by more than one connection; recorded/live execution cannot be attributed to one of them (#43) | no | no |

A `stale` topology is deliberately never shown as current, and recorded
execution is never correlated against it, so a drifted picture cannot look
authoritative (ADR-0011/0015). Recorded execution itself is always shown
regardless of topology status — a missing, invalid, or stale manifest is a
viewer limitation, not a reason to hide what actually ran.

## Deployment scoping and the multi-graph association (#43)

A topology document's `graphs[].id` is a document-local address, never a
`cord.graph.id` (ADR-0014); this codebase does not compare them by name.

Catalog entries are scoped per registered connection (alias/endpoint), not
merely per `graph_id`. Two connections advertising the same `graph_id` each
render their own independent entry -- their own `reachable`/topology, never
merged or overwritten. Recorded or live execution for a `graph_id` shared by
more than one connection carries no other identity to attribute it to one of
them, so it renders under one additional `ambiguous` entry instead
(`topology_status: "ambiguous"`) rather than being guessed onto either.

When a Deployment's topology document publishes exactly one Graph, that
Graph's structure is unambiguous and `cord view` shows its Nodes. When a
document publishes more than one Graph, `cord view` correlates it only
through a connection's explicit `graph_map` (`cord_runtime.connections.
set_graph_map`, `cord graph-map <alias> <document_graph_id> <graph_id>`) --
an explicit `{document_graph_id: cord.graph.id}` association, set only by
that command, never inferred by name or position. A multi-graph document with
no matching `graph_map` entry (including a record from before this field
existed, read as an empty mapping) still shows its presence and
warnings/gaps, but withholds Node structure rather than guessing.

## Expanded beta.4 documents

A positive-depth document contains the root plus its materialized children in
`graphs[]`. Set `cord graph-map <alias> <document_graph_id> <graph_id>` for
the structure to correlate, even when the extra entries are only children.
The retained parent Node now matches its recorded Step; child Nodes are not
flattened into that parent's namespace. Child `subgraphId` values are document
references, not runtime Graph identities or checkpoint namespaces.

R3 reads branch facts from experimental revisions `"1"` and `"2"`, including
materialized child graphs. Unknown revisions and unknown branch facts still
produce unconfirmed warnings. After changing the published expansion depth,
run `cord sync <alias>`: the changed structure hash remains stale until that
explicit refresh. See [beta.4 adoption and verification](topology-beta4.md).

## beta.5 producer output

The beta.5 producer merges repeated or permuted join declarations into one
join. Such documents were previously `invalid` and are now drawn, with one
AND-join edge per source. A graph that calls its child through a wrapper
function can publish that child as a separate `graphs[]` entry by calling
`declare_children`. Select the parent or child with `cord graph-map` as above.
The declaration does not give runtime identity, so child Steps are not
attributed to the child graph. See
[beta.5 adoption and verification](topology-beta5.md).

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
(default 120s). More than one `--watch` target runs concurrently, each on its
own thread streaming lifecycle events onto a shared queue that one consumer
loop applies and re-renders from as they arrive -- so N targets make progress
independently (#46 AC1): none is drained to completion before the next even
starts, and the catalog is printed every time any target's state changes, not
only once after everything finishes.

Only public identities and lifecycle status are read: `cord_runtime.
aegra_client.stream_lifecycle`/`watch_lifecycle` yield exactly the `metadata`
(identity), `end` (terminal status) and `error` (a fixed diagnostic) SSE
frames; `values`/`updates`/`messages*`/`debug` frames carry graph business
state and are dropped unread. A Run's Assistant is resolved to its `graph_id`
(`describe_assistant`, the same public `/assistants` identity `cord list`
already reads) before the Run can be placed in the catalog; one that can't be
resolved is omitted with a diagnostic, never guessed. `cli.py`'s `--watch`
path (`_watch_worker`, #42) reads all three (`describe_run`,
`describe_assistant`, and the SSE stream as `watch`) through
`cord_runtime.backends.aegra.AegraExecutionBackend` rather than importing
`aegra_client` directly, though the backend still reuses these same
already-verified `aegra_client` functions underneath; behavior is unchanged.

Each watched Run's `LiveRun` is keyed by `cord_runtime.run_continuity`'s
logical Run ID for its (Deployment, Thread) pair, not the raw watched API Run
ID (#46 AC2): a resume gets a fresh Aegra API Run ID (ADR-0003 correction),
but the archive's `cord.run.id` -- and this catalog entry -- stay anchored to
the *original* submission, since a Run's OTel span never reopens on resume
(`cord_runtime.execution.resume_run`). `cord run` and `route_signal` now call
`record_submission` right after a successful `execute()`, the same call
`approval_inbox.submit_response` already made on resume, so that original
anchor actually exists; watching a Thread with no recorded submission (e.g.
started outside `cord`) falls back to the watched Run ID itself rather than
blocking or guessing (ADR-0015).

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

`cord view --watch` only follows a Run for as long as one invocation runs;
there is no standing daemon or background poller across separate invocations.
Within that one invocation, once every watched target's stream goes terminal,
`--archive` (if given) triggers a bounded convergence phase (#46 AC3/AC4,
`cord_runtime.live_reconciliation.await_convergence`): the archive is
re-read on a poll interval until each completed Run is either replaced by its
recorded counterpart -- printed as recorded in the same catalog that no
longer lists it as live, so it is never dropped from view -- or reaches the
`ingestion_failed_after` deadline, whichever comes first. No `--archive`
skips this phase entirely (nothing to converge against) and `cord view`
prints its usual single final snapshot. A Graph not otherwise named by a
connection or recorded execution is still created to hold its live Runs,
exactly like a recorded-only Graph (`topology_status: "no_connection"`).

## Execution timeline fields (#46 AC5)

`build_execution_tree` no longer trusts a Run span's own `endTimeUnixNano` as
its completion boundary: per ADR-0008, `cord_runtime.execution.run`'s span
closes the instant a graph first pauses, and `resume_run` never reopens it --
only new Step spans attach to the same trace on a resume. Each Run's `end_ns`
is instead the max of its own span end and every Step/Attempt end it
contains, the truthful outer boundary of everything actually recorded.

A Step with `resumed_from` set carries `approval_wait_ns`: the gap between
the original `awaiting_approval` Step's end and this Step's start, when that
original Step is present in the given spans. When it isn't (pruned, or never
delivered), `approval_wait_ns` is `None` -- never synthesized -- and the Run
carries `timeline_incomplete: True`. A Step still `awaiting_approval` with no
later Step resuming it is flagged `awaiting_resume: True`, evidence of an
open wait rather than a gap in the data. Each Run's `execution_ns` subtracts
only the *known* approval waits (`approval_wait_ns`, summed) from its total
`end_ns - start_ns` span, so a Run with an unmatched wait still gets a
best-effort `execution_ns` alongside its `timeline_incomplete` flag rather
than no number at all.

## Out of scope

The approval inbox (#14), graph editing, and detailed subgraph expansion at
positive depth. `cord view` reads through `check_freshness`, never
`cord sync`; the explicit refresh/snapshot path remains a separate,
still-unwired command (#11's CLI gap).


## Browser surface (#48)

`cord serve` exposes this same model as installed, read-only HTML/JSON and SSE.
See [the browser guide](browser-viewer.md) for startup, stable routes, additive
catalog fields, accessible states and installed-wheel verification.
