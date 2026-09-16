"""A tiny independent workflow that pauses for approval and resumes.

Model-free and gateway-free, like `opaque_graph.py`. It demonstrates the
Step/Attempt span boundary rule from ADR-0008's interrupt addendum: the
public seam is LangGraph's own `interrupt()`/`Command(resume=...)` contract
(Aegra / LangGraph owns interrupt execution and checkpointing per
ARCHITECTURE.md), not a callback from `agent-workflow-core`'s observer
protocol. The graph process supplies `cord_runtime` instrumentation directly
around its own node, the same pattern `opaque_graph.py` already uses.
"""

from contextlib import nullcontext
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from cord_runtime.execution import AttemptOutcome, StepOutcome


class State(TypedDict, total=False):
    approved: bool


def build_graph(checkpointer: BaseCheckpointSaver):
    def approve(state: State, config: RunnableConfig) -> dict:
        values = config.get("configurable", {})
        active = values.get("cord_active")
        resumed_from = values.get("cord_resumed_from")
        scope = (active.step("approve", StepOutcome.PASSED, resumed_from=resumed_from, pause=(GraphInterrupt,))
                  if active else nullcontext())
        with scope as step:
            with (step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext()):
                decision = interrupt({"question": "approve?"})
        return {"approved": bool(decision)}

    builder = StateGraph(State)
    builder.add_node("approve", approve)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    return builder.compile(checkpointer=checkpointer)
