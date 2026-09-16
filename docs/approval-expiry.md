# Approval expiry, halt, and repeated-execution evidence (#15)

`src/cord_runtime/approval_expiry.py` applies board-level reminder/timeout/
halt policy to a waiting (interrupted) Run and durably records exactly one
disposition -- `"resumed"` or `"halted"` -- for it, without inspecting or
reimplementing entity/core approval validation (ARCHITECTURE.md "Isolation,
routing, and approval": graphs decide where to interrupt and what a response
means; the platform owns only the inbox and the reminder/expiry clock).

## The clock, injected rather than read

Every function takes `now` explicitly instead of calling `time.time()`
itself, so reminder, timeout, and the halt action can be exercised by a test
with a controlled clock and no real waiting:

```python
record_waiting(board_dir, "aegra-local", thread_id, run_id,
               now=1000.0, reminder_after=60.0, timeout_after=300.0)
evaluate(board_dir, "aegra-local", thread_id, now=1300.0)  # -> TIMEOUT_DUE
```

`record_waiting` is idempotent per (Deployment, Thread): a later call --
including one made after a process restart, with a different `now` -- returns
the first-recorded deadlines and `run_id` unchanged. This is the restart
guarantee: a waiting approval never gets a fresh clock just because the board
process restarted. `run_id` here is expected to be `cord_runtime.run_continuity`'s
*logical* Run ID, not a raw Aegra API Run ID, so a resume's fresh API Run ID
is never mistaken for a new pending execution.

## One durable disposition, first-writer-wins

`resolve_approved` and `resolve_timed_out` both write through the same
first-writer-wins path: whichever call reaches a still-undisposed record
first sets `disposition`, and every later call -- for either outcome --
returns that same stored disposition instead of overwriting it. Concretely:

- `resolve_timed_out` checks the stored disposition *before* calling `halt`.
  If the approval already resolved (a concurrent `resolve_approved`), `halt`
  is never called, so a Run that was just approved is never also cancelled,
  and no graph-specific rejection is ever forged on the graph's behalf.
- `halt` is a zero-argument callable performing the platform's one chosen
  public action -- `cord_runtime.aegra_client.cancel`, the run's own public
  cancellation, never a synthetic `command.resume`. A `RuntimeError` it
  raises (ambiguous transport, per that module's contract) propagates
  unchanged and the wait is left undisposed, safe to reconsider on the next
  clock tick instead of being silently recorded as halted when the
  cancellation's own outcome is actually unknown.

This is a single-process, sequential-ordering guarantee (call order decides
the winner), not cross-process file locking; true concurrent-process dedupe
of responses remains backlog per #14's own blockers, same as before this
change.

## Repeated-execution evidence in the viewer

`cord_runtime.viewer.build_execution_tree` now carries `resumed_from` and
`repeated_execution` on every Step record. A normal resume links exactly one
new Step to the Step it resumes, so `resumed_from` values are expected to be
unique; when more than one Step declares the *same* `resumed_from` (e.g. a
duplicate resume submission reached the graph twice), every Step after the
earliest-starting one is flagged `repeated_execution: True`. `cord view`'s
text output prints a warning line for a flagged Step. This is span evidence
only -- "a new Step span alone is not proof of duplicate business mutation"
(#15) -- effect probes, receipts, and idempotency stay graph-owned.

## Replay-risk fixtures

`examples/replay_counter_graph.py` demonstrates why: `build_unsafe_graph`
increments a counter *before* `interrupt()` in the same node, so LangGraph's
replay-from-the-top-on-resume behavior runs that increment once during the
paused invocation and again during the resumed one -- two increments for one
logical approval. `build_safe_graph` moves the effect into its own node,
reached only once approval completes, and additionally guards it with a
graph-owned idempotency set keyed on `cord_resumed_from`, so even a duplicate
resume for the same paused point cannot repeat the effect. Neither fixture is
a template generator; both are minimal, model-free graphs for this test only.

## Verification

```sh
uv run --locked pytest -q tests/test_approval_expiry.py tests/test_aegra_client.py \
  tests/test_viewer.py tests/test_replay_counter_graph.py
uv run --locked pytest -q -m "not collector and not langfuse and not aegra"
```

The first command exercises the controlled-clock reminder/timeout/halt walk,
the restart-preserves-deadline guarantee, the approval-vs-timeout race (both
orderings), the ambiguous-halt-transport path, the viewer's repeated-execution
flag, and the unsafe (`count == 2`) vs. safe (`count == 1`) replay comparison
across one real LangGraph pause/resume cycle -- no provider key, model stub,
proxy, or real waiting anywhere. The second command is the full non-Docker,
non-pinned-binary suite; it passed at 290/290 after this change, confirming no
regression to escalation counts, redaction, the Collector gate, or the
existing interrupt/resume span boundary (#5, #6, #28).

## Out of scope here

Waiting-Run discovery across Deployments, the authorized submission boundary,
SMTP/Slack reminder delivery (existing core executors are reused, not
duplicated), production scheduling, and inspecting arbitrary graph side
effects -- none of those are implemented by this change; #15's own scope
section rules them out explicitly.
