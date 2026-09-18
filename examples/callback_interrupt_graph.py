"""A tiny interrupt/resume graph with zero Cordboard imports (#89).

Unlike `examples/interrupt_graph.py` (#28's graph-code-instrumented pattern,
which passes a live `cord_runtime.execution.Run`/`Step` through
`config["configurable"]`), this graph's "approve" node calls LangGraph's
public `interrupt()` and nothing else -- no `cord_runtime` import anywhere.
An externally attached `cord_langgraph_callbacks.CordCallbackHandler` is the
only thing that can turn this into a recorded, awaiting-approval Step (see
tests/test_langgraph_callbacks.py).
"""

from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class State(TypedDict, total=False):
    approved: bool


def build_graph(checkpointer: BaseCheckpointSaver):
    def approve(state: State) -> dict:
        decision = interrupt({"question": "approve?"})
        return {"approved": bool(decision)}

    builder = StateGraph(State)
    builder.add_node("approve", approve)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    return builder.compile(checkpointer=checkpointer, name="callback-interrupt-fixture")
