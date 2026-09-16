"""Two concurrently pending interrupts on one Thread, and a counter (#44).

Model-free and gateway-free, like `interrupt_graph.py`. Demonstrates the fact
#44's contract rests on: `langgraph.types.Interrupt` (pinned
``langgraph==1.2.11``) assigns each interrupt a deterministic ``.id`` derived
from its own task's namespace, so two parallel branches that each call
`interrupt()` in the same superstep produce two distinct, independently
addressable interrupts -- not one ambiguous pause. ``Command(resume={...})``
accepts a mapping of interrupt id to resume value for exactly this case
(`langgraph.types.Command.resume`'s own documented contract).

``charge`` only runs once both branches resume, guarded by a graph-owned
idempotency set keyed on *both* branches' `cord_resumed_from` span ids
together, the same technique `replay_counter_graph.py` uses for one
interrupt -- extended here so a duplicate resume submission for either
branch alone still cannot repeat the effect twice.
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
    approve_a: bool
    approve_b: bool
    count: int


def _branch_resumed_from(config: RunnableConfig, name: str) -> str | None:
    mapping = (config.get("configurable") or {}).get("cord_resumed_from") or {}
    return mapping.get(name) if isinstance(mapping, dict) else None


def build_graph(checkpointer: BaseCheckpointSaver, counter: list[int]):
    applied: set[tuple[str | None, str | None]] = set()

    def _approve(name: str, question: str, config: RunnableConfig) -> dict:
        values = config.get("configurable", {})
        active = values.get("cord_active")
        resumed_from = _branch_resumed_from(config, name)
        scope = (active.step(name, StepOutcome.PASSED, resumed_from=resumed_from, pause=(GraphInterrupt,))
                  if active else nullcontext())
        with scope as step:
            with (step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext()):
                decision = interrupt({"question": question})
        return {name: bool(decision)}

    def approve_a(state: State, config: RunnableConfig) -> dict:
        return _approve("approve_a", "approve a?", config)

    def approve_b(state: State, config: RunnableConfig) -> dict:
        return _approve("approve_b", "approve b?", config)

    def charge(state: State, config: RunnableConfig) -> dict:
        values = config.get("configurable", {})
        active = values.get("cord_active")
        key = (_branch_resumed_from(config, "approve_a"), _branch_resumed_from(config, "approve_b"))
        scope = active.step("charge", StepOutcome.PASSED) if active else nullcontext()
        with scope as step:
            with (step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext()):
                if key not in applied:
                    applied.add(key)
                    counter[0] += 1
        return {"count": counter[0]}

    builder = StateGraph(State)
    builder.add_node("approve_a", approve_a)
    builder.add_node("approve_b", approve_b)
    builder.add_node("charge", charge)
    builder.add_edge(START, "approve_a")
    builder.add_edge(START, "approve_b")
    builder.add_edge("approve_a", "charge")
    builder.add_edge("approve_b", "charge")
    builder.add_edge("charge", END)
    return builder.compile(checkpointer=checkpointer)
