---
name: track-upstream
description: Check the upstream repositories Cordboard depends on for new releases and commits, decide what Cordboard must adopt, and open Cordboard issues for it. Use when asked to track, check, or sync upstream changes ("track-upstream", "/track-upstream", "what changed upstream"). Optional argument narrows to one upstream.
---

# track-upstream

## Upstreams

| Repo | Baseline |
| --- | --- |
| `agent-topology/agent-topology` | `pyproject.toml` pins (`agent-topology-spec`, `agent-topology-langgraph`) |
| `redact-secret/redact-secret` | `pyproject.toml` pin (`redact-secret`) |
| `milocosmopolitan/agent-workflow-core` | inspected SHA in `docs/internal-dependencies.md` |
| `milocosmopolitan/git-agent` | inspected SHA in `docs/internal-dependencies.md` |
| `milocosmopolitan/campaign-agent` | inspected SHA in `docs/internal-dependencies.md` |

## 1. Collect changes since the baseline

```bash
rtk gh release list -R <repo> --limit 5
rtk gh api repos/<repo>/compare/<baseline>...HEAD --jq '.commits[] | .sha[0:7] + " " + (.commit.message | split("\n")[0])'
```

Read release notes and changed public APIs, not just commit titles. A `Draft`
release is not published; do not treat it as adoptable.

## 2. Keep only what Cordboard must act on

Act on: new releases of pinned packages, public API or format changes, fixes
for items in `docs/decisions/cordboard-upstream-requirements.md`, and
deprecations. Skip internal refactors and graph business logic — graph
behavior stays inside the graph (ADR-0014).

## 3. Deduplicate

```bash
rtk gh issue list --state all --search "<repo name> <version or topic>"
```

## 4. Draft, confirm, create

Draft each issue in English per `docs/issue-planning.md` (Outcome, Context and
decisions, Dependencies and readiness, Scope, Out of scope, Acceptance). Cite
the upstream release/commit/PR links. Show the drafts and create only after the
user approves:

```bash
gh issue create --title "<title>" --body-file <file> --label planning:backlog
```

## 5. Report

List per upstream: baseline → latest, issues opened, and changes skipped with
the reason. Do not edit pins or docs — adoption happens in the opened issue.
