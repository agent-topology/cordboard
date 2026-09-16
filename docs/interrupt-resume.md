# Interrupt/resume span boundaries (#28)

`cord_runtime.execution` closes a Step when the graph pauses for approval and
opens a new, linked Step when it resumes, per the
[ADR-0008 interrupt addendum](decisions/0008-outcome-split.md#추가--인터럽트와-재개의-span-경계-2026-09-12).
No model, gateway, network, or Docker dependency.

## The public seam, and why no upstream request was filed

The issue asked to identify a public lifecycle seam with real start/end
boundaries and Step/Attempt association before treating this as ready, and to
record an upstream contract request rather than an internal-API workaround if
none exists.

Two candidate seams exist:

- **`agent-workflow-core`'s observer protocol** (pinned at
  `876f6a8a4ba863424e1f85bc92f18dce59147957`). Its Attempt callback reports a
  declared outcome only, with no timestamps and no Step handle
  ([`observers.py#L218-L256`](https://github.com/milocosmopolitan/agent-workflow-core/blob/876f6a8a4ba863424e1f85bc92f18dce59147957/src/agent_workflow_core/observers.py#L218-L256)).
  Omiologic (pinned at `6dcf2e56ca3c015961c4a226e2025c178ec2d5b8`) currently
  wires this to a `NoOpRunObserver`. Turning these outcome-only notifications
  into span boundaries would be exactly the "fabricated duration span" the
  issue warns against, so this repo does not build on it.
- **LangGraph's own `interrupt()` / `Command(resume=...)` contract.**
  ARCHITECTURE.md already assigns "Run execution, checkpointing, resume,
  cancellation" to Aegra/LangGraph, not to Cordboard. This is public, stable
  API, and it is exactly where `interrupt()` raises `GraphInterrupt` through
  the node function that is already calling `cord_runtime.execution` directly
  (`opaque_graph.py`'s existing pattern, not a callback). No upstream change
  request is needed: the seam this issue needed already exists and is
  unrelated to the core observer's limitation.

## Wiring inside the graph process

The graph passes its own `cord_runtime.execution.Run` (or a resumed one)
through `config["configurable"]`, the same channel `minimal_graph.py` already
uses for `cord_step`. See `examples/interrupt_graph.py`:

```python
def approve(state, config):
    active = config["configurable"].get("cord_active")
    resumed_from = config["configurable"].get("cord_resumed_from")
    with active.step("approve", StepOutcome.PASSED,
                      resumed_from=resumed_from, pause=(GraphInterrupt,)) as step:
        with step.attempt(1, outcome=AttemptOutcome.PASSED):
            decision = interrupt({"question": "approve?"})
    return {"approved": bool(decision)}
```

`Run.step(..., pause=(GraphInterrupt,))` and `Step.attempt(...)` close the
open Attempt, then the Step, the moment `GraphInterrupt` propagates out of the
node — before it continues upward to actually suspend the host Run. The Step's
`cord.outcome` becomes `awaiting_approval` and its OTel span status is left
non-error; a real exception still marks `failed` with an error status exactly
as before this change. The entity, not `cord_runtime`, decides what an
interrupted Attempt's own outcome means (it is left as declared).

`Run.continuation` returns a `RunContinuation` — five plain strings (trace ID,
Run span ID, `cord.run.id`, Subject, Graph ID) safe to persist as ordinary
checkpointed state, with no upstream API Run ID (ADR-0003's mapping problem
stays with #14). `resume_run(tracer, continuation)` reopens that trace without
recreating the "run" root span, so a resumed node opens exactly one new Step
span, carrying `cord.resumed_from` back to the previous Step's span ID.

## Verification

```sh
uv run --locked pytest -q tests/test_execution.py tests/test_interrupt_graph.py
```

`tests/test_execution.py` uses a plain exception and a monkeypatched
`opentelemetry.sdk.trace.time_ns` (no sleeping) to check the boundary rule in
isolation: Attempt closes before Step, Step outcome is `awaiting_approval`
with a non-error span status, and an injected multi-minute gap between the two
Step spans changes neither span's own `end_time - start_time`.

`tests/test_interrupt_graph.py` drives the real LangGraph seam — `interrupt()`
inside `examples/interrupt_graph.py`'s node, an `InMemorySaver` checkpointer
standing in for Aegra's, and `Command(resume=True)` — and checks the same
boundary plus identity (`cord.node.name`, `cord.graph.id`, `cord.subject.id`)
on both Step spans, and that exactly one "run" span exists across both
invocations.

`uv run --locked pytest -q -m "not collector and not langfuse and not aegra"`
passed at 153/153 after this change, confirming no regression to escalation
counts, redaction, or the Collector gate (#5, #6).

## Out of scope here

Approval inbox/expiry, Aegra API Run ID → logical Run mapping (#14), and any
change to `agent-workflow-core` or Omiologic — none of those repos were
touched.
