# Local browser viewer (#48, #49)

ADR-0018 and ADR-0019 are Accepted. The installed wheel includes a Python HTTP
server, Jinja2 templates, CSS and JavaScript; there is no frontend build or
model setup. The observation routes (this file's original scope) stay
read-only; execution submission and approval response live on a separate
`/execute`/`/approvals` surface (#49, next section).

## Start

```sh
uv run --locked cord --board /path/to/board serve --archive /path/to/archive
# Optional deployment scope and explicit externally submitted Run:
uv run --locked cord --board /path/to/board serve personal \
  --host 127.0.0.1 --port 8080 --archive /path/to/archive \
  --watch personal:THREAD_ID:INVOCATION_ID
```

The command prints its actual URL (port 0 chooses a free port). Ctrl-C stops the
server. Only IPv4 loopback addresses are accepted in this slice. Remote serving
and authentication remain follow-ups; `--reminder-after`/`--timeout-after`
(default `300`/`3600` seconds) control the approval-expiry clock the
`/approvals` surface starts for each newly discovered waiting interrupt.

`cord add --auth-endpoint <url>` (or `cord auth-endpoint <alias> <url>` for an
already-registered connection) names the entity-owned HTTP port `/approvals`
consults for authorization; without it, that connection's approvals are always
rejected -- see "Execution submission and approval controls" below.

Runs submitted through Cordboard are discovered from its board-level continuity
store. One independent observer per invocation reads the backend's identity/status
API, retries disconnects, and uses the existing logical Run mapping and
`live_reconciliation.reconcile`. No graph state endpoint is requested.
An external Run can be selected with repeatable `--watch`. No observation process
survives `cord serve`.

Archive reads use `archive_health.read_active_spans`, which delegates complete
records to `archive_query.read_spans`. Empty directories and a trailing write
are visible diagnostics; malformed complete records fail visibly. There is no
second archive parser. A recorded Run replaces its live counterpart.

## Routes and shared data

| URL | Content |
| --- | --- |
| `/` | Catalog |
| `/connections/{alias}/graphs/{graph_id}` | Deployment-scoped Graph |
| `/graphs/{graph_id}` | Recorded/live Graph without a connection |
| `/graphs/{graph_id}/ambiguous` | Execution that cannot be attributed to one deployment |
| Any Graph URL + `/runs/{run_id}` | Run / Step / Attempt tree and timeline |
| Any Run URL + `/events` | SSE snapshots of that Run and its delivery diagnostics |
| Any Graph URL + `/execute` (GET/POST) | Explicit-input execution submission form (#49) |
| `/approvals` | Waiting-interrupt inbox across every registered connection (#49) |
| `/approvals/{alias}/{thread_id}/{interrupt_id}` (GET/POST) | One interrupt's bounded content and response form (#49) |

Identifiers are percent-encoded as individual path segments, including embedded
slashes. Connection aliases do not compete with reserved names. Even when
`serve alias` scopes the displayed catalog, correlation considers all registered
deployments, so a scope never manufactures identity evidence.

Every HTML read route accepts `?format=json` or `Accept: application/json`.
It returns the existing catalog, Graph entry or full execution tree record.
The shared catalog now includes additive `runs` and `topology_structure` fields;
`cord view --json` and web JSON use the same projection. Structure is available
only when the existing correlation function accepts the document. Unknown,
invalid, stale and ambiguous structures never get guessed or joined by name.

The HTML Graph page accepts `subject` (substring) and `status` (exact runtime
status or declared Step/Attempt outcome) filters. These presentation filters do
not change JSON payloads or infer a Run outcome from a Step outcome.

SSE sends `event: snapshot`, a monotonic numeric `id`, and JSON:
`{"run": <shared run record or live view>, "source": <source>, "diagnostics": [...]}`.
Source is `live`, `ingestion_pending`, `ingestion_failed`, `recorded`, or
`unavailable`. A reconnect starts after `Last-Event-ID` and sends a complete
current snapshot; it does not claim to replay historical transient states.
Unchanged states send heartbeat comments. Elapsed completion seconds alone do
not trigger events. SSE checks archive file identity/mtime/size and rebuilds
recorded trees only when those files change, without rebuilding the catalog or
probing every deployment each tick. This is a per-stream read optimization,
not a durable cache.

The page also polls the ordinary HTML route every three seconds to discover new
Runs and deployment/topology changes, and as fallback when SSE is unavailable.
Focused filters/links are not replaced; disclosure states are preserved.
Disconnected views retain their last content and show a text alert.

## Rendering and controls handoff

The topology uses the validated upstream structure, including the public
`derived_join_edges` helper for AND joins. BFS rows are a display layout only:
cycles and conditional edges do not imply execution order or parallel semantics.
An expandable text alternative retains full node IDs and edge kinds.

Timeline bars share the recorded Run's time axis and expose exact nanosecond
boundaries. Known approval waits have separate hatched bars; open/missing waits
have explicit unknown/incomplete labels. Native details/summary implement the
Run/Step/Attempt tree. All eight topology states have an icon and text label;
live updates use aria-live and errors use alerts. The 700px breakpoint puts
navigation, forms and timeline rows in one column.

`.cord-action-slot[data-run-id][data-thread-id][data-interrupt-id]` is the
stable insertion point beside waiting Steps/live interruptions. Unknown Thread
or interrupt IDs are empty strings, never invented from span IDs. The slot now
resolves identity through the authorized inbox (below) at render time: a
single matching waiting interrupt becomes a link into `/approvals/...`; more
than one on the same Thread, or no approval authority configured for the
connection, is shown as bounded explanatory text instead of a guess. The
observation page markup itself carries no mutation form or JS handler.

## First-run orientation (#69)

`GET /` states what Cordboard connects and records in one short paragraph, then
renders every catalog entry's own recorded and live Runs as explicit, clickable
choices instead of a bare count. Each choice pairs a plain-English purpose --
`cord_runtime.web.presentation.scenario_summary`, derived only from vocabulary
`build_catalog`/`build_execution_tree` already expose (declared Step/Attempt
outcomes, Aegra status) -- with its exact `run_id` in secondary `<code>`
markup, never inventing graph-specific meaning (AGENTS.md's no-descriptors
rule). A Run with a matching authorized waiting interrupt links straight to
its `/approvals/...` response form (reusing the same authorized-inbox
resolution the `cord-action-slot` on the Run page already performs, exposed to
the index template as `action_slot_for`); an ambiguous or unauthorized case
renders the same bounded explanatory text the Run page already does, never a
guessed link. Every graph card also links to its `/execute` form directly, so
first execution is one click from `/`. An idle graph (no recorded or live
Runs) states that plainly instead of a bare zero, and the sidebar/no-catalog
empty states name the bounded next action (`cord add`) instead of only "no
graphs" prose. None of this adds a new domain classification: it only
rephrases the same `build_catalog`/`_action_slot` outputs the Graph and Run
pages already render, so `?format=json` is unchanged.

## Observed execution path over topology (#70)

Each Run page now renders an "Observed execution path" section overlaying that
Run's own recorded Steps onto the same published topology diagram the Graph
page draws (`cord_runtime.web.presentation.run_topology`, reusing `layout`
unchanged). Only the current, correlated topology (`graph.topology_structure`,
already withheld for absent/unreachable/invalid/stale/ambiguous/no-connection
per ADR-0011/0015) is ever overlaid; every other status renders the same
"Topology unavailable for correlation" fallback the Graph page uses, and the
Run's own timeline/tree below stay unconditionally visible -- an uncorrelated
topology never hides a Run.

Each topology Node gets one of five states from that Run's own Steps alone,
never a second domain classification: `not_observed` (no Step touched it),
`passed`/`failed` (its one Step's declared outcome), `paused` (a Step still
`awaiting_resume`), or `repeated` (more than one Step touched the Node --
a same-Node retry, or a pause whose resuming Step re-executes the same Node).
A `repeated` or `paused` Node keeps every one of its Steps listed, in
recorded order, rather than collapsing them into one fabricated outcome. A
recorded Step whose Node has no match in the topology is never guessed onto a
Node by name; it is surfaced under the diagram as its own explicit warning
instead. Each state has a non-color icon glyph (`NODE_STATUS_LABELS`) on the
node, a `data-node-status` attribute, and a full-text "Execution path text
alternative" `<details>` list mirroring the SVG -- the same accessible pattern
`layout`'s topology diagram already uses. This is a rendering-only projection
over the unchanged `build_execution_tree`/`_graph_view` outputs: it adds no
new field to the shared catalog, so `?format=json` on every Graph/Run route is
byte-identical to before.

## Uncertainty and recovery states (#71)

Every finite status value the shared read models already expose --
`topology_status`, `node_status`, Deployment `reachable`, a Run's
`source` (recorded/live/ingestion_pending/ingestion_failed), the Aegra
`aegra_status` vocabulary, the approval `action_slot` status, an approval
`submission_result` status, and `archive_health`/`collector_health` -- is
bound once, in `cord_runtime.web.presentation.STATE_CATALOG`, to a stable
label, a plain-language explanation, an evidence/ownership cue naming the
module that produced the fact, and a safe next action or an explicit
statement that none is needed (`describe(family, key)`). `TOPOLOGY_LABELS`
and `NODE_STATUS_LABELS` are now derived from this same catalog instead of
a separate copy, so the SVG badges and the new disclosures can never drift.
Templates render each occurrence through `templates/_state.html`'s shared
macros (`state.detail`, `state.legend`, `state.action_slot_body`) as a
`<details>` disclosure next to the existing badge/status text, so the
default view stays as compact as before and the explanation is one click
away. A value absent from the catalog is never guessed: `describe` returns
an explicit "this is a contract gap" action instead of inventing one
(Blockers and handoff).

Two real gaps surfaced while enumerating this vocabulary and were fixed as
part of the same change, not left as follow-on debt:

- `Observation.read_archive` bypassed `archive_health`'s own PENDING/EMPTY/
  INCOMPLETE/FAILED/HEALTHY classification and hand-rolled two ad hoc
  diagnostic strings that conflated PENDING with EMPTY and had no FAILED
  case at all -- a corrupt complete archive record's `ArchiveError`
  propagated uncaught into a whole-page `503`. It now classifies through
  `archive_health` directly, so FAILED renders as a labeled, actionable
  diagnostic and execution stays visible instead of the page going down.
- The approval respond path (`_handle_respond_post`) built its HTML message
  as `f"{result.status}: {result.reason}"`, forwarding a rejecting
  `auth_endpoint`'s own `reason` string into the page unbounded. It now
  renders the cataloged `submission_result` label/explanation/action and
  only shows the boundary's `reason` as a clearly labeled, 200-character-
  bounded secondary line.

`collector_health` (`reachable`/`unavailable`) is in the catalog for
forward compatibility but is not wired to a live probe: `connections.py`
has no per-connection Collector health endpoint field today, and adding one
is a connection-scoped product decision (ADR-0014) out of scope for this
presentation-only change. This is a recorded contract gap, not a silent
omission.

## Execution submission and approval controls (#49, ADR-0019)

`GET/POST /connections/{alias}/graphs/{graph_id}/execute` renders and accepts
an explicit-input execution form: a registered Assistant (populated from the
deployment's own `GET /assistants`, filtered to the selected Graph), Subject, and JSON
input/context. Submission delegates to `aegra_client.execute` -- the same
function the CLI would use -- and durably records the submission via
`run_continuity.record_submission`. A fresh, server-tracked single-use nonce
is embedded per page render; a second POST with the same or a missing nonce
is rejected (`409`) without a second execution attempt, so a double-click or
network retry cannot silently duplicate a Run. An `execute()` transport
failure that leaves the outcome ambiguous is reported as `unknown` and never
retried automatically.

`GET /approvals` lists every waiting runtime interrupt across registered
connections (`approval_inbox.discover_waiting`); `GET/POST
/approvals/{alias}/{thread_id}/{interrupt_id}` shows one interrupt's bounded
business content and accepts a response, delegating to
`approval_inbox.submit_response` unchanged. The submitted `approver` field is
only ever a claim -- authorization is decided by the connection's optional
`auth_endpoint` (`cord add --auth-endpoint ...` / `cord auth-endpoint`), an
entity-owned HTTP port reached through the new
`entity_auth.HttpEntityAuthBoundary`. No `auth_endpoint` configured, or the
port unreachable/returning an invalid response, is always a rejection --
never an implicit allow. `submit_response` now also starts a managed
Deployment that went idle while its Run sat interrupted before resuming it
(#50); a startup failure surfaces as `deployment_unavailable` (`409`) the
same way any other non-`resumed` disposition does, before any response-dedupe
claim is taken.

Every mutation POST requires a matching `Origin` header and a double-submit
`cord_csrf` cookie/form-token pair matching the server's secret
(`hmac.compare_digest`); a mismatch or
missing pair is rejected (`403`) before any domain function runs. Interrupt
values and response payloads live only for the request/response render, never
written to the board beyond the existing identifier-only `response_dedupe`/
`run_continuity` records, and the server never logs a request body.

`_render_execute` now calls `connection_diagnostics.diagnose_connection`
directly (#72's composition, reused as anticipated) before deciding what to
render. When `capabilities.execution.state != "ready"`, the page renders that
capability's `describe("reachability", ...)` entry -- the identical
icon/label/explanation/action `cord diagnose` prints on the CLI for the same
connection -- and offers no submit control, instead of calling
`AegraExecutionBackend.list_assistants()` and silently turning a probe
failure into an empty form. This adds no new route or JSON shape: the
existing `?format=json`/`Accept: application/json` branch on `/execute` is
unchanged (#73). See [docs/onboarding.md](onboarding.md) for the full
operator walkthrough from a registered Deployment endpoint to a first
observable Run, including this diagnosis path.

## Verification

Milestone 8 review (2026-09-16): the service-free suite with `proxy` and
`web-test` enabled passed **611 tests**, with 36 external-integration tests
deselected. The wheel build, isolated installation and browser asset/template
checks passed. `actionlint` passed for the new CI workflow. These are local
results; a hosted Actions run and external stack checks were not executed.

Local commands:

```sh
uv run --locked --group web-test playwright install chromium
uv run --locked --group proxy --group web-test pytest -q \
  -m 'not collector and not aegra and not langfuse'
uv build --wheel
```

`tests/test_web.py` covers HTTP/JSON identity and escaping, eight topology
states, declared outcomes, exact wait intervals, SSE reconnect/deduplication,
ingestion deadlines and durable replacement, empty/error/disconnected views,
loopback binding, responsive DOM, keyboard focus and polling fallback, plus
(#69) the index orientation text, per-Run plain-English summaries derived
purely from declared outcomes/Aegra status, the authorized-inbox action link
(ready/ambiguous/no-authority) surfaced directly on `/`, the bounded empty-
catalog/empty-graph next actions, and exact JSON parity with the unchanged
`build_catalog` model. It also covers (#70) `run_topology` marking only
observed Nodes and withholding without a current correlated topology,
preserving a paused Node's own Step outcome, preserving repeated same-Node
Step executions instead of collapsing them, surfacing an unmatched Step's
Node without guessing, the Run page's non-color text-alternative rendering
for each state, an uncorrelated topology never hiding the Run's timeline/tree,
and exact JSON parity with the unchanged `run` model.
The browser test writes desktop/narrow screenshots (including the `/` index
itself) to pytest's temporary folder and asserts the orientation heading and
a named scenario link are present and keyboard-reachable at both widths, and
(#70) that each Run page's "Observed execution path" diagram is present and
narrow-viewport-safe alongside the existing timeline.
It needs the optional web-test group; absent Playwright is an explicit skip,
not browser acceptance.

`tests/test_web_controls.py` (#49) exercises the execution/approval surface
against a real `cord serve` process and a real browser, with only the
upstream Aegra and `auth_endpoint` HTTP calls scripted (the same
`requests.Session.request` monkeypatch `tests/test_aegra_client.py` already
uses): submit -> pause -> authorized resume -> resumed disposition; a
rejected response never reaching `backend.resume`; an ambiguous resume
transport failure surfacing as `unknown`, not a guessed success; a
same-nonce double submission creating exactly one Thread; and cross-origin
or missing/invalid-CSRF-token POSTs rejected (`403`) before any domain
function runs. It needs the same optional web-test group.

`scripts/verify-browser-artifact.py` is a black-box consumer: it invokes the
installed CLI and uses HTTP and Playwright, never imports Cordboard internals.
Its inputs are a running external Testbed Aegra/Postgres/Collector environment,
one idle topology publisher, one success, one retry and one interrupt execution.
Two connection aliases advertising the idle graph check independent navigation.

The external Testbed still owns its acceptance suite and future migration of
this reproducible check. It is not replaced by the local mocked rendering tests.
The present external interrupt fixture produces no telemetry: the artifact check
asserts live interruption and ingestion-pending visibility, never a fabricated
interrupt duration. Recorded open/resumed wait intervals are covered by the
shared-model and local DOM tests.

### Executed evidence — 2026-09-16

- Focused existing viewer/CLI checks: **128 passed**.
- Final service-free suite including proxy and browser groups: **555 passed,
  36 deselected**, 63.77 seconds. The deselected Collector/Aegra/Langfuse product
  tests were not counted as passes.
- Chromium 145 (Playwright 1.58.0): actual DOM assertions, 1280×900 and 375×667
  screenshots, keyboard focus, no horizontal overflow and no page JavaScript errors.
- Wheel `cord_runtime-0.0.1-py3-none-any.whl` SHA256:
  `50e82235e3191ba062478c399fce2838508d6de8f0494d4fd7ea33b5a6b3e6d9`.
  Installed non-editably; `uv pip check` passed.
- Real Testbed Aegra 0.10.4/Postgres 16.10/Collector 0.148.0:
  **installed browser acceptance passed**. The isolated copy under
  `/tmp/cordboard-browser-acceptance` added pinned Jinja2 and Playwright to its
  baseline; the original Testbed checkout/environment was not modified.
- An initial artifact run found navigation waiting on a live SSE connection.
  Starting browser enhancement after the load event fixed it; the subsequent
  wheel and complete artifact run above passed. Earlier failed-run artifacts
  remain separate and are not passed evidence.
- Local Markdown reference checks and `git diff --check` passed.
  This Python repository has no npm CI script or Rust crate; npm/cargo checks
  are inapplicable. [CI](../.github/workflows/ci.yml) now configures the service-free
  suite including Chromium, wheel build, and installed asset checks. A hosted run
  and external Testbed suite migration are not claimed by this local evidence.

### Executed evidence — 2026-09-17 (#69)

- Focused new/updated suite: `uv run --locked --group web-test pytest -q
  tests/test_web.py` -- **25 passed** (Chromium DOM/screenshot tests included,
  no skips), covering the four new/updated orientation tests plus every
  pre-existing case unchanged.
- Full service-free suite: `uv run --locked --group proxy --group web-test
  pytest -q -m 'not collector and not aegra and not langfuse'` -- **615
  passed, 36 deselected**, 41.42 seconds (four net-new tests over the #48/#49
  baseline of 611; zero regressions).
- Chromium 145.0.7632.6 (Playwright 1.58.0): the `/` index renders "What
  Cordboard shows here" plus a keyboard-reachable "Recorded Run: passed" link,
  and the 375×667 index screenshot has no horizontal overflow
  (`document.documentElement.scrollWidth <= innerWidth`).
- `uv build --wheel` passed: `cord_runtime-0.0.1-py3-none-any.whl` SHA256
  `05954c8abae10d907786d39e3680be917fdbe9510362d31589866fe57820c93a`, with the
  updated `page.html`/`presentation.py`/`viewer.css` confirmed present inside
  the archive. Installed non-editably into an isolated `uv venv --python
  3.12`; `uv pip check` passed (58 packages compatible), and the installed
  package's `scenario_summary`/`page.html` render correctly from the wheel,
  not from the source tree.
- No real Testbed/browser-acceptance run against `cordboard-testbed` was
  executed for this change -- ADR-0016's control-plane/Testbed boundary keeps
  that suite's migration and final artifact acceptance external; this is local
  mocked-rendering evidence only, consistent with the #48/#49 evidence above.
- `git diff --check` passed on the changed files.

### Executed evidence — 2026-09-17 (#70)

- Focused new/updated suite: `uv run --locked --group proxy --group web-test
  pytest -q tests/test_web.py` -- **30 passed** (Chromium DOM/screenshot tests
  included, no skips), covering the six new/updated observed-path tests plus
  every pre-existing case unchanged (catalog fixture's topology Nodes changed
  from `alpha`/`omega` to `draft`/`verify` to actually correlate against the
  fixture's recorded `draft` Steps; no other fixture-consuming test asserted
  on the old names).
- Full service-free suite: `uv run --locked --group proxy --group web-test
  pytest -q -m 'not collector and not aegra and not langfuse'` -- **620
  passed, 36 deselected**, 43.70 seconds (five net-new tests over the #69
  baseline of 615; zero regressions).
- `uv build --wheel` passed: `cord_runtime-0.0.1-py3-none-any.whl` SHA256
  `243d19f94c41f719aa37f56b2e5605187c2f4eb66813e3ba3e45665d8da5e333`, with the
  updated `page.html`/`presentation.py`/`viewer.css`/`server.py` confirmed
  present inside the archive. Installed non-editably into an isolated `uv venv
  --python 3.12`; `uv pip check` passed (58 packages compatible), and the
  installed package's `run_topology`/`NODE_STATUS_LABELS` import and run
  correctly from the wheel, not from the source tree.
- No real Testbed/browser-acceptance run against `cordboard-testbed` was
  executed for this change -- ADR-0016's control-plane/Testbed boundary keeps
  that suite's migration and final artifact acceptance external; this is local
  mocked-rendering evidence only, consistent with the #48/#49/#69 evidence
  above.

### Executed evidence — 2026-09-17 (#71)

- Focused new/updated suite: `uv run --locked --group proxy --group web-test
  pytest -q tests/test_web.py tests/test_web_controls.py` -- **44 passed**
  (Chromium browser tests included, no skips), covering four new
  `test_web.py` cases (state-catalog completeness, the `describe` contract-gap
  fallback, incomplete-archive visibility, and corrupt-archive FAILED
  visibility instead of a 503) plus one new `test_web_controls.py` case
  (the rejected-response reason is cataloged and bounded), and every
  pre-existing case unchanged.
- Full service-free suite: `uv run --locked --group proxy --group web-test
  pytest -q -m 'not collector and not aegra and not langfuse'` -- **625
  passed, 36 deselected**, 48.40 seconds (five net-new tests over the #70
  baseline of 620; zero regressions).
- `uv build --wheel` passed: `cord_runtime-0.0.1-py3-none-any.whl` SHA256
  `50bb2a737844230e1d3e24fd0f5d70c4f61f2eac8fd089e7a8ca32db0ffa4aaa`, with the
  updated `page.html`/`_state.html`/`presentation.py`/`observation.py`/
  `server.py`/`viewer.css` confirmed present inside the archive. Installed
  non-editably into an isolated `uv venv --python 3.12`; `uv pip check` passed
  (58 packages compatible), and the installed package's `describe`/
  `STATE_CATALOG` import and run correctly from the wheel, not from the
  source tree.
- No real Testbed/browser-acceptance run against `cordboard-testbed` was
  executed for this change -- ADR-0016's control-plane/Testbed boundary keeps
  that suite's migration and final artifact acceptance external; this is
  local mocked-rendering evidence only, consistent with the #48/#49/#69/#70
  evidence above.

Reproduction after the external Testbed's documented candidate installation and
`scripts/start-environment`:

```sh
/tmp/cordboard-browser-acceptance/.venv/bin/python scripts/verify-browser-artifact.py \
  --cord /tmp/cordboard-browser-acceptance/.venv/bin/cord \
  --archive /tmp/cordboard-browser-acceptance/.artifacts/browser-48/archive \
  --evidence /tmp/cordboard-browser-acceptance/.artifacts/web-check-2
```

Use a fresh evidence directory for each run. [Exact executed commands/results](evidence/browser-viewer/results.json)
and [candidate installation inventory](evidence/browser-viewer/candidate.json)
are retained. Screenshot inspection confirmed readable labels, true timing bars,
visible unknown waits and no clipped narrow-screen content:
[idle topology](evidence/browser-viewer/idle-narrow.png),
[success](evidence/browser-viewer/success-desktop.png),
[retry](evidence/browser-viewer/retry-desktop.png),
[interrupt](evidence/browser-viewer/interrupt-narrow.png),
[disconnect](evidence/browser-viewer/disconnected-narrow.png).

| Acceptance criterion | Evidence |
| --- | --- |
| Same-ID Graphs remain separately selectable | Encoded/scoped HTTP tests and installed idle-a/idle-b navigation |
| Topology before execution; absent/stale/ambiguous never hide records | Installed idle SVG before any idle Run; all eight states and ambiguous assignment tested against the shared model |
| Actual boundaries, declared outcomes, no invented waits | Installed success/retry JSON and DOM; shared timing tests, open/missing wait labels and exact wait-layout assertion |
| Continuous live/recorded/ingestion diagnostics; accessible disconnect/empty/error | Installed automatic new-Run discovery, interrupted→pending and forced server disconnect; deterministic pending→failed→recorded tests and polling/focus/error checks |
| Installed browser/DOM and screenshots, including narrow viewport | External synthetic environment and non-editable wheel, Chromium DOM assertions and inspected desktop/375px screenshots |
