# Internal dependency review — 2026-09-15

This is a source and lockfile review of the six local checkouts, not a claim
that they have passed Cordboard integration. Commit links below identify the
inspected source. Release information comes from checked-in release records and
local tags; registries and remote branch tips were not re-queried. No dependency
pins, upstream source, services, or external business state were changed.

## Source snapshot

| Repository | Inspected HEAD | Release/source distinction |
| --- | --- | --- |
| [agent-topology](https://github.com/agent-topology/agent-topology) | `6fff9a6bfbc8eba05b5abeed59aeccf272dfa4b1` | Published baseline beta.3, tag commit `848b179aee32789a6f8b0ad4552a3a1262a05d55`; beta.4 artifacts qualified at `05486d7ee9cee7650779cb0e9c00a85bd152a3e8`, publication still pending in the inspected records |
| [redact-secret](https://github.com/redact-secret/redact-secret) | `6449f78eb340e94d21410fee3f4fd275853e8e1d` | Latest local tag beta.2 at `8cdc1b118449a15be545ecf70bb7f0df53f6126e`; HEAD includes additional unreleased detector fixes |
| [agent-workflow-core](https://github.com/milocosmopolitan/agent-workflow-core) | `876f6a8a4ba863424e1f85bc92f18dce59147957` | Package version `0.1.0b3`; tag `v0.1.0.beta.3` points to older `94cd31f58d8e94d55ea2d952c6db5508539ea53f`. Notifications are in the later pinned source, not that release tag |
| [campaign-agent](https://github.com/milocosmopolitan/campaign-agent) | `9112b72e0f78dffe6a49125318fb1d0d96a01303` | No local release tag; manifest version `0.1.0`; core pinned to `876f6a8…` |
| [git-agent](https://github.com/milocosmopolitan/git-agent) | `b67cb35a6195609a5d89976768fb5a4884f17ca1` | No local release tag; core pinned to `876f6a8…` |
| [Omiologic](https://github.com/milocosmopolitan/omiologic-aegra) | `6dcf2e56ca3c015961c4a226e2025c178ec2d5b8` | No local release tag; Aegra 0.10.4, LangGraph 1.2.11, topology beta.3, redactor beta.2; editable sibling `git-agent` and core pinned to `876f6a8…` |

The git-agent README and Omiologic README/operator runbook had local edits;
Omiologic also had an untracked `.env.example`. Those changes were preserved and
are not attributed to the commit links. Findings below use committed source,
configuration, and test definitions. The campaign README's “production workflows
remain defined” description lags its implemented factories and lifecycle tests.

## What now exists and what Cordboard consumes

| Component | Implemented upstream surface | Cordboard implication |
| --- | --- | --- |
| agent-topology | Public spec/producer APIs; beta.4 candidate preserves expanded parents and materializes child graphs | Consume documents through the public spec. AT-1 is addressed in candidate source, not in the published beta.3 baseline |
| redact-secret | Shared Rust detector/policy engine and language bindings; beta.2 expands incremental support and qualification | Cordboard still pins Python/CLI beta.1. A newer entity redactor is not evidence that Cordboard upgraded or requalified it |
| agent-workflow-core | Runtime-neutral model profiles, validation/routing, effect/approval contracts, observer protocols, events and notifications; optional LangGraph adapters | These belong inside graph deployments. Their presence does not require a Cordboard dependency or model configuration |
| campaign-agent | Compiled graph factories and local lifecycle composition for campaign contract, channel concept/production, stakeholder review and deliverable report, with copy subworkflows | A substantial second domain implementation exists. It needs an entity's resources and API exposure; it is not itself an addressable Deployment |
| git-agent | `create_graph(graph_id="issue_resolution", policy=...)`, typed invocation context, verification, model nodes, human approval and ordered Git/GitHub effects | Consume the hosting entity's API. Do not import its policy, workspace adapters or business state into Cordboard |
| Omiologic | Aegra registration, request-scoped context assembly, concrete Git/GitHub/model adapters, redacted process egress, topology generation, notification persistence and Slack approval ingress | Existing Deployment implementation to integrate with. Execution, discovery and Cordboard recording have separate remaining checks |

Sources: [core architecture](https://github.com/milocosmopolitan/agent-workflow-core/blob/876f6a8a4ba863424e1f85bc92f18dce59147957/ARCHITECTURE.md),
[git-agent factory](https://github.com/milocosmopolitan/git-agent/blob/b67cb35a6195609a5d89976768fb5a4884f17ca1/src/git_agent/graphs/issue_resolution/graph.py#L114-L256),
[campaign factory](https://github.com/milocosmopolitan/campaign-agent/blob/9112b72e0f78dffe6a49125318fb1d0d96a01303/src/campaign_agent/campaign_contract/graph.py#L662-L754),
[campaign lifecycle fixture](https://github.com/milocosmopolitan/campaign-agent/blob/9112b72e0f78dffe6a49125318fb1d0d96a01303/tests/i14_entity_lifecycle/test_entity_lifecycle.py),
[Omiologic architecture](https://github.com/milocosmopolitan/omiologic-aegra/blob/6dcf2e56ca3c015961c4a226e2025c178ec2d5b8/ARCHITECTURE.md),
and [entity dependency pins](https://github.com/milocosmopolitan/omiologic-aegra/blob/6dcf2e56ca3c015961c4a226e2025c178ec2d5b8/pyproject.toml).

## Topology: beta.3 baseline versus beta.4 candidate

The [candidate release record](https://github.com/agent-topology/agent-topology/blob/6fff9a6bfbc8eba05b5abeed59aeccf272dfa4b1/docs/releases/v0.1.0-beta.4.md)
records package/artifact qualification with `publish=false`. It does not establish
registry availability. Python's supported LangGraph range remains 1.2.10–1.2.11;
LangGraph.js remains 1.4.14.

For positive depth, candidate producers retain the parent's node ID and use
`subgraphId` to reference a separate `graphs[]` entry. Follow those references;
do not split colon-delimited IDs to reconstruct parentage. A child-ID collision
keeps the child opaque and adds `child-graph-id-collision`. The old
`expanded-subgraph-metadata` gap is retired in candidate output. Depth 0 remains
unchanged; positive-depth structure hashes change and must be recomputed.

`x-topology-interpretation` revision `"2"` describes materialized children;
revision `"1"` remains for graphs without materialization. Unsupported revisions
must remain opaque. Structure hashes do not authenticate interpretation metadata;
cache interpretation-derived facts with the revision as well. Dynamic interrupts,
approval validity, policy drift and effect receipts remain runtime evidence.
Factory-owned graphs need an import-safe compiled-object export for `agt`; the
CLI does not call factories. Candidate `--depth N` exposes the existing API option.
See the [upgrade guide](https://github.com/agent-topology/agent-topology/blob/6fff9a6bfbc8eba05b5abeed59aeccf272dfa4b1/docs/guides/upgrading-beta.4.md).

## Redaction versions are independent deployment facts

Cordboard's [archive fixture](archive.md) still uses `redact-secret==0.1.0b1`
and the matching CLI. Omiologic uses `0.1.0b2`. The upstream
[changelog](https://github.com/redact-secret/redact-secret/blob/6449f78eb340e94d21410fee3f4fd275853e8e1d/CHANGELOG.md)
records beta.2's cross-surface incremental support, terminal invalid-input handling
and assessment tooling. HEAD additionally fixes multiline contextual assignments
and placeholder false positives; these are explicitly unreleased.

The public whole-input `scan_and_redact`/finding-action boundary remains the basis
of Cordboard's exporter. `block` must suppress the span even if replacement text
exists. Neither upstream assessment nor Omiologic's installation replaces a
Cordboard Python/CLI/Collector regression when its pin is changed. No performance
or detection-accuracy result was measured in this review.

## Actual integration gaps

1. **The API target is the entity, not the library.** Omiologic's
   [registration](https://github.com/milocosmopolitan/omiologic-aegra/blob/6dcf2e56ca3c015961c4a226e2025c178ec2d5b8/aegra.json)
   contains `readiness`, `issue_resolution`, and `core_notification_fixture`.
   Campaign graphs are not registered there. The entity owns resources,
   credentials, approval authority and checkpointing; Cordboard carries API
   requests. Its [context bridge](https://github.com/milocosmopolitan/omiologic-aegra/blob/6dcf2e56ca3c015961c4a226e2025c178ec2d5b8/src/omiologic/context_bridge.py#L104-L135)
   is already implemented and must not be rebuilt in Cordboard.
2. **A generated file is not HTTP discovery.**
   [Topology generation](https://github.com/milocosmopolitan/omiologic-aegra/blob/6dcf2e56ca3c015961c4a226e2025c178ec2d5b8/src/omiologic/topology.py#L66-L157)
   uses the explicit registration list and writes `dist/topology/<graph-id>.json`,
   with document-local ID `main`. This does not implement the proposed
   `/.well-known/agent-topology.manifest.json` endpoint or an Agent Card.
   Keep the Deployment/Graph association outside the unmodified JSON. A missing
   published document remains a viewer state, not a connection failure (ADR-0015).
3. **A redacted exporter is not execution instrumentation.** Omiologic's
   [production context](https://github.com/milocosmopolitan/omiologic-aegra/blob/6dcf2e56ca3c015961c4a226e2025c178ec2d5b8/src/omiologic/graphs/issue_resolution.py#L189-L246)
   supplies `NoOpRunObserver`. Core observer callbacks and `EventPayload` are not
   automatically Cordboard Run/Step/Attempt spans. A producer-side observer adapter,
   identity/Outcome mapping and end-to-end archive evidence are still needed.
   The [public observer protocols](https://github.com/milocosmopolitan/agent-workflow-core/blob/876f6a8a4ba863424e1f85bc92f18dce59147957/src/agent_workflow_core/observers.py#L218-L256)
   report outcomes without explicit start/end timestamps or a Step handle on
   `attempt()`. A public lifecycle/context seam must establish real timing and
   parentage; outcome events alone must not be fabricated into duration spans.
   Core named-profile escalation must be explicitly declared, never inferred
   from a model or profile string (ADR-0013).
4. **Existing resume support does not complete Cordboard's resume semantics.**
   Graph/core approval validation and Omiologic's verified Slack resume path exist.
   Cordboard still needs its own inbox/expiry integration and logical Run continuity
   across Aegra API Run IDs. Core notification storage and Slack delivery are not
   the planned Cordboard Signal router or its deduplication contract.
5. **Implementation evidence is bounded.** Campaign's lifecycle fixture uses local
   adapters and fake models/decisions/notification targets, with no live browser,
   Slack or ESP. It records a repeated artifact-commit revision-identity gap that
   prevents treating the full revise chain as production-qualified. Omiologic's
   inspected production context still supplies `verification_command=("true",)`
   and a one-second timeout; operators need meaningful entity-owned verification
   before real issue work. These are graph/entity concerns, not new Cordboard
   configuration fields.

## Issue ownership after reconciliation

The 2026-09-15 tracker review updates the existing issues rather than making each
dependency observation a new prerequisite. These are implementation scopes, not
newly completed features.

| Work | Existing owner | Readiness |
| --- | --- | --- |
| Connect and run existing deployments | [#12](https://github.com/omiologic/cordboard-proto/issues/12) | Ready; first implementation. No topology, new template or provider prerequisite |
| Read optional topology | [#11](https://github.com/omiologic/cordboard-proto/issues/11) | Ready independently; initial released beta.3 spec baseline |
| Producer instrumentation and pause/resume Step boundaries | [#28](https://github.com/omiologic/cordboard-proto/issues/28) | Refine the public lifecycle seam; does not block API connection |
| Generic recorded viewer | [#13](https://github.com/omiologic/cordboard-proto/issues/13) | After #11/#12; existing archived fixtures support UI development |
| Authorized inbox and logical/API Run continuity | [#14](https://github.com/omiologic/cordboard-proto/issues/14) | After #13/#28; entity validation remains authoritative |
| Expiry, routing, deduplication, cascades, isolation and live UI | [#15–#20](https://github.com/omiologic/cordboard-proto/issues/3) and [#15](https://github.com/omiologic/cordboard-proto/issues/15) | Refine against actual prerequisite interfaces; no all-features serial gate |

The #1 foundation Epic and Slice 0/0.5 milestones close against the existing
boundary and Langfuse verification records. This reconciliation runs no new
integration tests. #28 moves into Slice 1 as the owner of actual producer recording;
#14 retains Aegra multi-call identity mapping. DeepAgents scaffolding, beta.4
publication and a redactor upgrade are not gates for starting Cordboard.

## Verification scope

This review read source spans, registration/configuration, lockfiles, local tag
targets, upstream qualification records and test definitions. It did not run the
six projects' test suites, publish artifacts, start Aegra, call a provider, send
notifications, or execute Git/GitHub effects. Upstream recorded checks are cited
as upstream evidence, not newly passed checks. Cordboard documentation references
and whitespace are checked separately; existing Slice 0/0.5 evidence remains in
its original guides.
