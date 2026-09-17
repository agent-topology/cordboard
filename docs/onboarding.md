# Bring your own Deployment: from endpoint to a first observable Run (#73)

This walks an operator through the existing `cord`/browser command sequence
end to end: registering an independently owned Deployment, an optional
topology association, one explicit execution, and observing it live then
recorded -- or stopping at a precise readiness diagnosis instead of an empty
form. Every command here already exists; this page only states the exact
sequence and cross-links each step's own reference
([docs/cord-cli.md](cord-cli.md), [docs/browser-viewer.md](browser-viewer.md)).
Cordboard never requires editing its own code or adding a per-graph
configuration file to complete this ([ADR-0013](decisions/0013-switchboard-boundary.md),
[ADR-0014](decisions/0014-no-graph-descriptors.md)).

## Fixture used below

[`aegra/opaque.json`](../aegra/opaque.json) deploys
[`examples/opaque_graph.py`](../examples/opaque_graph.py): a deterministic,
model-free, one-assistant (`opaque-graph`) graph with no provider credentials
or Tier policy, already reused by earlier issues' acceptance
([docs/switchboard-boundary.md](switchboard-boundary.md)). It stands in for
any independently owned Deployment an operator would actually connect --
Cordboard's registration/execution/observation path does not distinguish
between them.

Launch it the same way [docs/aegra.md](aegra.md#manual-execution) launches
the default deployment, pointed at the opaque config instead:

```sh
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_USER=postgres POSTGRES_DB=cordboard
docker compose -f aegra/compose.yaml up -d --wait
uv run --locked --project aegra python -m examples.aegra_server --config aegra/opaque.json
```

The server binds to `127.0.0.1:2026`; `GET /health` must return `200`.

## Successful path

1. **Register the Deployment.** `cord add` never contacts the endpoint or
   requires it to already be reachable ([docs/cord-cli.md](cord-cli.md#cord-add-alias-endpoint---launch-command---idle-after-seconds---replace)):

   ```sh
   cord add opaque http://127.0.0.1:2026
   ```

2. **Identify its reachable Assistants/Graphs.**

   ```sh
   cord list opaque
   # opaque	http://127.0.0.1:2026	reachable	graphs=opaque-graph
   cord diagnose opaque --json
   # {"alias": "opaque", ..., "capabilities": {"execution": {"state": "ready", ...}, ...}}
   ```

   `cord diagnose`'s exit status follows only the required `execution`
   capability: `0` here means `ready`
   ([docs/cord-cli.md](cord-cli.md#cord-diagnose-alias---archive-path---json)).
   Stop here if it is not `ready` -- see "Missing-required-contract path"
   below for what that looks like and why it is safe to stop.

3. **Optional topology.** Run `cord sync opaque` / `cord graph-map` if a
   topology document is published and multi-graph correlation is wanted, or
   skip both entirely. Missing topology is optional and non-blocking
   ([ADR-0015](decisions/0015-never-block-connection.md)): the execute form
   below is reachable either way.

4. **Start the browser viewer.**

   ```sh
   cord serve
   ```

   Open the printed URL. `opaque-graph` appears as a card; "Submit an
   execution" links to its execute form
   ([docs/browser-viewer.md](browser-viewer.md#routes-and-shared-data)).

5. **Submit one execution.** Fill Subject and JSON input, then submit. The
   form POSTs to `.../execute`, which 303s to the new Run's page
   ([docs/browser-viewer.md](browser-viewer.md#execution-submission-and-approval-controls-49-adr-0019)).
   The Run shows `live` status first, then `recorded` once its archive
   record arrives (`cord serve --archive <path>` to observe recorded
   history; omit it and the Run still shows live).

## Missing-required-contract path

Point a fresh alias at an endpoint with no reachable Aegra process:

```sh
cord add broken http://127.0.0.1:1
cord diagnose broken
# broken  http://127.0.0.1:1  checked ...
#   execution [required] -> unavailable: Disconnected — The last probe of this Deployment's endpoint failed or timed out.
#   ...
echo $?   # 1
```

`cord diagnose`'s `execution: unavailable` line is the same
`web.presentation.describe("reachability", ...)` vocabulary the browser now
shows. If `broken` (or any alias) was reachable at some point and its
graph_id is already known, opening that graph's `.../execute` page directly
renders the identical diagnosis -- icon, label, explanation, and action --
instead of an empty Assistant dropdown, and offers no submit control
(`data-execution-state="unavailable"` on the page). A Deployment that has
never once been reachable has no known graph_id to browse to yet; register
it, confirm `cord diagnose` reports `ready`, then continue with the
successful path above.

Missing approval authority is unrelated to this path and never blocks it:
execution does not require `auth_endpoint` to succeed. An unauthorized
respond attempt is explained non-blockingly at respond time instead
([docs/browser-viewer.md](browser-viewer.md#execution-submission-and-approval-controls-49-adr-0019)).

## Out of scope

Creating graph source code, selecting models, installing provider
credentials, or owning production startup stay outside Cordboard
([ADR-0013](decisions/0013-switchboard-boundary.md)). There is no per-graph
`cord.yaml` or Cordboard topology extension
([ADR-0014](decisions/0014-no-graph-descriptors.md)). This page documents an
existing command sequence; it does not add a new command, HTTP route, or
JSON shape.
