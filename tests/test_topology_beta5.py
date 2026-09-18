"""Replay published beta.5 producer output through Cordboard's public readers."""

import json
from typing import TypedDict

from agent_topology.langgraph import declare_children, describe
from agent_topology.spec import validate_document
from langgraph.graph import END, START, StateGraph
import pytest

from cord_runtime.topology import (
    CHANGED, UNCHANGED, VALID, check_freshness, graph_by_id,
    parallel_interrupt_warnings, refresh_snapshot,
)
from cord_runtime.viewer import correlate_topology
from cord_runtime.web.presentation import expansions, layout, run_topology
from test_topology import publisher


class State(TypedDict):
    value: int


def _identity(state):
    return {}


def _read(document, tmp_path):
    with publisher({"status": 200, "body": json.dumps(document).encode()}) as endpoint:
        return check_freshness(tmp_path, endpoint)


def test_repeated_join_declarations_are_now_a_drawable_document(tmp_path):
    builder = StateGraph(State)
    for name in ("a", "b", "sink"):
        builder.add_node(name, _identity)
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    # beta.4 emitted one join per declaration; the validator rejected the
    # duplicate id, so Cordboard could only report the document invalid.
    builder.add_edge(["a", "b"], "sink")
    builder.add_edge(["b", "a"], "sink")
    builder.add_edge("sink", END)
    document = describe(builder.compile())
    assert validate_document(document) == []
    assert len(document["graphs"][0]["structure"]["joins"]) == 1
    freshness = _read(document, tmp_path)
    assert freshness.reading.status == VALID
    result = correlate_topology(freshness, "execution")
    assert result["correlated"]
    joins = [edge for edge in layout(result["structure"])["edges"] if edge["kind"] == "AND join"]
    assert sorted((edge["source"], edge["target"]) for edge in joins) == [("a", "sink"), ("b", "sink")]


@pytest.fixture
def wrapped_graph():
    """A parent that calls its child through a plain function, not add_node."""
    leaf = StateGraph(State)
    for name in ("x", "y"):
        leaf.add_node(name, _identity)
        leaf.add_edge(START, name)
        leaf.add_edge(name, END)
    child = leaf.compile(interrupt_before=["y"])
    parent = StateGraph(State)
    parent.add_node("call", lambda state: child.invoke(state))
    parent.add_edge(START, "call")
    parent.add_edge("call", END)
    return parent, child


def test_undeclared_wrapped_child_stays_opaque(wrapped_graph):
    parent, _ = wrapped_graph
    document = describe(parent.compile(), depth=1)
    assert [graph["id"] for graph in document["graphs"]] == ["main"]
    assert parallel_interrupt_warnings(document) == []


def test_declared_wrapped_child_is_selectable_without_guessing(wrapped_graph, tmp_path):
    parent, child = wrapped_graph
    compiled = declare_children(parent.compile(), {"call": child})
    document = describe(compiled, depth=1)
    assert validate_document(document) == []
    assert [graph["id"] for graph in document["graphs"]] == ["main", "main:call"]
    call = next(node for node in graph_by_id(document, "main")["structure"]["nodes"] if node["id"] == "call")
    assert graph_by_id(document, call["subgraphId"]) is not None
    # The child's own static interrupt is now visible to R3, with its branch facts.
    warnings = parallel_interrupt_warnings(document)
    assert [(warning.graph_id, warning.interrupted_target_ids) for warning in warnings] == [("main:call", ("y",))]
    assert warnings[0].confirmed
    freshness = _read(document, tmp_path)
    assert freshness.reading.document == document
    # The child address is a document reference, never a runtime Graph identity.
    assert not correlate_topology(freshness, "execution")["correlated"]
    child_result = correlate_topology(freshness, "execution", {"main:call": "execution"})
    assert child_result["node_ids"] == ("__end__", "__start__", "x", "y")
    parent_result = correlate_topology(freshness, "execution", {"main": "execution"})
    diagram = run_topology(parent_result["structure"], {"steps": [
        {"node": "call", "outcome": "passed", "awaiting_resume": False},
    ]})
    assert diagram["unmatched_steps"] == []
    # The mapped parent expands its declared child from the same document (#85).
    assert parent_result["subgraphs"] == {"main:call": graph_by_id(document, "main:call")["structure"]}
    [entry] = expansions(parent_result["structure"], parent_result["subgraphs"])
    assert (entry["node"], entry["address"], entry["status"]) == ("call", "main:call", "resolved")
    assert [node["id"] for node in entry["diagram"]["nodes"]] == ["__end__", "__start__", "x", "y"]
    assert entry["children"] == []


def test_adopting_a_declaration_is_stale_until_explicit_sync(wrapped_graph, tmp_path):
    parent, child = wrapped_graph
    before = describe(parent.compile(), depth=1)
    after = describe(declare_children(parent.compile(), {"call": child}), depth=1)
    assert before["structureHash"] != after["structureHash"]
    response = {"status": 200, "body": json.dumps(before).encode()}
    with publisher(response) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        response["body"] = json.dumps(after).encode()
        assert check_freshness(tmp_path, endpoint).status == CHANGED
        refresh_snapshot(tmp_path, endpoint)
        assert check_freshness(tmp_path, endpoint).status == UNCHANGED
