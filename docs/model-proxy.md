# Optional graph-owned LiteLLM example (#8)

[ADR-0013](decisions/0013-switchboard-boundary.md) supersedes the platform-wide
gateway requirement. Everything on this page configures the example graph's
optional model dependency. Cordboard does not ask operators for provider keys,
model names, alias lists or a proxy URL to connect a graph. Graphs may use their
own SDK, gateway, one model, many models, or no model.

The `fast`/`deep` checks belong to this example's escalation scenario. They do not
validate other graphs. Provider compatibility tests remain useful to this
example's owner, but do not gate Cordboard or Slice 0 completion.

The existing example graph now sends model calls through a DB-free LiteLLM
1.87.0 proxy with exactly two aliases, `fast` and `deep`. The graph owns its
validation and `fast → fast → deep` policy. Each live Attempt encloses generation
and assessment, and records its disposition before ending. Provider mapping is
entirely in [examples/model_proxy/config.yaml](../examples/model_proxy/config.yaml).

## Setup and deterministic verification

Use Python 3.12 and the pinned Collector/redact-secret binaries from
[Archive setup](archive.md#setup). All direct dependencies are pinned in
[pyproject.toml](../pyproject.toml), with complete resolution in `uv.lock`.
LiteLLM's proxy extra includes optional integrations; this slice enables no DB,
virtual keys, budgets, Redis, Langfuse, or enterprise functionality.

```sh
uv sync --locked --group proxy --python 3.12
uv run --locked --group proxy python -m examples.model_proxy.config --validate
uv run --locked --group proxy pytest -q tests/test_proxy.py
uv run --locked --group proxy pytest -q
```

`tests/test_proxy.py` starts a controlled OpenAI-compatible HTTP endpoint, the
actual LiteLLM process, and the existing Collector on temporary loopback ports.
The input is three short responses, not a probabilistic provider failure.
It checks stored Attempt outcomes, direct model-span parents, trace IDs,
temporal containment, and the archive escalation query. Other cases replace
the provider mapping under `fast`, exercise parallel Runs, scan synthetic
credential redaction and private-key suppression, and test the smoke harness.
Nothing in this suite needs real credentials.

## Real-provider setup

The operator supplies `EXAMPLE_PROVIDER_BASE`, `EXAMPLE_PROVIDER_KEY`,
`EXAMPLE_FAST_MODEL`, and `EXAMPLE_DEEP_MODEL` in the process environment. Use a
secret manager or a hidden terminal input for the key; do not put credentials
in YAML, command arguments, shell history, issue text, or captured output.
Model values use LiteLLM's provider-prefixed identifiers. The graph operator
chooses the actual provider and models. Sonnet and GPT 5.5
in the issue-authoring instructions identify implementation agents, not a required
pair of runtime providers. No specific provider compatibility is claimed until
its smoke succeeds. [Boundary acceptance](switchboard-boundary.md) separates
this optional check from Cordboard completion.

For separate providers, edit each alias's `api_base` and `api_key` references to
distinct `os.environ/EXAMPLE_PROVIDER_*` variables. Keep `callback.py` alongside
the config: LiteLLM loads custom callbacks relative to the YAML file.
Multiple deployments may share the same alias, with optional integer `order`.
The graph code contains neither provider identifiers nor keys.

Start a fresh Collector archive as described in [Archive setup](archive.md),
then start the validated proxy in another terminal:

```sh
uv run --locked --group proxy python -m examples.model_proxy.config \
  --config examples/model_proxy/config.yaml --port 4000 \
  --collector http://127.0.0.1:4318/v1/traces
```

The launcher validates before importing LiteLLM and passes only runtime
essentials and explicitly referenced provider environment variables to the
child. Inherited database, debug, Langfuse, and OTel auto-export flags are not
passed. It binds to loopback. `/health/liveliness` must return HTTP 200 before
running the graph. Ctrl-C stops the proxy.

Upstream console output can include provider exceptions and resolved config,
so the launcher discards that output before it can be written. Configuration
errors remain actionable and value-free. If startup fails, rerun `--validate`,
check the named environment references, `callback.py`, the port and locked
installation. Do not turn on payload logging to investigate a real-provider
request. This small launcher is not a product `cord up` command.

## Smoke check and graph execution

Against the running proxy and a fresh archive:

```sh
uv run --locked --group proxy python -m examples.provider_smoke \
  --archive archive \
  --cli .tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin
uv run --locked --group proxy python -m examples.proxy_graph
uv run --locked --group proxy archive-check archive \
  --cli .tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin
```

Both example commands accept `--proxy` and `--collector` URL overrides.
The smoke makes exactly one non-streaming chat request per alias, with a tiny
copy prompt and `max_tokens: 16`. Any non-empty string completion is compatible;
it does not require the model to fail validation or escalate. It verifies two
stored model spans with the correct Attempt parents, redaction stamps, one
trace, zero escalations, and a clean archive scan. Its JSON summary contains
only compatibility and counts. A missing span, blocked export, incompatible
response, or failed scan fails the command. It waits at most ten seconds for
asynchronously scheduled callbacks and Collector buffering.

The graph command separately exercises deterministic validation against actual
model output and prints only the Step outcome, Attempt count, and escalation
count. It may legitimately pass on the first Attempt. It is not the smoke
check's pass/fail oracle. HTTP/provider errors produce a fixed diagnostic;
response bodies are never printed. Requests time out after 60 seconds and do
not follow redirects. A logical Attempt may include up to two configured
availability retries inside the same alias; the default is zero.

## Telemetry and configuration contracts

[ProxyTelemetry](../examples/model_proxy/telemetry.py) implements LiteLLM's
public async `CustomLogger` success/failure callbacks. It extracts the incoming
W3C `traceparent` with the public OTel propagator and creates a `chat` span whose
direct parent is the matching Attempt. Missing/invalid context suppresses the
span; it never manufactures a root or reparents stored data. It records request
model, response model, token counts and fixed success/error status. In the
pinned proxy, the callback's response model is rewritten to the public alias;
the request model retains the selected provider deployment. Neither determines
the graph's Tier or Outcome.

The callback never copies prompts, model output, exception bodies or arbitrary
request metadata. Its only exporter is the existing `RedactingOTLPExporter`,
which invokes public `redact-secret` APIs in-process. A synthetic credential in
the selected model identifier is redacted before OTLP transmission; a synthetic
PEM identifier suppresses the complete model span. The Collector still drops
unstamped spans. The existing gate-negative tests remain part of the full suite.
Model input sent intentionally to the provider is distinct from telemetry.

The built-in LiteLLM OTel callback was investigated first. In 1.87.0 its
`_get_span_context` returns the proxy-parent context but `None` for the parent
object; the success handler therefore emits `litellm_request` under `Received
Proxy Server Request`, even with the shallow-span option disabled. The public
custom callback meets the direct-parent contract without overriding private
methods or changing upstream code. The regression test checks the archived
parent IDs, not a diagram or an HTTP success alone.

[Configuration validation](../examples/model_proxy/config.py) allows only
the settings used by this slice. It rejects cross-alias/general/context-window/
content-policy fallback lists and direct Langfuse or other callbacks, with
instructions to use same-alias deployments and `callback.handler`. It also
rejects unknown settings, extra aliases, DB configuration, literal API keys,
message logging and excessive retry counts. It is an intentionally bounded
configuration contract, not a validator for every possible LiteLLM feature.

## Evidence and limits

Historical #8 evidence (commands below use the current example paths and explicit
optional dependency group). Verified on macOS arm64 with CPython 3.12.14, LiteLLM 1.87.0, Collector 0.148.0,
OTel 1.39.1 and redact-secret 0.1.0b1. This measures routing ownership, propagation, redaction and archive compatibility,
not throughput or provider quality.

| Command | Observed result |
| --- | --- |
| `uv sync --locked --group proxy --python 3.12` | 135 resolved packages, 131 installed packages checked |
| `python -m examples.model_proxy.config --validate` (through locked uv) | configuration valid |
| `uv run --locked --group proxy pytest -q` | **99 passed in 28.97s**, including 24 proxy tests |
| `uv run --locked --group proxy python -m examples.minimal_graph` | hello; exactly one escalation |
| Local Markdown references and `git diff --check` | passed |

The lock resolution changes existing `orjson` from 3.12.0 to 3.11.6 and
`websockets` from 16.1.1 to 15.0.1 to satisfy LiteLLM's constraints. The full
suite covers the pre-existing graph/archive behavior under this resolution.

**Real-provider smoke: not run.** No operator provider mapping/credentials were
present. The controlled smoke-harness test verifies the command's behavior but
is not evidence of any actual provider compatibility. The original #8 checklist
keeps that historical unrun check; ADR-0013 scopes it to this optional example.

Streaming, tool calls, multimodal input, provider-native features, durable
telemetry delivery during Collector downtime, and availability failover under
real outages are not claimed. Standalone graph business results remain in memory;
the separate #9 Aegra path persists checkpoint state in Postgres.
The generic atomic next-Attempt escalation helper remains planned; this graph
uses existing runtime contexts and sets its declared outcome before closing.

Public references: [LiteLLM custom callbacks](https://docs.litellm.ai/docs/observability/custom_callback),
[proxy callback loading](https://docs.litellm.ai/docs/proxy/call_hooks), and
[pinned OTel integration source](https://github.com/BerriAI/litellm/blob/v1.87.0/litellm/integrations/opentelemetry.py).
