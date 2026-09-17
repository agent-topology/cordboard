# agent-topology beta.4 adoption — 2026-09-17

The [published release](https://github.com/agent-topology/agent-topology/releases/tag/v0.1.0-beta.4)
uses source `05486d7ee9cee7650779cb0e9c00a85bd152a3e8` and Python packages
`agent-topology-spec==0.1.0b4` / `agent-topology-langgraph==0.1.0b4`.
The runtime spec dependency was already pinned to b4. This update adds the
producer only to the dev group for reproducible consumer tests, and enables
experimental interpretation revision `"2"` alongside `"1"` in the R3 reader.
Revision 2 preserves the existing branch contract; unknown revisions remain
opaque and unknown branch facts never confirm parallel semantics.

## Released capabilities and adoption

| Capability | beta.4 change | Cordboard behavior |
| --- | --- | --- |
| Expanded parent identity (AT-1) | Parents retain their Node IDs at positive depth | Parent Steps can match the selected topology; regression tested |
| Nested addresses | Children have separate `graphs[]` entries, referenced by `subgraphId`; repeated call sites have separate addresses | Existing explicit `graph_map` selects one structure; no guessed runtime identity or flattened child Nodes |
| Nested interpretation (AT-3) | Materializing graphs emit revision 2; children retain their own branch facts | R3 now recognizes revisions 1 and 2 across every published graph |
| CLI expansion | `agt describe --depth N` exposes API depth | Available to the entity's producer; Cordboard still only consumes published JSON |
| Expanded metadata gap | `expanded-subgraph-metadata` is retired | Real producer regressions assert it is absent; other gaps still surface |

Publication does not widen beta.4's Python LangGraph 1.2.10–1.2.11 support.
Dynamic interrupts, approval validity, effects and runtime invocation identity
remain outside static topology. Sentinel/branch facts remain experimental,
not promoted core fields. Recursive child diagrams and child-call attribution
are not implemented by this change.

The separate upstream follow-ups #163 and #164 are now closed through a
[compatibility-floor decision](https://github.com/agent-topology/agent-topology/blob/93e0ccc5a9245d7ff03911c496b2a28502ac700b/docs/decisions/0013-sentinel-branch-promotion-bar-unmet-compatibility-floor.md)
and a [version-evaluation policy](https://github.com/agent-topology/agent-topology/blob/main/docs/reference/langgraph-version-policy.md).
Those later source documents clarify guarantees and evaluation cadence;
they do not change the beta.4 artifacts or their accepted framework versions.

## Operator migration

The entity chooses expansion depth when producing its document, for example
`describe(compiled_graph, graph_id="main", depth=2)`, or
`agt describe path/to/export.py:graph --out topology.json --depth 2`.
The CLI target must be an import-safe module-level compiled object; it does
not invoke a factory. Entity publication stays entity-owned (ADR-0014/0016).

A positive-depth document contains multiple graph entries. Associate the
desired document address explicitly, then refresh the snapshot:

```sh
cord graph-map <alias> main <execution-graph-id>
cord sync <alias>
cord view <alias>
```

Positive-depth structure hashes change across the producer upgrade or an
expansion-depth change. Until explicit sync, an existing snapshot correctly
reports stale and withholds correlation. Depth-0 output is unchanged according
to the upstream release; this change does not claim a local beta.3/beta.4
byte-for-byte comparison. Interpretation facts are read from each fresh
document, not cached by structure hash.

## Verification

The measurement is consumer compatibility with published nested topology,
not execution correctness of an external entity. The input is a minimal
two-level compiled graph reusing one child at two call sites at each level,
with static interrupt declarations. It requires no model, credentials,
Docker, graph execution or long-running capture.

[`tests/test_topology_beta4.py`](../tests/test_topology_beta4.py) calls the
installed public producer at depths 0/1/2, producing 1/3/7 graph entries. It
checks schema validation, retained parent Nodes, unique resolvable child
references, confirmed R3 warnings in root/child graphs, unchanged HTTP document
consumption, explicit mapping, parent execution overlay, and stale-until-sync
behavior. [`tests/test_topology.py`](../tests/test_topology.py) also checks
known/unknown branch facts in both supported revisions and opacity for a
future revision. The overlay uses synthetic recorded Step evidence; no live
child runtime attribution is claimed.

```sh
uv sync --locked
uv run --locked pytest -q tests/test_topology.py tests/test_topology_beta4.py
uv run --locked --group proxy --group web-test pytest -q \
  -m 'not collector and not aegra and not langfuse'
```

External Collector/Aegra/Langfuse and real-entity acceptance are not requalified
by this change. The CLI depth command above follows upstream's release
contract; the local regression uses the Python API.

Executed result: the full service-free command above passed **661 tests,
36 deselected** in 107.07s on Python 3.12, including the six added regression
cases. Local Markdown references and `git diff --check` passed. No external
service acceptance run was executed.
