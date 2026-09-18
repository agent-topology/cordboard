# LangGraph callback producer (#89)

A separate, minimal-dependency distribution --
[`cord-langgraph-callbacks`](../cord-langgraph-callbacks) -- records
Cordboard Run/Step spans for a LangGraph graph an entity hosts, without any
change to that graph's own code. See
[ADR-0022](decisions/0022-langgraph-callback-producer.md) for why this is its
own distribution (not a `cord-runtime` extra) and why Subject/Graph identity
is a constructor argument rather than read from callback `metadata`.

## Why a callback, and why a separate package

ADR-0008/#28's existing instrumentation requires the graph's own node
functions to call `cord_runtime.execution` directly. A reusable library graph
cannot take that dependency (ARCHITECTURE.md's ownership split: the graph
owns business logic, the platform owns common execution vocabulary). #52
already ruled out `agent-workflow-core`'s core observer -- it reports
declared Attempt outcomes only, with no real Step start/end boundary.

LangGraph's other public seam -- `BaseCallbackHandler`, attached through
`config["callbacks"]` -- has real per-node start/end/error events and
survives an unmodified graph. `cord-runtime` cannot be the vehicle for it,
though: it pins `redact-secret==0.1.0b1`/`opentelemetry-sdk==1.39.1` exactly,
which conflicts with an entity that has already resolved different pins of
either (verified against omiologic-aegra's `redact-secret==0.1.0b4`). The
producer integration is therefore its own package with only ranged
`opentelemetry-api`/`langchain-core` dependencies, and it never constructs or
installs a `TracerProvider` -- the entity keeps its own redacted provider
(ADR-0006).

## Wiring inside the entity process

```python
from opentelemetry import trace
from cord_langgraph_callbacks import CordCallbackHandler

tracer = trace.get_tracer("my-entity")  # the entity's own redacted provider

handler = CordCallbackHandler(
    tracer,
    graph_id="channel_concept",       # from the Aegra assistant/graph the entity selected
    subject=cord_subject,             # the same value the entity puts in configurable["cord_subject"]
    subject_type="cord.subject",
)
result = graph.invoke(state, {**config, "callbacks": [handler]})
handler.close()  # ends and exports the Run span this invocation opened, if any
```

Nothing above touches the graph's own code -- `examples/callback_child_graph.py`
and `examples/callback_interrupt_graph.py` (this repo's verification
fixtures) import nothing from `cord_runtime` or `cord_langgraph_callbacks`.

## What gets recorded

- One "run" span per `close()`d handler instance that saw at least one
  `graph:step:N`-tagged event (ADR-0015: recording needs spans; an
  invocation that never reaches a node records nothing).
- One "step:\<node\>" span per Pregel task, parented on the Run directly for
  a top-level node, or nested under the calling Step (ADR-0021,
  `cord.node.name` of the enclosing call) when `metadata["langgraph_checkpoint_ns"]`
  shows it ran inside a declared child graph -- LangGraph's own
  subgraph-as-node pattern, the same mechanism `agent-topology`'s
  `subgraphId` describes statically.
- No Attempt spans. A callback only observes a Node's own invoke boundary,
  not model retries inside it (#89 scope: no Attempt/escalation inference
  from callbacks).
- `cord.outcome=awaiting_approval`, non-error span status, when the Node's
  own `interrupt()` raises `langgraph.errors.GraphInterrupt` through it
  (ADR-0008's rule, unchanged from the graph-code-instrumented path).

## Resume

`cord.resumed_from` links a resumed Step back to the Step it resumes, the
same way ADR-0008's graph-code-instrumented path does -- but a callback alone
cannot discover the previous invocation's span id: LangGraph's public
interrupt/resume contract exposes it nowhere. The verified mechanism instead
puts the entity in the loop, once, across the call boundary it already
controls:

```python
# On the interrupting invocation, after handler.close():
approval, = handler.awaiting_approval_steps   # one per parallel pending interrupt
continuation = handler.continuation
my_store.save(thread_id, continuation, approval.span_id)  # entity-owned, outside the graph

# On the resuming invocation:
continuation, resumed_from = my_store.load(thread_id)
handler = CordCallbackHandler(tracer, resume=continuation, resumed_from=resumed_from)
final = graph.invoke(Command(resume=answer), {**config, "callbacks": [handler]})
handler.close()
```

`resume=` reopens the existing trace without re-creating the "run" root span
(ADR-0008); the first Step this handler instance opens carries
`cord.resumed_from`. If an entity does not wire this, resume still works
correctly at the graph level -- it simply produces an unlinked new Step,
same as any other first invocation, rather than an error.

## Verification

```sh
uv run --locked pytest -q tests/test_langgraph_callbacks.py
```

7 tests, no Docker/model/network dependency:

- `test_semconv_version_matches_cord_runtime` -- the duplicated
  `SEMCONV_VERSION` literal (ADR-0022) has not drifted from `cord_runtime.execution`.
- `test_two_level_graph_records_nested_run_and_steps_and_passes_archive_contract`
  -- `examples/callback_child_graph.py` (two levels, zero `cord_runtime`
  imports) records a Run with "prepare"/"collect" as top-level Steps and
  "gather" nested under "collect"; the spans pass through the real
  in-process `redact_secret` binding and OTel encoding (the same public
  redactor the Collector gate runs, ADR-0006), then through the unmodified
  archive contract (`archive_query.query`/`viewer.build_execution_tree`). A
  live Collector process is environment-dependent, like this repo's other
  `collector`-marked tests, and is not exercised here.
- `test_viewer_overlays_the_recorded_child_step_under_its_topology_node` --
  a real `agent-topology.langgraph.describe()` document of that same graph
  (depth 1, one declared child call -- shaped like omiologic's
  `channel_concept`) overlays the recorded "gather" Step under "collect"'s
  resolved child structure through the existing, unchanged
  `web.presentation.run_topology` (ADR-0021).
- `test_interrupt_closes_step_as_awaiting_approval_with_non_error_status` --
  `examples/callback_interrupt_graph.py`'s real `interrupt()` call closes the
  Step correctly with zero graph-code cord_runtime awareness.
- `test_resume_links_cord_resumed_from_across_two_handler_instances` --
  two separate `graph.invoke()` calls (interrupt, then
  `Command(resume=True)`) through two separate handler instances produce a
  linked `cord.resumed_from` and exactly one Run root span across both.
- `test_untagged_chain_events_are_ignored` -- a `seq:step:*`-tagged event
  (routing/trigger functions) is never recorded as a Step.
- `test_constructor_rejects_mixing_fresh_and_resumed_run_identity` --
  `resume=` and `graph_id=`/`subject=`/`subject_type=`/`run_id=` are mutually
  exclusive at construction.

Packaging (AC3, ADR-0022): a wheel built from `cord-langgraph-callbacks/`
installed cleanly alongside `redact-secret==0.1.0b4`/`opentelemetry-sdk==1.44.0`
in an isolated venv (`uv pip check` reported every package compatible); its
declared `Requires-Dist` is `opentelemetry-api`/`langchain-core` only, and its
source never calls `TracerProvider(`/`set_tracer_provider(`.

`uv run --locked pytest -q -m "not collector and not aegra and not langfuse"`
passed at 692/692 (6 skipped, 36 deselected) after this change -- 7 more than
the prior 685/685 baseline (#86), no regression.

## Out of scope here

Entity wiring into omiologic-aegra (tracked in
[omiologic-aegra#67](https://github.com/milocosmopolitan/omiologic-aegra)),
displaying final output values, and any change to the core observer.
