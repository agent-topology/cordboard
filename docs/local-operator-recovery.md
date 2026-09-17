# Making the recurring local operator loop recoverable (#75)

This records the real restart/disconnect operator session #75's own
refinement gate asked for -- "Backlog until #74 identifies the real operator
session and actual recovery friction" -- and the one genuine gap it found,
fixed, and re-verified. #74's own pilot ([docs/real-entity-pilot.md](real-entity-pilot.md))
never stopped or restarted anything (its Omiologic server ran continuously
throughout), so it produced no recovery friction to ground #75's generic
restart/upgrade language in; this session supplies that evidence directly
instead of replaying every Testbed failure, per #75's own Verification
section. It reuses only the connection/execution/observation surfaces
Slices 7-8 (#71-#73) already proved; no new command, HTTP route, or JSON
shape was added.

## What already covers this outcome, unexercised here

Before running anything, reading the existing implementation (not the
design docs) showed most of #75's scope already built, each with its own
existing test coverage this session did not need to reproduce:

- **Rediscovery without cached health** -- `cord diagnose`/`cord run`/`cord
  list` never cache reachability; every invocation is a fresh live probe
  (`cord_runtime.connection_diagnostics.diagnose_connection`, #72). A
  restarted external Deployment is therefore rediscovered by construction,
  not by any restart-specific logic.
- **Truthful recorded/live/pending identity** -- `cord_runtime.run_continuity`,
  `cord_runtime.pending_completions`, and `cord_runtime.live_reconciliation`
  are already `flock`-guarded, file-backed, and restart-safe by design (#14,
  #46, #50); each has its own deterministic test suite
  (`tests/test_run_continuity.py`, `tests/test_pending_completions.py`,
  `tests/test_live_reconciliation.py`). This session did not re-derive that
  evidence live.
- **Waiting approvals resume or stay unavailable, never silently duplicated**
  -- `cord_runtime.approval_expiry.record_waiting`'s own docstring already
  states a restart never grants an already-waiting approval a fresh clock;
  `cord_runtime.response_dedupe` claims resumption `flock`-guarded and
  cross-process (#49/#51). Covered by `tests/test_approval_expiry.py` and
  `tests/test_response_dedupe.py`.
- **External Deployments are never started or stopped implicitly** --
  `cord_runtime.connections.is_managed`/`deployment_lifecycle.ensure_started`
  only ever touch a connection that opted in with `--launch` (#17); a
  connection with no `launch` is only ever contacted. Verified live below.

The one place this reading found a real, demonstrable gap was the managed
(opted-in) lifecycle path: a crashed managed process's stale tracked record.

## Fixture used below

The already-documented [opaque fixture](onboarding.md#fixture-used-below)
(`aegra/opaque.json` deploying `examples/opaque_graph.py`), launched the same
way [docs/onboarding.md](onboarding.md) already launches it, on a board-local
Postgres (`aegra/compose.yaml`, `POSTGRES_PORT=5433`). This session did not
touch the operator's own live Omiologic pilot server from #74 -- restarting
that shared, long-running dev process for another project carries real risk
disproportionate to what a fully local, disposable fixture already proves
about the exact same generic connection/diagnostic code path (#73's dependency-
free surfaces do not distinguish which Deployment they are pointed at).

## Scenario 1: external Deployment stop/restart -- rediscovered, no gap

```sh
cord add opaque-ext http://127.0.0.1:2027
cord diagnose opaque-ext --json   # execution: ready
cord run opaque-ext opaque-graph cordboard-75-ext-<ts> input.json --timeout 30
# {"run_id": "...", "status": "success", "values": {"items": ["a","b","c"], "size": 3}}
kill -9 <uvicorn pid>              # simulate a crash/stop outside Cordboard
cord diagnose opaque-ext --json   # execution: unavailable, exit 1 -- no manual edit
# ... restart the same server command ...
cord diagnose opaque-ext --json   # execution: ready again
cord run opaque-ext opaque-graph cordboard-75-ext-post-restart-<ts> input2.json --timeout 30
# {"run_id": "...", "status": "success", ...} -- genuinely usable, not just /health
```

Confirms AC1 for the external case: rediscovery required zero code changes
and zero state-file edits, because reachability is never cached.

## Scenario 2: managed Deployment crash -- the real gap

```sh
cord add opaque-managed http://127.0.0.1:2028 --launch "uv run --project aegra python -m examples.aegra_server --config aegra/opaque.json --port 2028 ..." --idle-after 5
cord rule add r1 manual opaque-managed opaque-graph subject --input items=items
cord signal manual signal.json --timeout 40
# {"status": "routed", "result": {"status": "success", ...}}   -- ensure_started launched it
```

`deployment_lifecycle.json` now tracks `{"status": "running", "pid": 34388,
"active_runs": 0}` for the `uv run` wrapper pid. Killing the real HTTP-serving
child (`kill -9` on the uvicorn worker, not the tracked wrapper pid --
`examples/aegra_server.py`'s own `process.wait()` then exits the wrapper too,
so all three processes in the launch chain exit) simulates exactly what an
operator restart/crash/OOM looks like from Cordboard's side: the record still
says `running`, but nothing is listening.

Before the fix:

```sh
cord diagnose opaque-managed --json
# execution: unavailable/unreachable
# managed_lifecycle: ready/running   <- contradicts execution; stale and wrong
cord signal manual signal2.json --timeout 15
# {"status": "execution_failed", "error": "Aegra API request failed; check service readiness and input contract"}
# deployment_lifecycle.json unchanged: still "status": "running", pid 34388 (dead)
```

`deployment_lifecycle.ensure_started` (`src/cord_runtime/deployment_lifecycle.py`)
only checked `record["status"] == RUNNING` from the stored file -- never
whether the tracked process (or its endpoint) was actually still alive. A
crashed managed process therefore wedges: every submission fails against the
dead endpoint, and the record only self-heals once `idle_after` elapses *and*
an operator remembers to run `cord deployment sweep` -- exactly the "hidden
state repair" #75's Outcome says an operator must not need. (`active_runs`
itself does not leak: `route_signal`'s `finally` block still releases it on
`execution_failed`, so this is a liveness gap, not a claim-counting one.)

## The fix

`ensure_started` now re-verifies a stored `RUNNING` record with a bounded,
single-shot liveness probe (`_default_is_alive`, 1s timeout -- distinct from
the existing `health_check`, which polls up to `health_timeout` for a
*freshly launched* process to become healthy) before trusting it; a record
that fails this recheck falls through to the same relaunch path a
never-started Deployment already takes, so nothing else about start/idle/stop
semantics changes. `connection_diagnostics._managed_lifecycle` now also takes
the connection's own `execution` reachability fact and reports `unavailable:
stale` instead of a contradictory `ready: running` when the tracked record
disagrees with it -- visibility only, no state mutation (a passive
diagnostic still never calls `ensure_started`, unchanged from #72).
`web/presentation.STATE_CATALOG["lifecycle_status"]["stale"]` gives that
status the same labeled vocabulary every other status already has.

After the fix, replaying the exact same crash:

```sh
cord diagnose opaque-managed --json
# managed_lifecycle: unavailable/stale   <- now visible, not contradictory
cord signal manual signal3.json --timeout 40
# {"status": "routed", "result": {"status": "success", ...}}   <- auto-restarted
cord diagnose opaque-managed --json
# execution: ready, managed_lifecycle: ready/running   <- consistent again
```

No `cord deployment sweep`, no waiting for `idle_after`, no edit to
`deployment_lifecycle.json`. `tests/test_deployment_lifecycle.py` adds
`test_a_stale_running_record_is_relaunched_not_trusted`,
`test_a_stale_running_record_that_fails_to_relaunch_is_recorded_start_failed`,
and a real-process analogue (`test_default_is_alive_relaunches_after_a_real_process_is_killed`,
using the module's existing `tiny_server` fixture) that kills a live process
and confirms the default prober -- not a fake -- detects it.
`tests/test_connection_diagnostics.py` adds
`test_lifecycle_running_but_unreachable_is_stale_not_a_false_ready` and a
guard, `test_lifecycle_stopped_and_unreachable_is_still_plain_stopped_not_stale`,
confirming a deliberately stopped (idle-swept) Deployment being unreachable
is not misreported as this new crash-only status.

## Scenario 3: waiting approval across a crash

Not independently re-verified live in this session: `approval_inbox.
submit_response` (`src/cord_runtime/approval_inbox.py:220`) calls the same
`ensure_started` this fix changed, before ever resuming, so it inherits the
same liveness recheck through one shared code path rather than a duplicated
one. `discover_waiting`'s own identity (deployment, thread_id, interrupt_id)
is read from `run_continuity`/Aegra's own `/state`, independent of whether
the managed process is currently up, so a crash does not lose or duplicate a
waiting approval's identity -- only delays resuming it until the Deployment
is reachable again, which is the documented, correct behavior (never retry
an ambiguous submission automatically).

## Scenario 4: reinstall / package upgrade

```sh
uv pip install --reinstall-package cord-runtime -e .
cord --board <board> list
# opaque-ext, opaque-managed, omiologic-ext all still listed, reachable=true
```

Board state (`.cordboard/*.json`) lives in the operator's board directory,
entirely outside the installed package/venv, so a reinstall of the exact same
version cannot touch it by construction -- confirmed above, not assumed.

The forward-looking half of AC5 ("fails with a bounded migration diagnostic")
is a recorded, correctly-scoped limit rather than something built
speculatively here: every schema change to `.cordboard/*.json` so far has
been additive-only, defaulted on read (`cord_runtime.connections`'s own
docstring: "A record with no `graph_map` key is read as an empty mapping...
every connection record predating this field still loads unchanged"), so no
upgrade has ever actually broken board-state compatibility -- there is no
real failure yet to bound a diagnostic around. A future *breaking* field
change would currently surface as a raw `KeyError`/`TypeError` rather than a
labeled diagnostic; that is the actual, minimal, named gap (not "add a
version-stamp migration subsystem" -- nothing in this codebase's history
justifies that scope yet).

## Acceptance criteria

- [x] A stopped and restarted qualified entity is rediscovered or diagnosed
      through the existing board without manual state-file edits -- Scenario
      1 (external) and Scenario 2 (managed, after the fix).
- [x] Recorded Runs remain readable and live/pending Runs retain truthful
      logical identity and delivery state -- already covered by
      `run_continuity`/`pending_completions`/`live_reconciliation`'s own test
      suites; not re-exercised live here (see "What already covers this").
- [x] Waiting approvals either resume through the configured entity authority
      or remain explicitly unavailable; none are silently accepted or
      duplicated -- already covered by `approval_expiry`/`response_dedupe`;
      Scenario 3 traces the shared code path rather than re-deriving it live.
- [x] External Deployments are never started/stopped implicitly, while
      opted-in managed connections retain bounded lifecycle behavior --
      Scenario 1 (external, never touched) and Scenario 2 (managed, now
      genuinely bounded: a crash self-heals on the next submission instead of
      wedging until `idle_after` and a manual sweep).
- [x] Reinstall or artifact upgrade preserves documented compatible state or
      fails with a bounded migration diagnostic -- Scenario 4: the common
      case (reinstall, no schema change) is verified; the uncommon case (a
      hypothetical breaking schema change) is recorded as a named, currently
      unbounded gap rather than fabricated coverage.
- [x] One repeatable end-to-end operator session records actual recovery
      evidence and remaining ownership limits -- this document; every command
      above uses only fixtures and connections this repo already owns and
      can reproduce without third-party credentials.

## Handoff

Exercising the same crash against a graph with a real interrupt seam (#74's
handoff already named `issue_resolution` as the candidate once its no-op
observer is replaced) would additionally verify that a resumed approval after
a managed-Deployment crash reaches the correct logical Run with no duplicate
effect -- Scenario 3 above traces the code path but does not replay it
end-to-end live. A future Feature that changes a `.cordboard/*.json` field in
a non-additive way needs a bounded-diagnostic load path for that specific
change; this session intentionally does not build that ahead of a real
instance of it.
