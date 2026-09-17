# Slice 6–9 operator-readiness roadmap

Created 2026-09-17 from the implemented Slice 1–5 and Testbed evidence. This is
a directional Project roadmap, not evidence that the outcomes below already
exist. GitHub milestones and issues own delivery planning; accepted ADRs and
[Architecture](../ARCHITECTURE.md) continue to own system boundaries.

## Roadmap record

```yaml
schema_version: 1
roadmap_id: cordboard-operator-ready
title: Cordboard from validated control plane to usable local operator tool
scope:
  type: project
  id: cordboard
end_state: >-
  A first-time local operator can launch a safe populated environment,
  understand how different graph executions appear, and connect an
  independently owned graph without platform code changes or knowledge of the
  acceptance harness.
items:
  - roadmap_item_id: playable-first-run
    roadmap_id: cordboard-operator-ready
    outcome: >-
      A first-time operator reaches a populated browser viewer through one
      bounded, documented local path using a released or explicitly built
      artifact and no provider credentials.
    measure:
      indicator: Fresh-environment first-run path
      baseline: >-
        The proven path requires a wheel build, external Testbed preparation,
        several services, and manually assembled board and archive inputs.
      target: >-
        The supported path exposes deterministic idle topology, success,
        retry, and approval examples without repository-internal knowledge.
    horizon: NOW
    order: 1
    depends_on: []
    risks:
      - statement: >-
          Turning the acceptance harness into the product experience could
          expose Testbed complexity.
        response: >-
          Keep black-box acceptance separate while presenting a purpose-built
          operator entry path over the same public contracts.

  - roadmap_item_id: explain-runs-visually
    roadmap_id: cordboard-operator-ready
    outcome: >-
      An operator can tell which graph is selected, what path a Run took,
      where it retried or paused, and what action or diagnosis is available
      directly from the viewer.
    measure:
      indicator: Scenario comprehension from the browser surface
      baseline: >-
        The viewer renders topology and execution evidence accurately, but
        exposes contract vocabulary, identifiers, empty states, and separate
        controls with little in-product explanation.
      target: >-
        Idle, success, retry, interrupt, unavailable, ambiguous, and ingestion
        scenarios are distinguishable without architecture-document lookup.
    horizon: NOW
    order: 2
    depends_on:
      - playable-first-run
    risks:
      - statement: >-
          A simplified explanation could hide uncertainty or imply graph state
          Cordboard does not own.
        response: >-
          Explain only published topology, runtime identity, spans, and bounded
          diagnostics already available through public contracts.

  - roadmap_item_id: bring-your-own-graph
    roadmap_id: cordboard-operator-ready
    outcome: >-
      An operator can connect an existing independent Deployment and receive
      actionable, connection-scoped guidance for execution, topology,
      telemetry, and approval readiness.
    measure:
      indicator: Independent Deployment onboarding
      baseline: >-
        The CLI primitives exist, but the operator must compose registration,
        topology synchronization, execution, archive, viewer, authorization,
        and instrumentation guidance manually.
      target: >-
        A supported onboarding path either reaches an observable Run or names
        the missing public contract without platform code changes.
    horizon: NEXT
    order: 3
    depends_on:
      - playable-first-run
      - explain-runs-visually
    risks:
      - statement: >-
          Onboarding could drift into per-graph configuration or topology
          ownership.
        response: >-
          Keep all platform settings connection-scoped and consume graph-owned
          manifests, APIs, and spans unchanged.

  - roadmap_item_id: real-entity-pilot
    roadmap_id: cordboard-operator-ready
    outcome: >-
      At least one independently maintained, non-synthetic entity is routinely
      run, approved when applicable, and inspected through Cordboard using only
      public integration contracts.
    measure:
      indicator: Repeatable non-synthetic operator session
      baseline: >-
        The full matrix is proven with synthetic entities; current internal
        entities retain known topology-serving and observer integration gaps.
      target: >-
        A recorded end-to-end session repeats after reinstall or restart
        without Cordboard importing entity business logic.
    horizon: NEXT
    order: 4
    depends_on:
      - bring-your-own-graph
    risks:
      - statement: >-
          Private entity specifics could leak into generic platform behavior.
        response: >-
          Treat the pilot as contract qualification and push missing producer
          capabilities upstream instead of adding entity-specific branches.

  - roadmap_item_id: daily-operator-loop
    roadmap_id: cordboard-operator-ready
    outcome: >-
      A local operator can return to a board, understand current delivery and
      approval health, and recover from common connection or ingestion failures
      without Testbed knowledge.
    horizon: LATER
    order: 5
    depends_on:
      - real-entity-pilot
    risks:
      - statement: >-
          Operational convenience could expand Cordboard into a deployment
          system.
        response: >-
          Retain entity-owned runtime provisioning and limit Cordboard to
          connection lifecycle, observation, routing, and approval coordination.

  - roadmap_item_id: graph-author-starter
    roadmap_id: cordboard-operator-ready
    outcome: >-
      A graph author can start from an optional supported integration starter
      that publishes topology and redacted execution evidence while retaining
      graph-owned business logic and runtime ownership.
    horizon: EXPLORE
    order: 6
    depends_on:
      - bring-your-own-graph
    risks:
      - statement: >-
          Scaffolding could be mistaken for a required Cordboard graph format.
        response: >-
          Keep it optional and generated around upstream public contracts, with
          no per-graph Cordboard descriptor.
```

## Delivery mapping

The roadmap items remain outcomes. The following GitHub records own the
delivery work and may be refined as predecessor evidence lands.

- Epic [#64](https://github.com/agent-topology/cordboard/issues/64) spans all
  four delivery slices.
- [Slice 6 — Reach a populated viewer from a clean environment](https://github.com/agent-topology/cordboard/milestone/11)
  advances `playable-first-run` through [#65](https://github.com/agent-topology/cordboard/issues/65)
  and [#69](https://github.com/agent-topology/cordboard/issues/69).
  #65 has Tasks [#66](https://github.com/agent-topology/cordboard/issues/66),
  [#67](https://github.com/agent-topology/cordboard/issues/67), and
  [#68](https://github.com/agent-topology/cordboard/issues/68).
- [Slice 7 — Explain graph execution visually](https://github.com/agent-topology/cordboard/milestone/9)
  advances `explain-runs-visually` through
  [#70](https://github.com/agent-topology/cordboard/issues/70) and
  [#71](https://github.com/agent-topology/cordboard/issues/71).
- [Slice 8 — Bring an independent graph onto the board](https://github.com/agent-topology/cordboard/milestone/10)
  advances `bring-your-own-graph` through
  [#72](https://github.com/agent-topology/cordboard/issues/72) and
  [#73](https://github.com/agent-topology/cordboard/issues/73).
- [Slice 9 — Operate a maintained entity through public contracts](https://github.com/agent-topology/cordboard/milestone/12)
  advances `real-entity-pilot` and `daily-operator-loop` through
  [#74](https://github.com/agent-topology/cordboard/issues/74) and
  [#75](https://github.com/agent-topology/cordboard/issues/75).
- `graph-author-starter` remains an uncommitted `EXPLORE` possibility. It has
  no milestone or implementation issue.

Only #66 is initially `planning:ready`. #67–#75 remain
`planning:backlog`; each must absorb its predecessor's actual paths, commands,
contracts, and evidence before promotion. #65 is a Feature with child Tasks and
therefore carries no readiness label. Work on one implementation issue at a
time, as required by [Issue planning](issue-planning.md).

## Ordering rationale

The first two outcomes are `NOW` because the implementation and external
Testbed evidence already make a safe playground and viewer-comprehension work
credible. Independent graph onboarding is `NEXT`: its exact workflow should
reuse the vocabulary and failure guidance learned from the playground. A real
entity pilot follows the generic onboarding path so entity-specific gaps do not
become platform policy. Recurring operation remains `LATER` until the pilot
reveals the smallest real recovery matrix. Optional graph-author scaffolding is
retained under `EXPLORE` rather than being promoted into `cord new` or `cord up`
before onboarding evidence exists.

## Evidence and review

Current-state evidence comes from [Architecture](../ARCHITECTURE.md), the
[Slice 1–5 retrospective](slice1-5-retrospective.md), the
[browser viewer guide](browser-viewer.md), and the external
[`cordboard-testbed`](https://github.com/agent-topology/cordboard-testbed)
artifact qualification. Closed issues and historical checks are inputs to this
direction; they are not evidence that these new outcomes are complete.

Review this roadmap at each milestone close. Reordering or horizon changes must
retain the same roadmap item identity and record the evidence or changed
assumption that justifies the change.
