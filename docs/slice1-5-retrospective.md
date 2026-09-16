# Slice 1–5 retrospective and Testbed follow-up

Reviewed 2026-09-16 against product commit
[`01ec27e5e4bef60db0c9e38590660499b2374b45`](https://github.com/agent-topology/cordboard/tree/01ec27e5e4bef60db0c9e38590660499b2374b45),
the current GitHub issue bodies, the external Testbed verification record, and
agent-workflow-core revision
[`876f6a8a4ba863424e1f85bc92f18dce59147957`](https://github.com/milocosmopolitan/agent-workflow-core/tree/876f6a8a4ba863424e1f85bc92f18dce59147957).

Tracking: [Slice 1~5 Retrospect & Testbed](https://github.com/agent-topology/cordboard/milestone/8).
This review creates follow-up work; it does not implement those features, rerun
historical suites, reopen closed issues, or close the original milestones.

## What the previous slices established

The product has useful independent pieces: public deployment connection and
execution, optional topology reading, CLI catalog rendering, explicit
Run/Step/Attempt instrumentation, local continuation and expiry stores,
Signal/Rule routing, claim-based deduplication, managed lazy startup, completion
cascades and a lifecycle reconciliation model. The redaction/Collector/archive
foundation and optional Langfuse comparison already have historical evidence.

The weakness is integration between those pieces and the strength of the
completion claims. Closed issues are not interchangeable with fulfilled original
acceptance criteria. Some checklists remain unchecked, but unchecked boxes alone
also do not prove missing implementation: each gap below has a code or documented
scope basis.

| Slice / earlier issues | Implemented or recorded evidence | Remaining gap and follow-up |
| --- | --- | --- |
| Slice 1: #11, #12, #13, #28 | Existing deployment CLI; schema/freshness warnings; text/JSON catalog; public LangGraph pause/resume span helpers | Explicit topology refresh and multi-graph association, deployment-scoped identity, real producer acceptance, browser rendering: #41, #43, #47, #52 |
| Slice 2: #14, #15 | Thread-to-logical-Run mapping helper; expiry policy for one known wait; span-based repeated-execution warning | Mapping is not connected to actual submission/viewer; no shared authorized inbox; response races are only sequential in one process; real resume request mismatch: #40, #44 |
| Slice 3: #16, #17 | Shared manual/file/schedule routing; persistent dedupe/concurrency stores; managed startup/idle sweep | Resume startup, ambiguous outcomes and long-running execution claims need integrated evidence: #42, #50 |
| Slice 4: #18, #19 | Immediate successful completion cascade; barrier-controlled isolation tests | Completion after initial polling deadline/resume and real multi-entity isolation remain to prove: #50, #51 |
| Slice 5: #20 | SSE lifecycle folding, replay handling and live/recorded source vocabulary | Command drains streams and renders once; no continuous archive re-read/browser update or wired logical resume identity: #45, #46, #47 |

## Concrete findings

- **Deployment collisions:** `viewer.build_catalog` builds
  `connection_by_graph[graph_id]`, so later connections overwrite earlier ones.
  Recorded/live grouping also uses graph IDs without a deployment partition.
  [Source](https://github.com/agent-topology/cordboard/blob/01ec27e5e4bef60db0c9e38590660499b2374b45/src/cord_runtime/viewer.py#L189).
- **Unwired continuity:** Graft callers for `record_submission` show test callers
  only. The mapping helper is not the shared approval/submission implementation
  required by #14. [Contract](../src/cord_runtime/run_continuity.py).
- **Approval races:** [approval-expiry.md](approval-expiry.md) explicitly limits
  the existing disposition guarantee to single-process sequential ordering.
  It is not cross-process deduplication of concurrent responses.
- **One-shot viewing:** `_watch_live_run` reconciles against an empty recorded
  set, and `cmd_view` reads archive files once after following streams.
  [Source](https://github.com/agent-topology/cordboard/blob/01ec27e5e4bef60db0c9e38590660499b2374b45/src/cord_runtime/cli.py#L130).
  The current catalog does not carry the full timing needed for a browser timeline.
- **Ambiguous public result:** `aegra_client` uses `waiting` both for an actual
  interrupt and an unfinished invocation after the client deadline; successful
  execution reads full checkpoint `values`. The backend work is a compatibility
  change, not merely moving a file.
- **Actual resume mismatch:** the Testbed's first Aegra 0.10.4 HTTP resume request
  without `assistant_id` returned 422; the request with an explicit assistant
  passed. Current `aegra_client.resume` omits this field. This is an observed
  external request difference, not a test of the product helper.
- **Evidence boundary:** [Testbed verification](https://github.com/agent-topology/cordboard-testbed/blob/main/VERIFICATION.md)
  records three real-process passes for discovery, fresh execution and direct
  HTTP interrupt/resume. Collector export, archive scanning, negative gates,
  Langfuse, SSE/cancel, multi-entity/routing and private entities were not run.
  The present retrospective did not rerun those three tests.

## Collector and viewer responsibilities

The target path is:

```text
Entity-owned public instrumentation
  -> in-process public redaction
  -> Collector redaction-stamp gate
  -> append-only OTLP archive
  -> shared execution read/update model
  -> CLI and local web viewer

Execution API lifecycle -> shared execution read/update model
Collector delivery/health evidence -> separate ingestion diagnostics
Collector -> optional Langfuse branch
```

The Collector receives, gates and exports. It is neither a graph/approval state
store nor the viewer's query API. The archive remains the record of origin;
Langfuse is optional. A successful run does not imply successful telemetry
delivery, and a clean empty archive does not prove redaction.

The viewer must separately expose runtime state, live/recorded source, partial
or unavailable telemetry, and topology correlation status. It uses actual span
boundaries for Step/Attempt duration and shows human approval waiting separately.
It retains unmatched/ambiguous evidence instead of guessing parents, deployment
identity or business state. A missing or stale topology must not hide execution.

The web scope includes both observation and execution/approval controls. The
server defaults to loopback and uses platform command validation plus the
entity's authorization boundary. A synthetic authorized entity boundary will
prove those contracts before Omiologic integration; Omiologic's continuing
development does not block the product.

## Useful agent-workflow-core contracts and limits

The inspected core is a private, runtime-neutral workflow library, package
version `0.1.0b3`; it is not a server or a mandatory Cordboard dependency.

| Public contract | Potential reuse | Limit to preserve |
| --- | --- | --- |
| `GraphObserver` / `RunObserver` | Entity-side identity, declared Step/Attempt outcomes | Current callbacks have no actual start/end lifecycle and no mandatory Step handle for an Attempt; do not fabricate duration spans |
| `ObserverMetadata` | Bounded, classified metadata and optional usage | Classification is not a substitute for the public redactor or Collector gate |
| `EventPayload` / `EventStore` | Explicit correlation/causation and deduplicated event collection | Sequence is run-local collection order, not causal order, SSE cursor or OTel span ID; memory storage is not restart durability |
| LangGraph `observer_from_runtime` | Injected graph observer and explicit `event_run_id` continuity | A no-op/failing observer produces no tracing proof; bridge identity alone does not instrument duration |
| Effect/approval and notification contracts | Preserve entity authorization/plan validation; distinguish accepted/failed/unknown receipts | Do not copy business validators into Cordboard; notification receipts are not execution dedupe or exactly-once delivery |

Sources:
[observer protocol](https://github.com/milocosmopolitan/agent-workflow-core/blob/876f6a8a4ba863424e1f85bc92f18dce59147957/src/agent_workflow_core/observers.py#L218),
[runtime bridge](https://github.com/milocosmopolitan/agent-workflow-core/blob/876f6a8a4ba863424e1f85bc92f18dce59147957/src/agent_workflow_core/adapters/langgraph/observer.py#L42),
[core event decision](https://github.com/milocosmopolitan/agent-workflow-core/blob/876f6a8a4ba863424e1f85bc92f18dce59147957/docs/decisions/0006-unified-events-and-notifications.md).

#52 qualifies an optional producer seam. It can finish with a demonstrated
limitation and a minimal upstream contract request; no other new feature depends
on it. The basic synthetic path uses public LangGraph/OTel instrumentation,
already demonstrated by the product's [interrupt fixture](interrupt-resume.md).
No core or Omiologic repository changes are authorized by this tracking work.

## Work packages and order

| Issue | Type | Delivery boundary |
| --- | --- | --- |
| [#40](https://github.com/agent-topology/cordboard/issues/40) | Bug | Fix Aegra resume submissions with explicit assistant identity |
| [#41](https://github.com/agent-topology/cordboard/issues/41) | Feature | Establish artifact-based synthetic deployment acceptance |
| [#42](https://github.com/agent-topology/cordboard/issues/42) | Feature | Separate execution transport, runtime status, and public results |
| [#43](https://github.com/agent-topology/cordboard/issues/43) | Feature | Scope execution and topology identities by deployment |
| [#44](https://github.com/agent-topology/cordboard/issues/44) | Feature | Complete durable approval discovery and authorized resume |
| [#45](https://github.com/agent-topology/cordboard/issues/45) | Feature | Expose Collector delivery health and trustworthy archive ingestion |
| [#46](https://github.com/agent-topology/cordboard/issues/46) | Feature | Continuously reconcile live execution with durable records |
| [#47](https://github.com/agent-topology/cordboard/issues/47) | Feature | Provide a local web viewer with execution and approval controls |
| [#48](https://github.com/agent-topology/cordboard/issues/48) | Task | Render deployment topology and live execution timelines in the browser |
| [#49](https://github.com/agent-topology/cordboard/issues/49) | Task | Add validated execution and approval controls to the web viewer |
| [#50](https://github.com/agent-topology/cordboard/issues/50) | Feature | Complete routing and managed lifecycle across asynchronous runs |
| [#51](https://github.com/agent-topology/cordboard/issues/51) | Feature | Qualify multi-entity and failure scenarios in synthetic CI |
| [#52](https://github.com/agent-topology/cordboard/issues/52) | Feature | Qualify optional agent-workflow-core producer integration |

#40 and #41 are ready and independent. Begin one implementation at a time.
#43 and #52 have no implementation predecessor, but require the contract
refinement named in their bodies before becoming ready.

The principal chains are #40 → #42 → #44 → #46 → #48 → #49,
with #43 also required for #44/#48, and #41 → #45 also required for #46.
#50 integrates #42/#44 into asynchronous routing. #51 qualifies the combined
multi-entity/browser path. #47 owns integrated browser acceptance through
child Tasks #48 and #49; it has no redundant parent implementation PR.

Existing Epics #2 and #3 remain the parents; no new Epic or project board is
needed. Native issue types, hierarchy and prerequisite edges mirror the body
links. Backlog labels mean prerequisites or specification details remain;
closed historical issues do not silently satisfy those new conditions.

## Minimum distinguishing fixtures and completion evidence

Use an integer-transform graph, a declared failure/retry graph, a checkpointed
approval followed by a safe counter effect, and two entities advertising the
same graph ID. Use two overlapping distinct Subjects plus one same-Subject
conflict pair, controlled clocks/barriers, and a small known span cohort.

Verify success separately from negative controls: wrong authority, stale
interrupt, duplicate response/completion, process restart, SSE replay, one entity
failing independently, Collector delay/unavailability, and rejected unstamped or
blocked spans. Never require a large campaign or real provider just to measure
identity, lifecycle or viewer correctness.

Final evidence includes the wheel digest and dependency inventory, exact
commands, expected/actual counts, public archive checks, browser assertions and
inspected screenshots. Tests of pure helpers stay in Cordboard; actual process
and browser acceptance belongs in Testbed. Migrate old fixtures only after
equivalent evidence and reference/helper updates exist. No historical archive
or test result is relabeled as a new pass.
