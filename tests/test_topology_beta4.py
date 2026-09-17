"""Replay published beta.4 producer output through Cordboard's public readers."""

import json
from typing import TypedDict

from agent_topology.langgraph import describe
from agent_topology.spec import validate_document
from langgraph.graph import END, START, StateGraph
import pytest

from cord_runtime.topology import (
    CHANGED, UNCHANGED, VALID, check_freshness, graph_by_id,
    parallel_interrupt_warnings, refresh_snapshot,
)
from cord_runtime.viewer import correlate_topology
from cord_runtime.web.presentation import run_topology
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
