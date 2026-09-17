# Playground launch contract (#66, ADR-0020)

> **Status: planned.** ADR-0020 fixes the command sequence, artifact
> boundary, and ownership below. None of it is implemented yet:
> `scripts/run-playground` and the standalone idle-only publisher module are
> [#67](https://github.com/agent-topology/cordboard/issues/67)'s scope, in the
> `cordboard-testbed` repository, not this one. This document records the
> decision so #67 and #68 do not have to re-derive it.

## Why this is not `cord serve` alone

`cord serve` (ADR-0018 §7) only renders whatever is already registered and
recorded. Reaching a *populated* viewer — idle topology, a successful Run, a
failed-then-retried Run, and a Run paused on approval — needs a real Aegra
deployment with Postgres checkpoints, because Aegra is Cordboard's only
`ExecutionBackend`. ADR-0016 already forbids Cordboard from owning that real
process; `cordboard-testbed`'s `environments/minimal/` already assembles it.
This contract is the thin, human-facing composition of what already exists —
not a new backend and not a new Cordboard subcommand.

## Command sequence

```sh
# 1. Install the candidate artifact (existing cordboard-testbed contract)
python3 scripts/install-wheel /absolute/path/cord_runtime-<version>-py3-none-any.whl \
  --sha256 <64-character-build-sha256>
.venv/bin/python scripts/prepare-tools

# 2. Launch the playground (new; cordboard-testbed, #67)
.venv/bin/python scripts/run-playground --evidence .artifacts/playground
```

`run-playground` prints the same "Minimal environment ready" line
`start-environment` already prints, then the `cord serve` URL line
(`Cordboard: http://127.0.0.1:<port>/`) once the four scenarios below are
seeded. Ctrl-C stops the server and, immediately after, everything
`start-environment` already stops (owned processes, then
`docker compose stop`; the Postgres checkpoint volume is retained, matching
existing acceptance behavior). Docker and the two pinned public binaries
(`otelcol-contrib`, `redact-secret`) are visible prerequisites, not something
`run-playground` downloads or hides.

## Artifact boundary

| Artifact | Lives in | Owns |
| --- | --- | --- |
| `cord-runtime` wheel | `cordboard` release or explicit commit build | CLI, viewer, backend client, public contracts |
| Testbed checkout + pinned binaries + Docker | `cordboard-testbed` | Real Aegra, Postgres, Collector processes, fixture graphs |

## Ownership table

| Concern | Owner |
| --- | --- |
| Aegra process, Postgres (Docker Compose), Collector process | `run-playground` (Testbed), reusing `scripts/start-environment` unchanged |
| `deterministic`/`interrupt` fixture graph content | `environments/minimal/graphs/` (Testbed) — a synthetic entity's own graph, per ADR-0016 §4 |
| Idle-only topology publisher | `run-playground` (Testbed), extracted from `scripts/verify-browser-artifact.py`'s `IdlePublisher` into a standalone module |
| `--board` directory | Created fresh per invocation under `--evidence`; format owned by `cord_runtime.connections`, location/lifetime owned by Testbed |
| `--archive` directory | Fixed path under `--evidence` (e.g. `.artifacts/playground/archive`), reused across reruns, append-only |
| Cleanup on Ctrl-C | Inherited from `start-environment`'s existing signal handling; no new cleanup logic |

## The four scenarios

| Scenario | How it is produced | What the viewer shows |
| --- | --- | --- |
| Idle topology | Two connection aliases advertising the same `graph_id` with no execution | Two independent `/connections/{alias}/graphs/{graph_id}` pages, each "No recorded Runs", same published topology |
| Successful execution | The deterministic fixture graph run with an input interpreted as immediate success | One Step, one Attempt, outcome `passed` |
| Failed-then-passed retry | The same graph run with an input interpreted as one failure then a retry | One Step, two Attempts: `["failed", "passed"]` |
| Waiting approval | The interrupt fixture graph run and left paused (no response submitted) | Live diagnostics show "Live identity and status only." and `ingestion_pending` — this fixture emits no telemetry, so no completed archive record is ever produced |

No exact input payloads or node names are prescribed here — those belong to
the fixture graphs already checked into `cordboard-testbed`
(`environments/minimal/graphs/deterministic.py`, `interrupt.py`), which are
graph-owned per `AGENTS.md`'s no-descriptors rule.

## Rerun semantics

Rerunning `run-playground` does not require inventing a new path each time:
session metadata (`session.json`, candidate identity) still gets a fresh
sub-directory per invocation, but the `--archive` directory itself is a fixed,
reused, append-only location — consistent with ADR-0005's archive semantics.
The Postgres checkpoint volume is likewise retained across restarts by
`start-environment`, so prior Threads are not lost.

## Failure modes

Occupied ports (55432/52026/54318), an already-running Testbed Compose
session, and missing Docker or pinned binaries are all already reported with
a bounded, non-destructive failure by `start-environment`; `run-playground`
does not add new failure classification on top of it. `cord serve` failures
(invalid host/port) use ADR-0018 §7's existing validation unchanged.

## What this document is not

It is not implementation evidence. See ADR-0020 for the full rationale,
rejected alternatives, and trade-off analysis. See
[#67](https://github.com/agent-topology/cordboard/issues/67) for the
implementation Task and [#68](https://github.com/agent-topology/cordboard/issues/68)
for the black-box qualification that replays this exact command sequence
headlessly.
