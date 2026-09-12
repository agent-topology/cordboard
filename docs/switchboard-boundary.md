# Switchboard boundary and #7 acceptance

[ADR-0013](decisions/0013-switchboard-boundary.md) establishes the boundary:
the caller selects a Graph/Assistant, Cordboard connects its execution API and
records execution, and the graph owns its models, tools and private state.
Model identities, provider keys, SDKs, gateways, model counts and Tier policies
are not registration or execution requirements. Transporting a graph's API
payload does not mean interpreting or archiving its content.

## Implementation

- The generic [Aegra client](../src/cord_runtime/aegra_client.py) still accepts
  the caller's graph identifier and payload. No graph-specific change was needed.
- LiteLLM configuration, launcher and telemetry callback now live in
  [examples/model_proxy](../examples/model_proxy/config.py), outside the runtime
  wheel. The root `proxy` dependency group is opt-in. This example's environment
  settings are named `EXAMPLE_PROVIDER_*`, `EXAMPLE_FAST_MODEL` and
  `EXAMPLE_DEEP_MODEL`; they are not Cordboard configuration.
- [Execution instrumentation](../src/cord_runtime/execution.py) permits
  `step.attempt(1, outcome=AttemptOutcome.PASSED)` without Tier or model data.
  Existing positional calls remain supported. Omitting Outcome starts as failed;
  a graph must explicitly declare success. Exceptions still record failure.
- New Run roots emit semconv `0.3.0`. The [query](archive-query.md) accepts old
  `0.2.0` records under their original Tier requirement and new `0.3.0` records
  with optional Tier. It counts only declared escalations. Existing captures are
  unchanged; unsupported/identity-free old records still fail explicitly.
- `cord.yaml`/`x-cord` model policy plans and missing-Tier catalog warnings have
  been withdrawn. Historical HTML carries a visible ADR-0013 notice.

## What is measured and the minimum input

The new [opaque graph](../examples/opaque_graph.py) counts an `items` list. Only
the graph and its caller understand those fields. It has a separate
[Aegra deployment file](../aegra/opaque.json), no model, no proxy and no Tier.
Two requests (one item, then zero items) distinguish successful routing and fresh
state. The first item is a synthetic credential fixture, to verify that the
request content is absent from the telemetry archive. It is intentionally
checkpointed by Aegra; checkpoint storage is not a redacted telemetry archive.

The assertions examine actual HTTP execution, Postgres state and Collector OTLP:
two different API Run IDs, Thread IDs and traces; six spans (two each of Run,
Step and Attempt); exact parents and contained times; no model/Tier attributes;
zero declared escalations; a clean raw/decoded archive scan. No provider server,
credentials or LiteLLM process is started for this test.

## Reproduction

Prepare Docker and the checksum-verified Collector/redact-secret binaries using
[archive setup](archive.md#setup). Commands run from the repository root.
Use fresh `--basetemp` paths: pytest replaces an existing directory at that path.

```sh
uv sync --locked --project aegra
UV_PROJECT_ENVIRONMENT=.tools/boundary-venv uv sync --locked
UV_PROJECT_ENVIRONMENT=.tools/boundary-venv uv run --locked python -c \
  'from importlib.util import find_spec; assert find_spec("litellm") is None; assert find_spec("cord_runtime.proxy_config") is None'
UV_PROJECT_ENVIRONMENT=.tools/boundary-venv uv run --locked pytest -q \
  tests/test_aegra.py::test_graph_execution_without_model_configuration \
  --basetemp .tools/issue7-boundary-evidence

# Explicitly add the optional gateway dependency for its regression coverage.
uv run --locked --group proxy pytest -q --basetemp .tools/issue7-final-evidence
```

An old environment may retain an empty `litellm` namespace directory after
uninstalling the distribution. The isolated environment above establishes actual
absence without relying on that residual import path. Both lockfiles retain their
pinned versions; no upstream model API or integration was upgraded.

## Revised acceptance and tracker handoff

This is a scope correction, not a claim that an unrun check passed. The original
#7/#8/#9 descriptions treated real-provider compatibility as a platform exit gate;
that requirement is superseded by ADR-0013. GitHub issues and the milestone were
not edited or closed by this local change. Their provider requirements must be
reconciled with this decision before using those old checklists for closure.

| Criterion | Current scope and evidence |
| --- | --- |
| Controlled outcomes and archive counts | Optional LiteLLM example still checks failed → escalated → passed, one escalation; integrated five-Run fixture has 40 spans and two escalations |
| Actual-provider compatibility | Optional graph-owned integration check; **not run**, no provider compatibility claimed |
| Graph/process trace identity | Existing graph/proxy/Aegra checks retained; new model-free graph has six spans with no unexplained orphans |
| Query, redaction gate, synthetic secrets | Actual archives checked; fresh negative gate tests retained; Tier-free declarations count without model inference |
| Platform does not require model configuration | Clean default environment has no LiteLLM or runtime proxy launcher; independent graph executes without provider settings |

Sonnet and GPT 5.5 in [issue planning](issue-planning.md) identify implementation
agents that receive the same task specification. They do not mandate a pair of
runtime providers. This corrects the old example guide's interpretation.

## Actual results

Boundary verification on 2026-09-11 (America/New_York):

| Command/check | Result |
| --- | --- |
| Clean isolated default environment check above | Passed: neither LiteLLM nor the old runtime proxy launcher is importable |
| Model-free Aegra/Collector test in that environment | **1 passed in 4.78s** |
| Service-free tests | **86 passed, 27 deselected in 0.60s** |
| `uv run --locked --group proxy pytest -q --basetemp .tools/issue7-final-evidence` | **113 passed in 54.20s** |
| Model-free actual archive `archive-check` | `records=6 findings=False failure=False`; query `[]` |
| Integrated LiteLLM/Aegra actual archive `archive-check` | `records=40 findings=False failure=False`; query `minimal-graph / draft / 2` |
| `uv build --wheel --out-dir .tools/boundary-dist` and wheel inspection | Passed: no proxy modules, examples or LiteLLM dependency in runtime wheel |
| Changed document references and `git diff --check` | Passed; 149 local references checked |

An initial full run had 112 passes and one stale test assertion for the old
emitted semconv version. The expected new version was corrected to 0.3.0; the
final full run above passed. Historical 0.2.0 archive fixtures remain unchanged
and continue to pass their separate compatibility checks.

The actual-provider smoke remains **not run**. No paid model request was made.
No npm/Cargo/CI workflow exists in this repository.
