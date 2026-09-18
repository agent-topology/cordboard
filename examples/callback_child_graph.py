"""A tiny two-level graph with zero Cordboard imports (#89).

Unlike every other graph in this directory, nothing here calls
`cord_runtime.execution`: the outer node "prepare" runs, then a compiled
child graph is attached directly as the "collect" node -- LangGraph's own
subgraph-as-node pattern -- so both levels emit ordinary `graph:step:N`
chain events with nested `langgraph_checkpoint_ns`. Recording, if any
happens at all, comes entirely from an externally attached
`cord_langgraph_callbacks.CordCallbackHandler` (see
tests/test_langgraph_callbacks.py), demonstrating the issue's outcome: an
entity can record Run/Step spans for a graph it hosts without changing that
graph's code.
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    prepared: bool
    collected: bool


def _build_child():
    def gather(state: State) -> dict:
        return {"collected": True}

    builder = StateGraph(State)
    builder.add_node("gather", gather)
    builder.add_edge(START, "gather")
    builder.add_edge("gather", END)
    return builder.compile(name="child")


def build_graph():
    def prepare(state: State) -> dict:
        return {"prepared": True}

    builder = StateGraph(State)
    builder.add_node("prepare", prepare)
    builder.add_node("collect", _build_child())
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "collect")
    builder.add_edge("collect", END)
    return builder.compile(name="callback-child-fixture")
