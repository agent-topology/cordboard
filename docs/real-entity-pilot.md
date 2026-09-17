# Qualifying one maintained entity through public contracts (#74)

This records the exact scenario the issue's own refinement gate asked for
before implementation: the named entity and revision, verified access, and
each contract's real result -- replacing the issue's conditional acceptance
language. It reuses only the connection/execution/observation surfaces
[docs/onboarding.md](onboarding.md) (#73) already proved generic; no
`cord_runtime` code changed for this Feature.

## Selected entity and revision

[Omiologic](https://github.com/milocosmopolitan/omiologic-aegra)
(`milocosmopolitan/omiologic-aegra`) at commit
`6dcf2e56ca3c015961c4a226e2025c178ec2d5b8` -- the same commit the
[2026-09-15 internal dependency review](internal-dependencies.md) already
inspected. It is independently maintained (its own repository, its own
release history, ADR-0016 keeps runtime provisioning and credentials with its
owner) and non-synthetic: a real Aegra server was already running under the
entity owner's own process
(`/Users/minhokang/Work/aps/entities/omiologic/.venv/bin/python -m uvicorn
aegra_api.main:app --host 127.0.0.1 --port 2026`, `git rev-parse HEAD` at that
checkout matches the pinned commit above) with a live Postgres-backed
checkpointer and store (`GET /health` -> `{"status":"healthy","database":
"connected","langgraph_checkpointer":"connected","langgraph_store":
"connected"}`). Cordboard did not start, configure, or provision this
process -- it only connected to an endpoint the owner already runs, per
ADR-0016.

## Selected graph: `readiness`

Omiologic publishes three graphs (`GET /assistants/search`):
`core_notification_fixture`, `issue_resolution`, `readiness`. `readiness` was
selected, not the larger `issue_resolution` asset, because:

- It is the smallest maintained graph that still distinguishes the execution,
  topology, and telemetry contracts (its own docstring: "A dependency-free
  graph used to verify the Omiologic Aegra boundary").
- It performs no model call, no Git/GitHub effect, and no Slack notification
  -- so qualifying it carries no risk of a real business side effect, per
  this issue's Out of scope.
- `issue_resolution` carries real Git/GitHub/Slack effects and, per
  [docs/decisions/cordboard-upstream-requirements.md](decisions/cordboard-upstream-requirements.md)'s
  2026-09-15 section, its observer is currently a no-op -- exercising it
  would not add real telemetry evidence over `readiness` while adding real
  business risk.
- `readiness` has no interrupt node (confirmed from its published source);
  the approval/authorization contract is therefore recorded as unqualified
  below rather than fabricated, per this issue's own instruction.

## Exact scenario run

```sh
cord --board <board> add omiologic http://127.0.0.1:2026
cord --board <board> diagnose omiologic --json
cord --board <board> run omiologic readiness "cordboard-qualify-74-<ts>" input.json --timeout 60
```

`input.json`: `{"messages": [{"role": "user", "content": "cordboard-qualify-74 readiness probe"}]}`.

Results:

- **Execution -- passed.** `cord diagnose` reported
  `execution: {required: true, state: "ready", status: "reachable", graphs:
  [core_notification_fixture, issue_resolution, readiness]}`. `cord run`
  submitted one explicit Run with a fresh Subject and a fresh thread
  (`thread_id=3ea0c621-b0b1-4149-9db6-8e0683891b38`,
  `run_id=f76b9cbf-31a4-4694-b126-4f7e8f90cf38`), which completed
  `status: success` with the entity's own response content ("Omiologic Aegra
  server is ready."). `.cordboard/run_continuity.json` recorded the logical
  thread's `api_run_ids` mapping, confirming Cordboard's own identity
  persistence engaged against a real backend, not a fixture.
- **Topology -- not supplied.** `GET /.well-known/agent-topology.manifest.json`
  returns 404 on this revision; `cord diagnose` reports
  `topology: {state: "not_configured", status: "absent"}`. This is not a
  Cordboard defect: ARCHITECTURE.md's internal dependency review already
  recorded that "Omiologic's topology command generates files rather than
  serving the proposed discovery endpoint." No topology evidence is shown,
  matching this issue's "shown only when the entity actually supplies them."
- **Telemetry (archive/collector) -- not configured.** No OTLP archive path
  is known for this connection, so `cord diagnose --archive` was not run
  against a real file; `telemetry.archive` reports `not_configured` rather
  than a fabricated pass. `telemetry.collector` reports `unsupported`, the
  same pre-existing Cordboard-side gap #71/#72 already recorded (ADR-0014;
  `connections.py` has no per-connection Collector health field yet) -- not
  new to this Feature.
- **Approval/authorization -- unqualified.** `readiness` has no interrupt
  seam, so no `auth_endpoint` was registered and no approval was exercised.
  `approval_authorization` reports `not_configured`. Omiologic's
  `issue_resolution` graph does carry a real human-approval interrupt
  (per [internal-dependencies.md](internal-dependencies.md)), but qualifying
  it is out of scope here for the reasons above; it is a candidate for a
  later Feature, not a gap in this one.
- **Lifecycle -- external.** `managed_lifecycle` reports
  `state: "not_configured", status: "external"`: no `--launch` was passed,
  so Cordboard never started or could stop this process, matching ADR-0016.

## Acceptance criteria

- [x] The selected entity and exact revision/artifact are named before
      implementation begins -- Omiologic, `6dcf2e5…`, above.
- [x] Cordboard connects and submits one explicit Run through the public
      backend with fresh identities -- `readiness`, fresh thread/run IDs,
      `status: success`.
- [x] Published topology and redacted execution evidence are shown only when
      the entity actually supplies them -- neither is supplied at this
      revision; neither is shown or fabricated.
- [x] Any real interrupt uses the entity-owned authorization boundary and
      resumes the correct logical Run without duplicate effect -- no real
      interrupt exists on the selected graph, so this is recorded as
      unqualified rather than exercised.
- [x] No Cordboard source branch, persisted field, or configuration
      describes that entity's graph business logic -- no `cord_runtime`
      source changed; the connection record holds only an alias and an
      endpoint URL.
- [x] Missing or failed capabilities are recorded against their owning
      contract and are not relabeled as passes -- topology and telemetry are
      recorded as not supplied/not configured above, not as passes.

## Handoff

A later Feature that wants to qualify the topology or approval/authorization
contracts against Omiologic needs two things this pilot deliberately did not
force: the entity serving its generated topology at the well-known discovery
path, and a graph with both a real interrupt and a non-no-op observer
(`issue_resolution`, once its no-op observer is replaced) -- both are
entity-owned gaps already tracked in
[internal-dependencies.md](internal-dependencies.md) and
[cordboard-upstream-requirements.md](decisions/cordboard-upstream-requirements.md),
not new findings from this Feature and not a reason to add graph-specific
code to Cordboard.
