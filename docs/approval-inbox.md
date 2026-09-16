# Waiting-Run discovery and authorized resume across Deployments (#44)

Closes the residual scope #14/#15/#28 left as backlog: no existing module
scanned every registered Deployment for pending runtime interrupts, and
neither `cord_runtime.run_continuity` nor `cord_runtime.approval_expiry` keyed
anything by *which* interrupt on a Thread was being answered -- both assumed
a caller already knew the single (Deployment, Thread) pair to act on.

## Runtime interrupt identity is already public

`langgraph.types.Interrupt` (pinned `langgraph==1.2.11`) carries a
deterministic `.id`, derived from the interrupting task's own namespace path
when no explicit id is supplied, and `StateSnapshot.tasks` is a tuple of
`PregelTask`, each with `interrupts: tuple[Interrupt, ...]`. A Thread's public
`GET /threads/{id}/state` already exposes this shape, so two parallel
branches that each call `interrupt()` in the same superstep produce two
distinct, independently addressable interrupts today -- no upstream or Aegra
change was needed to discover this. `examples/dual_interrupt_graph.py` and
`tests/test_dual_interrupt_graph.py` demonstrate it against the real
LangGraph seam, not just the pinned source.

## `cord_runtime.approval_inbox`

- `discover_waiting(board_dir, reminder_after=, timeout_after=, now=)` scans
  every registered connection (`cord_runtime.connections`), filters to
  Threads `cord_runtime.run_continuity` already knows about whose backend
  `status()` is `RuntimeStatus.INTERRUPTED`, and reads `get_state` to list
  every `WaitingInterrupt`: `(deployment, assistant, subject, logical_run_id,
  thread_id, interrupt_id, value, revision)`. It starts (or recovers, restart
  -safe) each waiting Thread's `approval_expiry` clock exactly as before this
  change. A Deployment or Thread that raises a transport error while being
  probed is skipped, never raised -- one unreachable Deployment cannot hide
  every other Deployment's waiting interrupts.
- `submit_response(board_dir, deployment=, thread_id=, interrupt_id=,
  approver=, response_value=, revision=, auth_boundary=, now=)` routes one
  operator's answer to the exact interrupt it targets, in order:
  1. `cord_runtime.response_dedupe.claim` -- a duplicate/racing submission
     for the same interrupt stops here, before authorization or transport.
  2. `auth_boundary.authorize` (`cord_runtime.entity_auth.EntityAuthBoundary`)
     -- rejected, stale, or revision-mismatched submissions never reach
     `backend.resume`. A UI-supplied `approver` is only ever a claim passed
     to the boundary, never authority on its own.
  3. `backend.resume` -- an ambiguous-transport `RuntimeError` is recorded as
     `UNKNOWN`, never retried automatically; a clean resume is recorded
     `RESUMED` and, best-effort, closes the Thread's `approval_expiry` wait.

## `cord_runtime.entity_auth`

`EntityAuthBoundary` is the narrow seam a real owning entity implements
against (ARCHITECTURE.md "Isolation, routing, and approval"; #14's own
decision, preserved here: "reuse the owning entity's public
authorization/resume boundary; do not copy its Slack/Git validator into
Cordboard"). `SyntheticEntityAuthBoundary` is the one in-repo implementation,
for tests only -- never a production authorization path. It checks, in
order, whether the interrupt is still pending, whether the submitted
`revision` matches the Thread's current checkpoint, and finally whether the
claimed `approver` has a registered grant for that `(deployment, assistant)`
pair -- so a stale or revision-mismatched submission is never misreported as
merely "unauthorized."

## `cord_runtime.response_dedupe`

`approval_expiry.py`'s own docstring documents its first-writer-wins
disposition as a *single-process, sequential-ordering* guarantee, not
cross-process file locking. `response_dedupe` is the cross-process dedupe
#14 named as backlog: a durable, `flock`-guarded claim per `(deployment,
thread_id, interrupt_id)`, taken before a resume submission is attempted, and
exactly one of three durable outcomes recorded after -- `RESUMED`,
`REJECTED`, or `UNKNOWN`. It reuses `cord_runtime.concurrency`'s exact
`_locked`/atomic-write pattern, including the same real-concurrent-arrival
race test shape (`threading.Barrier`, #19's lesson reapplied to responses).

## Out of scope here

Slack/SMTP delivery, domain plan implementation, trusting an arbitrary
`approver_subject`, real business mutations, and any production
(non-synthetic) `EntityAuthBoundary` implementation -- that stays
entity-owned. No `cord` CLI or web surface is added by this change; per #44's
own handoff, that is a later "web controls" slice.

## Verification

```sh
uv run --locked pytest -q tests/test_approval_inbox.py tests/test_entity_auth.py \
  tests/test_response_dedupe.py tests/test_dual_interrupt_graph.py
uv run --locked pytest -q -m "not collector and not langfuse and not aegra"
```

The first command exercises: waiting-interrupt discovery across a reachable
and an unreachable Deployment, two distinguishable interrupts on one Thread,
the authorization boundary's stale/revision-mismatch/unauthorized paths (each
isolated to its own failure reason), the response-dedupe duplicate-submission
refusal, an ambiguous resume transport failure recorded `unknown` and never
retried, and the real LangGraph seam producing two distinct `Interrupt.id`s
that resume independently via `Command(resume={id: value, ...})`. The second
command is the full non-Docker, non-pinned-binary suite; it passed at
504/504 after this change (31 deselected: `collector`/`langfuse`/`aegra`
markers), confirming no regression to escalation counts, redaction, the
Collector gate, or the existing interrupt/resume span boundary (#5, #6, #15,
#28).

Real Aegra HTTP/SSE verification of this contract (the actual `/state`
response shape, real revision/checkpoint field names under load, and a real
authorized-boundary integration) is Testbed's job per ADR-0016 §3, not this
repo's -- the 2026-09-16 Testbed handoff comment on #44 names this as the
next external verification once this lands.
