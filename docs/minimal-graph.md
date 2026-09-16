# Minimal deterministic graph

Issue #4 establishes an executable contract before platform integrations.
The graph, enums, immutable records, controlled response callable, and module
entrypoint live in [examples/minimal_graph.py](../examples/minimal_graph.py).
The fixtures and assertions live in [tests/test_minimal_graph.py](../tests/test_minimal_graph.py).

## Run locally

From the repository root, with uv installed:

```sh
uv sync --locked --python 3.12
uv run --locked python -m examples.minimal_graph
uv run --locked pytest -q tests/test_minimal_graph.py
```

The initial sync downloads Python if necessary and installs dependencies.
After synchronization, the graph and tests need no service, provider credential,
or network connection. The supported `run()` entrypoint disables LangSmith
tracing within the invocation, including inherited tracing settings. Do not
attach additional exporters or invoke the compiled graph directly with tracing enabled.
The optional `tracer` argument uses the redacting archive path described in
[Model proxy integration](model-proxy.md).

Verified locally on 2026-09-11 with uv 0.12.10 and CPython 3.12.14:

| Direct dependency | Pin | Purpose |
| --- | --- | --- |
| langgraph | 1.2.11 | Outer graph and draft Attempt subgraph |
| langsmith | 0.12.4 | Public context manager to disable automatic tracing |
| pytest | 9.1.1 | Development tests |

[pyproject.toml](../pyproject.toml) pins direct dependencies;
[uv.lock](../uv.lock) pins their complete resolution. Python is constrained to
3.12. These versions installed and imported successfully together. This is
evidence for this example, not an upstream topology or redaction compatibility
matrix. APIs were checked against the public
[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api);
release metadata is published on [PyPI](https://pypi.org/project/langgraph/1.2.11/).

## Execution contract

The outer LangGraph executes `fetch → draft → verify` exactly once each.
`fetch` takes the supplied source in memory; it performs no external fetch.
The draft Step owns an internal LangGraph with one `attempt` node and a
conditional edge back to `attempt` or to completion. Generation and assessment
share that node so one live span encloses both operations. Internal transitions are
Attempts within that one draft Step, not repeated executions of the outer draft
Node. The graph owns the bounded policy `fast → fast → deep`; the injected
model receives only `(source, tier, attempt_number)` and returns a string.

The tiny task is to copy `hello` exactly. R1 requires the source content;
R5 rejects a trailing period. Removing exactly one trailing period is the only
repair. The graph validates the repaired value again before accepting it.
Verification is deterministic and calls no model.

| Case | Controlled responses | Ordered Tier/Attempt Outcome | Draft Step | Output | Escalations |
| --- | --- | --- | --- | --- | --- |
| Passed | `hello` | fast/passed | passed | `hello` | 0 |
| Repaired | `hello.` | fast/failed | repaired | `hello` | 0 |
| Same-tier retry | `wrong`, `hello` | fast/failed, fast/passed | passed | `hello` | 0 |
| Escalation (entrypoint) | `wrong`, `still wrong`, `hello` | fast/failed, fast/escalated, deep/passed | passed | `hello` | 1 |
| Terminal failure | `wrong`, `wrong`, `wrong` | fast/failed, fast/escalated, deep/failed | failed | `null` | 1 |

An Attempt retains its original deterministic Verdict and violated rules.
For a repair, that Attempt remains `failed`; the Step becomes `repaired` and
the final verify Step passes. Failed repair candidates cannot become successful
results. All-fail runs terminate after three Attempts with no accepted output;
the verify Step also fails. An exhausted response fixture raises an error rather
than pretending that the graph passed or providing an implicit fallback.

`StepOutcome` and `AttemptOutcome` are distinct plain enums, so matching textual
values do not compare equal across types. Their record constructors reject
wrong enum types and raw strings. The Step vocabulary includes
`awaiting_approval` and `halted` for contract completeness; this example does
not implement approval or halt workflows. `escalated` is only an Attempt value.

Subject is a required non-empty opaque string, kept exactly as supplied; there
is no scheme parser or normalization. Each call starts fresh state. There is
no checkpointer, shared mutable response cursor, or Subject-keyed state store.
Reusing the compiled graph or the fixed response callable does not consume
responses across executions. Injected model implementations are responsible
for their own state; the supplied callable is stateless.

Records are in memory and the entrypoint prints synthetic fixture results as
JSON. The default entrypoint remains service-free. With an explicit tracer,
`run()` also emits Run/Step/Attempt spans; execution objects are per-invocation
configuration, never checkpoint state or mutable shared closures. The
[proxy entrypoint](model-proxy.md) supplies the redacting exporter and sends
W3C trace context to LiteLLM. Aegra, the `cord` CLI, UI, triggers and topology
derivation remain planned.

## Verification evidence

The measurement is execution disposition and isolation, not throughput or
provider quality. Inputs are one short source string and at most three responses
per execution. Separate pytest fixtures cover passed, repaired, and terminal
failed dispositions. Tests assert final results, exact ordered Attempts,
ownership under one draft Step, escalation counts, outcome type rejection,
Subject preservation, sequential reruns, and concurrent invocation isolation.
Socket connection attempts fail the tests, and a test enables inherited tracing
to check that `run()` still disables it.

Observed commands and results:

```text
uv sync --locked --python 3.12
  resolved 45 packages; checked 42 installed packages
uv run --locked python -m examples.minimal_graph
  subject=github:issue/4; output=hello
  fast/failed → fast/escalated → deep/passed; escalation_count=1
uv run --locked pytest -q
  27 passed
```

ADR-0004 and ADR-0008 now record this sequence in Korean, including why a
same-tier retry is not an escalation. Root summaries and the decision index
link to the implemented contract. The current [CI workflow](../.github/workflows/ci.yml)
runs Python and browser checks. There is no npm project or Rust crate;
no npm or cargo checks are claimed.
