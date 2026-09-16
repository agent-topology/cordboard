"""Unsafe vs. safe side-effect placement around interrupt()/resume (#15).

Model-free and gateway-free, like `interrupt_graph.py`. LangGraph re-runs a
paused node's function from the top on resume, so code placed *before*
`interrupt()` inside the same node executes once for the initial (paused)
invocation and again for the resumed one: two increments for one logical
approval. `docs/interrupt-resume.md` already warns that templates should
separate interrupt nodes from side effects for exactly this reason.

`build_unsafe_graph` puts the counter increment there on purpose, as a fixture
demonstrating the risk -- never a template. `build_safe_graph` moves the
effect into its own node, reached only once the resumed invocation completes
approval, and additionally guards it with a graph-owned idempotency set keyed
on `cord_resumed_from`: even a duplicate resume submission for the same
paused point (the platform's own concern, not this graph's, per #15) cannot
repeat the effect a second time. Side-effect idempotency stays graph-owned
either way; Cordboard only ever supplies the interrupt/resume span identity
(`cord_active`, `cord_resumed_from`), never the effect's own bookkeeping.
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
    count: int


def build_unsafe_graph(checkpointer: BaseCheckpointSaver, counter: list[int]):
    """`counter[0]` sits before `interrupt()` in one node: replayed on resume."""

    def approve_and_charge(state: State, config: RunnableConfig) -> dict:
        values = config.get("configurable", {})
        active = values.get("cord_active")
        resumed_from = values.get("cord_resumed_from")
        scope = (active.step("approve_and_charge", StepOutcome.PASSED,
                              resumed_from=resumed_from, pause=(GraphInterrupt,))
                  if active else nullcontext())
        with scope as step:
            with (step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext()):
                counter[0] += 1  # unsafe: re-executed by the resumed invocation too
                decision = interrupt({"question": "approve?"})
        return {"approved": bool(decision), "count": counter[0]}

    builder = StateGraph(State)
    builder.add_node("approve_and_charge", approve_and_charge)
    builder.add_edge(START, "approve_and_charge")
    builder.add_edge("approve_and_charge", END)
    return builder.compile(checkpointer=checkpointer)


def build_safe_graph(checkpointer: BaseCheckpointSaver, counter: list[int]):
    """The effect lives in its own node, reached only after approval, guarded
    by a `resumed_from`-keyed idempotency set the graph itself owns."""
    applied: set[str] = set()

    def approve(state: State, config: RunnableConfig) -> dict:
        values = config.get("configurable", {})
        active = values.get("cord_active")
        resumed_from = values.get("cord_resumed_from")
        scope = (active.step("approve", StepOutcome.PASSED,
                              resumed_from=resumed_from, pause=(GraphInterrupt,))
                  if active else nullcontext())
        with scope as step:
            with (step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext()):
                decision = interrupt({"question": "approve?"})
        return {"approved": bool(decision)}

    def charge(state: State, config: RunnableConfig) -> dict:
        values = config.get("configurable", {})
        active = values.get("cord_active")
        resumed_from = values.get("cord_resumed_from")
        scope = (active.step("charge", StepOutcome.PASSED, resumed_from=resumed_from)
                  if active else nullcontext())
        with scope as step:
            with (step.attempt(1, outcome=AttemptOutcome.PASSED) if step else nullcontext()):
                if resumed_from not in applied:
                    applied.add(resumed_from)
                    counter[0] += 1
        return {"count": counter[0]}

    builder = StateGraph(State)
    builder.add_node("approve", approve)
    builder.add_node("charge", charge)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", "charge")
    builder.add_edge("charge", END)
    return builder.compile(checkpointer=checkpointer)
