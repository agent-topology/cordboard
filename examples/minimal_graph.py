"""A fetch → draft → verify graph with a bounded draft Attempt subgraph."""

from dataclasses import asdict, dataclass
from enum import Enum
import json
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context


class StepOutcome(Enum):
    PASSED = "passed"
    REPAIRED = "repaired"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"
    HALTED = "halted"


class AttemptOutcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"


class Tier(Enum):
    FAST = "fast"
    DEEP = "deep"


@dataclass(frozen=True)
class Verdict:
    violated_rules: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.violated_rules


@dataclass(frozen=True)
class Attempt:
    number: int
    tier: Tier
    outcome: AttemptOutcome
    verdict: Verdict

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AttemptOutcome):
            raise TypeError("Attempt requires an AttemptOutcome")


@dataclass(frozen=True)
class Step:
    node: str
    outcome: StepOutcome
    attempts: tuple[Attempt, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, StepOutcome):
            raise TypeError("Step requires a StepOutcome")


@dataclass(frozen=True)
class RunResult:
    subject: str
    output: str | None
    steps: tuple[Step, ...]

    @property
    def escalation_count(self) -> int:
        return sum(
            attempt.outcome is AttemptOutcome.ESCALATED
            for step in self.steps
            for attempt in step.attempts
        )


class Model(Protocol):
    def __call__(self, source: str, tier: Tier, attempt: int) -> str: ...


@dataclass(frozen=True)
class ControlledResponses:
    """Index by per-execution Attempt number; never consume a shared iterator."""

    responses: tuple[str, ...]

    def __call__(self, source: str, tier: Tier, attempt: int) -> str:
        if attempt > len(self.responses):
            raise ValueError("Controlled response fixture exhausted")
        return self.responses[attempt - 1]


def validate(output: str, source: str) -> Verdict:
    """Require an exact copy of the fixed source, without trailing punctuation."""
    rules = []
    if output.removesuffix(".") != source:
        rules.append("R1: exact source")
    if output.endswith("."):
        rules.append("R5: no trailing period")
    return Verdict(tuple(rules))


class DraftState(TypedDict):
    source: str
    attempts: tuple[Attempt, ...]
    candidate: str
    output: str | None
    outcome: StepOutcome
    next_attempt: bool


class GraphInput(TypedDict):
    subject: str
    source: str


class GraphState(GraphInput):
    steps: tuple[Step, ...]
    output: str | None


# Graph-owned policy: retry once at fast, then escalate once to deep.
TIERS = (Tier.FAST, Tier.FAST, Tier.DEEP)


def build_graph(model: Model):
    """Compile reusable graphs with no checkpointer or mutable execution closure."""

    def generate(state: DraftState) -> dict:
        index = len(state["attempts"])
        return {"candidate": model(state["source"], TIERS[index], index + 1)}

    def assess(state: DraftState) -> dict:
        candidate = state["candidate"]
        verdict = validate(candidate, state["source"])
        repaired = candidate.removesuffix(".")
        repair_passed = not verdict.passed and validate(repaired, state["source"]).passed
        index = len(state["attempts"])
        retry = not (verdict.passed or repair_passed) and index + 1 < len(TIERS)

        # This routing decision declares the *current* Attempt's disposition.
        if verdict.passed:
            attempt_outcome = AttemptOutcome.PASSED
        elif retry and TIERS[index + 1] is not TIERS[index]:
            attempt_outcome = AttemptOutcome.ESCALATED
        else:
            attempt_outcome = AttemptOutcome.FAILED

        if verdict.passed:
            outcome, output = StepOutcome.PASSED, candidate
        elif repair_passed:
            outcome, output = StepOutcome.REPAIRED, repaired
        else:
            outcome, output = StepOutcome.FAILED, None

        return {
            "attempts": state["attempts"] + (
                Attempt(index + 1, TIERS[index], attempt_outcome, verdict),
            ),
            "outcome": outcome,
            "output": output,
            "next_attempt": retry,
        }

    def route(state: DraftState) -> Literal["generate", "done"]:
        return "generate" if state["next_attempt"] else "done"

    attempts = StateGraph(DraftState)
    attempts.add_node("generate", generate)
    attempts.add_node("assess", assess)
    attempts.add_edge(START, "generate")
    attempts.add_edge("generate", "assess")
    attempts.add_conditional_edges("assess", route, {"generate": "generate", "done": END})
    attempt_graph = attempts.compile()

    def fetch(state: GraphInput) -> dict:
        # Subject is opaque: require presence, but do not parse or normalize it.
        if not isinstance(state["subject"], str) or not state["subject"].strip():
            raise ValueError("Subject must be a non-empty string")
        if not isinstance(state["source"], str) or not state["source"] or state["source"].endswith("."):
            raise ValueError("Source must be non-empty and have no trailing period")
        return {"steps": (Step("fetch", StepOutcome.PASSED),), "output": None}

    def draft(state: GraphState) -> dict:
        result = attempt_graph.invoke({"source": state["source"], "attempts": ()})
        return {
            "steps": state["steps"] + (
                Step("draft", result["outcome"], result["attempts"]),
            ),
            "output": result["output"],
        }

    def verify(state: GraphState) -> dict:
        output = state["output"]
        passed = output is not None and validate(output, state["source"]).passed
        outcome = StepOutcome.PASSED if passed else StepOutcome.FAILED
        return {"steps": state["steps"] + (Step("verify", outcome),)}

    graph = StateGraph(GraphState, input_schema=GraphInput)
    graph.add_node("fetch", fetch)
    graph.add_node("draft", draft)
    graph.add_node("verify", verify)
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "draft")
    graph.add_edge("draft", "verify")
    graph.add_edge("verify", END)
    return graph.compile()


def run(graph, *, subject: str, source: str = "hello") -> RunResult:
    # Even an inherited LangSmith tracing setting must not export this example.
    with tracing_context(enabled=False):
        state = graph.invoke({"subject": subject, "source": source})
    return RunResult(state["subject"], state["output"], state["steps"])


def main() -> None:
    graph = build_graph(ControlledResponses(("wrong", "still wrong", "hello")))
    result = run(graph, subject="github:issue/4")
    payload = asdict(result) | {"escalation_count": result.escalation_count}
    print(json.dumps(payload, default=lambda value: value.value, indent=2))


if __name__ == "__main__":
    main()
