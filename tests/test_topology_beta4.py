"""Replay published beta.4 producer output through Cordboard's public readers."""

import json
from typing import TypedDict

from agent_topology.langgraph import describe
from agent_topology.spec import validate_document
from langgraph.graph import END, START, StateGraph
import pytest

from cord_runtime.topology import (
    ABSENT, CHANGED, INVALID, NO_SNAPSHOT, UNCHANGED, VALID, FreshnessCheck, TopologyReading,
    check_freshness, graph_by_id, parallel_interrupt_warnings, refresh_snapshot,
)
from cord_runtime.viewer import build_catalog, correlate_topology
from cord_runtime.web.presentation import expansions, layout, run_topology
from test_topology import publisher


class State(TypedDict):
    value: int


def _identity(state):
    return {}


@pytest.fixture
def nested_graph():
    leaf = StateGraph(State)
    leaf.add_node("work", _identity)
    leaf.add_edge(START, "work")
    leaf.add_edge("work", END)
    compiled = leaf.compile()
    # Reuse the same compiled object at two call sites at each level.
    for names in (("a", "b"), ("left", "right")):
        parent = StateGraph(State)
        for name in names:
            parent.add_node(name, compiled)
            parent.add_edge(START, name)
            parent.add_edge(name, END)
        compiled = parent.compile(interrupt_before=[names[1]])
    return compiled


@pytest.mark.parametrize("depth, count", [(0, 1), (1, 3), (2, 7)])
def test_published_nested_documents_keep_addresses_and_confirm_branch_facts(nested_graph, depth, count, tmp_path):
    document = describe(nested_graph, graph_id="root:address", depth=depth)
    assert validate_document(document) == []
    assert len(document["graphs"]) == count
    assert not any(gap["code"] == "expanded-subgraph-metadata"
                   for gap in document["completeness"]["gaps"])
    root = graph_by_id(document, "root:address")
    assert {"left", "right"} <= {node["id"] for node in root["structure"]["nodes"]}
    references = [node["subgraphId"] for graph in document["graphs"]
                  for node in graph["structure"]["nodes"] if "subgraphId" in node]
    assert len(set(references)) == count - 1
    assert all(graph_by_id(document, address) is not None for address in references)
    warnings = parallel_interrupt_warnings(document)
    assert len(warnings) == (1 if depth == 0 else 3)
    assert all(warning.confirmed for warning in warnings)
    assert root["x-topology-interpretation"]["version"] == ("1" if depth == 0 else "2")
    with publisher({"status": 200, "body": json.dumps(document).encode()}) as endpoint:
        freshness = check_freshness(tmp_path, endpoint)
    assert freshness.reading.status == VALID
    assert freshness.reading.document == document
    # A child address is never guessed to be an execution Graph identity.
    if depth:
        assert not correlate_topology(freshness, "execution")["correlated"]
    for graph in document["graphs"]:
        result = correlate_topology(freshness, "execution", {graph["id"]: "execution"})
        assert result["correlated"]
        assert result["structure"] == graph["structure"]
    result = correlate_topology(freshness, "execution", {root["id"]: "execution"})
    diagram = run_topology(result["structure"], {"steps": [
        {"node": "left", "outcome": "passed", "awaiting_resume": False},
    ]})
    assert diagram["unmatched_steps"] == []
    assert next(node for node in diagram["nodes"] if node["id"] == "left")["status"] == "passed"


def test_expansion_is_stale_until_explicit_sync(nested_graph, tmp_path):
    shallow = describe(nested_graph, depth=0)
    expanded = describe(nested_graph, depth=2)
    assert shallow["structureHash"] != expanded["structureHash"]
    response = {"status": 200, "body": json.dumps(shallow).encode()}
    with publisher(response) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        response["body"] = json.dumps(expanded).encode()
        freshness = check_freshness(tmp_path, endpoint)
        assert freshness.status == CHANGED
        assert not correlate_topology(freshness, "execution", {"main": "execution"})["correlated"]
        refresh_snapshot(tmp_path, endpoint)
        freshness = check_freshness(tmp_path, endpoint)
        assert freshness.status == UNCHANGED
        assert correlate_topology(freshness, "execution", {"main": "execution"})["correlated"]


def _tree(entries):
    """Call site, address, status and nested call sites, without layout noise."""
    return [(e["node"], e["address"], e["status"], _tree(e["children"])) for e in entries]


def _flatten(entries):
    for entry in entries:
        yield entry
        yield from _flatten(entry["children"])


def test_mapped_root_exposes_and_expands_every_child_recursively(nested_graph, tmp_path):
    document = describe(nested_graph, graph_id="root", depth=2)
    with publisher({"status": 200, "body": json.dumps(document).encode()}) as endpoint:
        freshness = check_freshness(tmp_path, endpoint)
    result = correlate_topology(freshness, "execution", {"root": "execution"})
    children = [graph["id"] for graph in document["graphs"][1:]]
    # Keyed by document address, followed through `subgraphId` only (#85).
    assert result["subgraphs"] == {address: graph_by_id(document, address)["structure"] for address in children}
    entries = expansions(result["structure"], result["subgraphs"])
    leaf = ["__end__", "__start__", "work"]
    assert _tree(entries) == [
        ("left", "root:left", "resolved", [("a", "root:left:a", "resolved", []),
                                           ("b", "root:left:b", "resolved", [])]),
        ("right", "root:right", "resolved", [("a", "root:right:a", "resolved", []),
                                             ("b", "root:right:b", "resolved", [])]),
    ]
    for entry in _flatten(entries):
        # Expansion is the existing layout of that child, and never a Step overlay.
        assert entry["diagram"] == layout(result["subgraphs"][entry["address"]])
        assert all("status" not in node and "steps" not in node for node in entry["diagram"]["nodes"])
    # One compiled child reused at two call sites stays two expansions.
    left, right = entries
    assert left["diagram"] == right["diagram"]
    assert left["address"] != right["address"]
    keys = [entry["key"] for entry in _flatten(entries)]
    assert len(keys) == len(set(keys)) == 6
    assert [node["id"] for node in left["children"][0]["diagram"]["nodes"]] == leaf
    # Mapping a child exposes only that child's own descendants, never siblings.
    child = correlate_topology(freshness, "execution", {"root:left": "execution"})
    assert sorted(child["subgraphs"]) == ["root:left:a", "root:left:b"]


def test_parent_overlay_is_unchanged_and_child_nodes_get_no_steps(nested_graph, tmp_path):
    document = describe(nested_graph, graph_id="root", depth=2)
    with publisher({"status": 200, "body": json.dumps(document).encode()}) as endpoint:
        freshness = check_freshness(tmp_path, endpoint)
    result = correlate_topology(freshness, "execution", {"root": "execution"})
    run = {"steps": [{"node": "left", "outcome": "passed", "awaiting_resume": False},
                     {"node": "work", "outcome": "passed", "awaiting_resume": False}]}
    diagram = run_topology(result["structure"], run)
    by_id = {node["id"]: node for node in diagram["nodes"]}
    assert set(by_id) == {"__end__", "__start__", "left", "right"}
    assert by_id["left"]["status"] == "passed" and by_id["right"]["status"] == "not_observed"
    # A Step naming a child Node is not guessed onto that child.
    assert [step["node"] for step in diagram["unmatched_steps"]] == ["work"]


@pytest.mark.parametrize("status, reading", [
    (ABSENT, TopologyReading(status=ABSENT)),
    (INVALID, TopologyReading(status=INVALID, errors=("bad",))),
    (CHANGED, None),
    (NO_SNAPSHOT, None),
])
def test_withheld_topology_withholds_every_child(nested_graph, status, reading):
    document = describe(nested_graph, graph_id="root", depth=2)
    reading = reading or TopologyReading(status=VALID, document=document)
    endpoint = "http://127.0.0.1:1"
    # NO_SNAPSHOT is current, so only the missing mapping withholds it here.
    catalog = build_catalog({"demo": {"endpoint": endpoint, "graphs": ["execution"], "reachable": True,
                                      "graph_map": {}}},
                            {endpoint: FreshnessCheck(status=status, reading=reading)}, {})
    graph = catalog["graphs"][0]
    assert graph["topology_structure"] is None
    assert graph["topology_subgraphs"] == {}
    assert expansions(graph["topology_structure"], graph["topology_subgraphs"]) == []
    mapped = correlate_topology(FreshnessCheck(status=status, reading=reading), "execution",
                                {"root": "execution"})
    assert ("subgraphs" in mapped) == (status == NO_SNAPSHOT)
