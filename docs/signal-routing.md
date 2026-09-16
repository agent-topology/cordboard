# Signal/Rule routing: manual, file, and schedule (#16)

`cord rule add` and `cord signal manual|file|schedule` route a Signal to an
already-registered connection/Assistant through one generic path
([ADR-0002](decisions/0002-vocabulary.md) Signal/Rule vocabulary,
[ADR-0013](decisions/0013-switchboard-boundary.md) switchboard boundary).
Cordboard evaluates only a Rule's declared match/mapping expressions; it never
interprets Signal payload meaning, prompts, model capabilities, or provider
metadata to choose or substitute a target. Adding a Rule for a Signal type
already in use is a configuration change to `.cordboard/rules.json`, not a
change to `cord_runtime.router`.

## Signal

A Signal is an external event ([ADR-0002](decisions/0002-vocabulary.md)):
`{"type": "manual" | "file" | "schedule", "id": "<deterministic string>",
"payload": {...}}`. Identity is deterministic per source, which is what lets
`cord_runtime.signal_dedupe` (#17) key its durable claim on this same id
without changing this contract:

| Source | Id | Payload |
| --- | --- | --- |
| `manual` | explicit `--id`, or `manual:<timestamp>` (ADR-0003 convention) | the given JSON file's object |
| `file` | `file:<path>:<content hash>` — same content, same id | the file's JSON object |
| `schedule` | `schedule:<name>:<ISO 8601 tick>` | `{"schedule": name, "at": tick, ...extra}` |

## Rule

A Rule is a Signal -> Assistant transformation declaration
([ADR-0002](decisions/0002-vocabulary.md)), stored per board (default: the
current directory) at `.cordboard/rules.json` as a JSON array:

```json
[
  {
    "name": "issue-triage",
    "signal_type": "file",
    "match": {"issue.repo": "cordboard-proto"},
    "connection": "aegra-local",
    "assistant": "triage-graph",
    "subject": "issue.url",
    "input": {"title": "issue.title", "body": "issue.body"}
  }
]
```

- `signal_type`: one of `manual`, `file`, `schedule`.
- `match` (optional): a bounded equality filter — every declared dot-path into
  the Signal payload must equal the given literal for the Rule to apply.
  No expression language, no code execution.
- `connection`/`assistant`: an already-registered `cord add` alias and an
  explicit assistant/graph id (#12). The rule-selected target must already be
  a connection Cordboard knows about; no model or provider declaration is
  required or read here — a model-free graph is a Rule target like any other
  ([ADR-0013](decisions/0013-switchboard-boundary.md)).
- `subject` (required): a bounded dot-path into the payload that must resolve
  to a non-empty string, transported as the Run's required Subject
  ([ADR-0003](decisions/0003-subject-not-thread.md)).
- `input` (optional): a bounded map of destination key -> dot-path into the
  payload, assembled into the Assistant's JSON input. Missing paths fail the
  Rule rather than silently omitting the key.

Add or replace one with:

```sh
$ cord rule add issue-triage file aegra-local triage-graph issue.url \
    --match issue.repo=cordboard-proto \
    --input title=issue.title --input body=issue.body
added rule 'issue-triage'
```

`--match KEY=VALUE` parses `VALUE` as JSON when possible (numbers, booleans,
`null`) and falls back to a literal string otherwise. `--replace` overwrites
an existing Rule of the same name; without it, a duplicate name is rejected.

## Firing a Signal

```sh
$ cord signal manual event.json
$ cord signal file path/to/event.json
$ cord signal schedule daily-digest 2026-09-16T09:00:00+00:00
```

Each prints one JSON line and routes through `cord_runtime.router.route_signal`:

- **`routed`** — a Rule matched; `result` carries the same `execute()` outcome
  as `cord run` (`run_id`/`thread_id`/`status`/`values`). Exit `0` only when
  the nested Run also reached `status: "success"`.
- **`unmatched`** — no declared Rule applies to this Signal.
- **`invalid_mapping`** — a Rule matched, but its declared `subject`/`input`
  path was missing from the payload, or its `connection` alias is unknown.
- **`concurrency_skipped`** — a Rule matched and mapped, but another Run is
  already in flight for the same declared (Assistant, Subject) pair; the
  declared default is to skip, not queue or run anyway (#17).
- **`deployment_unavailable`** — a Rule's `connection` declares a managed
  `launch` command (below) that failed to start or become healthy; `error`
  carries the failure. Nothing was submitted, so this Signal id stays
  retryable.
- **`duplicate`** — this Signal id was already claimed and submitted within
  the retention window (default 24h); a Run was not resubmitted.

`unmatched` and `invalid_mapping` both exit `1` and append one bounded,
sanitized record — `signal_type`, `signal_id`, and a `reason` string only,
**never the Signal payload** — to `.cordboard/unmatched.json`, so a
misconfigured or unrouted Signal stays visible without a second telemetry
path or a leaked secret.

## Deduplication, concurrency, and managed Deployment startup (#17)

Immediately before submitting a Run, `route_signal` applies three durable,
restart-safe checks, each backed by its own file under `.cordboard/` so they
survive a process restart between a claim and its outcome:

1. **Concurrency** (`cord_runtime.concurrency`): a claim on the declared
   (Assistant, Subject) pair. The only implemented policy is "skip" — a
   second arrival for a pair still claimed is turned away, not queued. A
   claim older than the execution timeout plus a startup/health budget is
   treated as abandoned (a crashed holder) and reclaimed.
2. **Managed Deployment startup** (`cord_runtime.deployment_lifecycle`): a
   connection registered with `cord add --launch "<command>" [--idle-after
   SECONDS]` is managed; Cordboard starts that Deployment's own existing
   entrypoint if it is not already running and polls its public `/health`
   before submitting. A connection registered without `--launch` is
   external and is never started or stopped, only ever contacted, exactly
   as before. `cord deployment sweep` stops every managed Deployment that is
   idle (no active claim) beyond its declared `idle_after` (default 600s);
   it is meant to be invoked by an operator's own cron, the same way this
   command's own trigger is (see below) — `cord` runs no background scheduler.
   A Deployment that hosts more than one Graph tracks one active-claim count
   per Deployment alias, so one Graph going idle never stops a sibling
   Graph's still-active Run.
3. **Signal-ID dedupe** (`cord_runtime.signal_dedupe`): claimed only
   immediately before submission, and never rolled back afterward — a failed
   or ambiguous `execute()` result still means the Run may have been
   accepted, so a redelivered Signal (including after a restart) must not
   resubmit it. A Signal that never reaches submission (unmatched, an
   invalid mapping, a concurrency skip, or a Deployment that failed to
   start) is not claimed, so a corrected redelivery can still retry. The
   default retention window is 24h.

## Out of scope here

HTTP/webhook adapters, `run.finished` cascades, and production scheduler
infrastructure beyond `cord deployment sweep` (an operator's own
cron/inotify/CI step invokes both it and `cord signal ...`). `cord signal
schedule` records one tick you name explicitly; it does not run a background
scheduler. Interpreting Signal payload meaning to pick or substitute a
target, or requiring model/provider configuration to route, is out of scope
by design ([ADR-0013](decisions/0013-switchboard-boundary.md)). Declared
concurrency policies other than "skip" are not implemented.
