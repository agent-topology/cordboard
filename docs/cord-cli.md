# `cord` CLI: connect and run existing deployments (#12)

`cord add`, `cord list`, and `cord run` connect an already-running Aegra
Deployment and invoke a caller-selected graph, with no manifest, model
settings, or graph-specific platform code
([ADR-0013](decisions/0013-switchboard-boundary.md),
[ADR-0014](decisions/0014-no-graph-descriptors.md),
[ADR-0015](decisions/0015-never-block-connection.md)). Cordboard does not
describe the graph it connects to; a connection record names only where a
Deployment lives. `cord view` (#13) is the generic catalog/topology/recorded-
execution viewer; see [viewer.md](viewer.md) for its full contract, including
how it scopes catalog entries per connection and correlates a multi-graph
topology document through `cord graph-map`'s explicit association (#43).
`cord graph-map` stores that association and `cord sync` explicitly refreshes
a connection's topology snapshot (#43). `cord
deployment sweep` stops idle managed Deployments (#17;
[signal-routing.md](signal-routing.md) has the full dedupe/concurrency/
lazy-startup contract). `cord new` and `cord up`'s manifest/drift handling
remain planned (#11, #16).

## Storage

Connections are stored per board (default: the current directory) at
`.cordboard/connections.json`, a single JSON object keyed by alias:

```json
{
  "aegra-local": { "endpoint": "http://127.0.0.1:2026" },
  "local": { "endpoint": "http://127.0.0.1:2027",
             "launch": ["aegra", "serve", "--port", "2027"], "idle_after": 600.0 }
}
```

`launch`/`idle_after` are optional and only present when the connection was
registered with `--launch`. Their presence is what makes a Deployment
"managed" (#17): Cordboard may start it on demand and stop it once idle.
Their absence keeps a Deployment "external" — only ever contacted, never
started or stopped, exactly as in the first slices.

A record also optionally carries `graph_map` (#43), set only by `cord
graph-map`: an explicit `{document_graph_id: cord_graph_id}` association from
a published topology document's local `graphs[].id` to the recorded
`cord.graph.id`, used to correlate a multi-graph document's Nodes. Its
absence — including every record predating this field — is read as no
associations, not an error.

Writes are atomic (write to a temp file, then `os.replace`). The endpoint must
be an `http`/`https` URL with a host and **no userinfo** (`user:pass@host` is
rejected); no credentials or graph descriptors are ever stored. Adding an
alias that already exists fails unless `--replace` is given explicitly — no
silent overwrite.

Use `--board <dir>` on any command to operate on a board other than the
current directory.

## Commands

### `cord add <alias> <endpoint> [--launch "<command>"] [--idle-after <seconds>] [--replace]`

Registers a Deployment's address under `alias`. Does not contact the
Deployment or require it to be reachable.

```sh
$ cord add aegra-local http://127.0.0.1:2026
added 'aegra-local' -> http://127.0.0.1:2026
```

`--launch` opts the Deployment into managed startup/idle shutdown (#17): an
operator-supplied command (parsed like a shell command line) that starts this
Deployment's own existing entrypoint. Reuse the entity's existing Aegra
entrypoint, dependency environment, and health check — this never recreates
its graph factories, auth, or Postgres management. `--idle-after` (seconds,
default 600) only applies with `--launch`.

```sh
$ cord add local http://127.0.0.1:2027 --launch "aegra serve --port 2027" --idle-after 300
added 'local' -> http://127.0.0.1:2027 (managed)
```

### `cord deployment sweep`

Stops every managed Deployment (one registered with `--launch`) that is
currently idle — no active claim from a Signal it started for — beyond its
declared `idle_after`. External Deployments, and a Deployment any Graph it
hosts is still actively using, are left untouched. Meant to be invoked by an
operator's own cron, the same way a Signal's own trigger invokes `cord
signal ...`; `cord` runs no background scheduler itself.

```sh
$ cord deployment sweep
stopped: local
$ cord deployment sweep
no idle managed deployments
```

### `cord graph-map <alias> <document_graph_id> <graph_id>`

Stores one explicit association from a published topology document's local
`graphs[].id` (`document_graph_id`) to the recorded `cord.graph.id`
(`graph_id`) on an already-registered alias's connection record (#43). Never
inferred by name or position — this is the only way a multi-graph document's
Nodes become visible in `cord view`. Setting the same `document_graph_id`
again replaces its prior association; other entries on the same alias are
left unchanged.

```sh
$ cord graph-map aegra-local billing cord-graph-billing
mapped 'aegra-local' document graph 'billing' -> 'cord-graph-billing'
```

### `cord sync [alias]`

Explicitly refreshes one connection's, or every registered connection's,
published topology snapshot (`cord_runtime.topology.refresh_snapshot`, #11
via #43's CLI wiring) and prints each alias's fresh fetch status. A `valid`
fetch replaces the stored snapshot; `absent`, `unreachable`, and `invalid`
leave it untouched — these stay distinct diagnostics and never block
anything ([ADR-0015](decisions/0015-never-block-connection.md)).

```sh
$ cord sync
aegra-local	http://127.0.0.1:2026	valid
```

Exits `1` if any refreshed connection's fetch was not `valid`, `0` otherwise
(including when no connections are registered) — a status report, like
`list`, not an action that can be misused.

### `cord list [alias]`

Prints reachability (`GET /health`) and the graphs the Deployment exposes
(`GET /assistants`, read for its `graph_id` field — the public Agent Protocol
surface, not a manifest) for every registered alias, or one named alias.
Registering a Deployment never requires a manifest to exist
([ADR-0015](decisions/0015-never-block-connection.md)); `list` shows only what
the running server itself reports.

```sh
$ cord list
aegra-local	http://127.0.0.1:2026	reachable	graphs=minimal-graph, opaque-graph
```

An unreachable Deployment is shown, not treated as invalid input:

```sh
$ cord list
aegra-local	http://127.0.0.1:2026	unreachable	graphs=-
```

### `cord view [alias] [--archive <path>]... [--watch <alias:thread_id:run_id>]... [--watch-timeout <seconds>] [--json]`

Prints the catalog: every Graph named by a registered connection's
`/assistants` probe or present in recorded execution, its optional published
topology, and its Subjects/Runs/Steps/Attempts. `--archive` (repeatable)
reads recorded execution through the shared archive contract
(`cord_runtime.archive_query.read_spans`); without it, connected Graphs still
render with an empty Run list. `--json` prints the catalog as one JSON object.
See [viewer.md](viewer.md) for the full topology-correlation and staleness
contract.

`--watch` (repeatable) follows one Run's live execution over its Deployment's
own Aegra SSE stream (`GET /threads/{thread_id}/runs/{run_id}/stream`) until
it reaches a terminal status or `--watch-timeout` elapses (default 120s), then
renders it alongside recorded execution -- see [viewer.md](viewer.md#live-runs-20)
for the full live/recorded reconciliation contract (#20).

```sh
$ cord view --archive examples/archive.graph-id.sample.otlp.jsonl
Graph archive-fixture  (no connection, topology: no_connection)
  Subject fixture:urn:cordboard:fixture:5
    Run 17581de7-9734-4a01-9619-3d1a1fec89c8
      Step draft -> passed
        Attempt 1, tier=fast -> failed
        Attempt 2, tier=fast -> escalated
        Attempt 3, tier=deep -> passed

$ cord view --watch aegra-local:9f3c...:a1b2...
Graph minimal-graph  (aegra-local, reachable, topology: absent)
  Live Run a1b2... (thread 9f3c...) [live, running]
```

### `cord run <alias> <assistant> <subject> <input.json> [--context <context.json>] [--timeout <seconds>]`

Invokes `assistant` (an explicit assistant id or graph id — the caller's
choice, never inferred) on the named connection with a required, non-empty,
opaque `subject` and JSON `input`. `--context`, if given, is transported
unread as the Agent Protocol's top-level `context` field, distinct from
`config.configurable`; a graph-owned bridge (for example Omiologic's request
scope) is what interprets it. `--timeout` bounds how long the command polls
before reporting a Run as waiting; it does not cancel the server Run.

Prints the result as one JSON line to stdout, separate from any telemetry:

```sh
$ cord run aegra-local minimal-graph "urn:cordboard:demo" input.json
{"run_id": "...", "thread_id": "...", "status": "success", "values": {...}}
```

A new execution always gets a fresh Thread/Run; resume is not implemented
here (#14). The command never retries an ambiguous POST automatically — each
invocation submits at most one `POST /threads` and one `POST .../runs`.

## Status and exit codes

| Exit | Meaning |
| --- | --- |
| `0` | Successful command. For `run`, the Aegra Run reached `status: "success"`. |
| `2` | Invalid usage or input: bad CLI arguments, an unknown alias, a malformed endpoint or connections file, invalid JSON in the input/context file, or an empty Subject. Nothing is submitted to the Deployment. |
| `1` | Connection or execution failure: an unreachable Deployment, or a Run whose `status` is not `"success"`. |

`run`'s `status` field distinguishes a bounded **waiting** Run — the server
reported `"interrupted"` (a pause), or the `--timeout` budget elapsed while it
was still `"pending"`/`"running"` — from a genuine failure. Both a paused Run
and a real failure exit `1` (there is no fourth exit code), but a waiting
result always carries `run_id`/`thread_id` on stdout so the caller can poll or
resume later; a failure prints a payload-free diagnostic to stderr instead,
with no request/response body or credential in it. `run` never silently
treats a pause as success, and never turns it into a retry.

`list` exits `1` if any Deployment it queried was unreachable, `0` otherwise
(including when no connections are registered yet) — it is a status report,
not an action that can be misused.

## Out of scope here

Graph scaffolding (`cord new`), automatic server provisioning, entity
credential or model configuration, approval submission, and resume of an
interrupted Run (#14) are not part of this surface. An Omiologic readiness
invocation runs through this same generic path
(`cord run <alias> readiness <subject> input.json`); it exercises connectivity
only and is not evidence of `issue_resolution` or `core_notification_fixture`
behavior, which require entity-owned operational configuration and explicit
operator authorization.
