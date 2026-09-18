---
name: request-upstream
description: Open a feature request or bug report in an upstream repository when Cordboard development needs something the upstream owns. Use when asked to request, file, or raise something upstream ("request-upstream", "/request-upstream <repo> <need>", "file this with agent-topology").
---

# request-upstream

Upstreams: `agent-topology/agent-topology`, `redact-secret/redact-secret`,
`milocosmopolitan/agent-workflow-core`, `milocosmopolitan/git-agent`,
`milocosmopolitan/campaign-agent`.

## 1. Confirm it belongs upstream

Request it upstream instead of working around it in Cordboard: formats and
detectors belong to `agent-topology` and `redact-secret`, graph behavior belongs
to the graph repo. If the need is really a Cordboard connection concern,
say so and stop (ADR-0014).

## 2. Deduplicate

```bash
rtk gh issue list -R <repo> --state all --search "<topic>"
```

If one exists, propose a comment with Cordboard's case instead of a new issue.

## 3. Draft

English, short, self-contained:

```markdown
## Need
What Cordboard needs and why, from the consumer side.

## Current behavior
What happens today, with version/commit and a minimal repro.

## Proposed behavior
The public API or format change requested. No Cordboard internals.

## Consumer context
Link the Cordboard issue/PR/doc that is blocked or working around this.
```

## 4. Confirm, create, record

Show the draft and create only after the user approves:

```bash
gh issue create -R <repo> --title "<title>" --body-file <file>
```

Then link the upstream issue from the Cordboard issue or doc that needed it.
For `agent-topology` and `redact-secret`, add it to
`docs/decisions/cordboard-upstream-requirements.md`.
