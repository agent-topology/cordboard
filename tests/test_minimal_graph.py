from concurrent.futures import ThreadPoolExecutor
import socket

import pytest

from examples.minimal_graph import (
    Attempt,
    AttemptOutcome,
    ControlledResponses,
    Step,
    StepOutcome,
    Tier,
    Verdict,
    build_graph,
    run,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The example must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def passed():
    return ControlledResponses(("hello",))


@pytest.fixture
def repaired():
    return ControlledResponses(("hello.",))


@pytest.fixture
def failed():
    return ControlledResponses(("wrong", "wrong", "wrong"))


def sequence(result):
    return [(a.tier.value, a.outcome.value) for a in result.steps[1].attempts]


def test_escalation_has_one_draft_step_and_one_escalation():
    calls = []
    responses = ControlledResponses(("wrong", "still wrong", "hello"))

    def model(source, tier, attempt):
        calls.append((source, tier, attempt))
        return responses(source, tier, attempt)

    graph = build_graph(model)
    result = run(graph, subject="github:issue/4")
    assert sequence(result) == [("fast", "failed"), ("fast", "escalated"), ("deep", "passed")]
    assert result.escalation_count == 1
    assert result.output == "hello"
    assert result.subject == "github:issue/4"
    assert [(s.node, s.outcome) for s in result.steps] == [
        ("fetch", StepOutcome.PASSED),
        ("draft", StepOutcome.PASSED),
        ("verify", StepOutcome.PASSED),
    ]
    assert [a.number for a in result.steps[1].attempts] == [1, 2, 3]
    assert not result.steps[0].attempts and not result.steps[2].attempts
    assert calls == [("hello", Tier.FAST, 1), ("hello", Tier.FAST, 2), ("hello", Tier.DEEP, 3)]


def test_passed_stops_after_first_attempt(passed):
    result = run(build_graph(passed), subject="manual:passed")
    assert result.steps[1].outcome is StepOutcome.PASSED
    assert result.output == "hello"
    assert sequence(result) == [("fast", "passed")]
    assert result.escalation_count == 0


def test_deterministic_repair_preserves_original_verdict(repaired):
    result = run(build_graph(repaired), subject="manual:repaired")
    assert result.steps[1].outcome is StepOutcome.REPAIRED
    assert result.steps[2].outcome is StepOutcome.PASSED
    assert result.output == "hello"
    assert sequence(result) == [("fast", "failed")]
    assert result.steps[1].attempts[0].verdict == Verdict(("R5: no trailing period",))
    assert result.escalation_count == 0


def test_terminal_failure_is_bounded(failed):
    result = run(build_graph(failed), subject="manual:failed")
    assert sequence(result) == [("fast", "failed"), ("fast", "escalated"), ("deep", "failed")]
    assert result.steps[1].outcome is StepOutcome.FAILED
    assert result.steps[2].outcome is StepOutcome.FAILED
    assert result.output is None
    assert result.escalation_count == 1


def test_same_tier_retry_success_is_not_escalation():
    result = run(build_graph(ControlledResponses(("wrong", "hello"))), subject="manual:retry")
    assert sequence(result) == [("fast", "failed"), ("fast", "passed")]
    assert result.steps[1].outcome is StepOutcome.PASSED
    assert result.output == "hello"
    assert result.escalation_count == 0


def test_repair_does_not_accept_wrong_content():
    result = run(build_graph(ControlledResponses(("wrong.",) * 3)), subject="manual:bad-repair")
    assert result.steps[1].outcome is StepOutcome.FAILED
    assert result.output is None
    assert result.steps[1].attempts[0].verdict.violated_rules == (
        "R1: exact source", "R5: no trailing period",
    )


def test_reruns_do_not_inherit_attempts_output_or_subject():
    graph = build_graph(ControlledResponses(("wrong", "wrong", "hello")))
    first = run(graph, subject="github:issue/4")
    failure = run(graph, subject="file:note", source="other")
    repeated = run(graph, subject="github:issue/4")
    assert first == repeated
    assert first.steps is not repeated.steps
    assert first.steps[1].attempts is not repeated.steps[1].attempts
    assert failure.subject == "file:note"
    assert failure.output is None
    assert failure.steps[1].outcome is StepOutcome.FAILED
    assert [a.number for a in failure.steps[1].attempts] == [1, 2, 3]


def test_concurrent_runs_share_no_execution_state():
    graph = build_graph(ControlledResponses(("wrong", "hello")))
    subjects = ["manual:first", "manual:second"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda subject: run(graph, subject=subject), subjects))
    assert [result.subject for result in results] == subjects
    assert all(sequence(result) == [("fast", "failed"), ("fast", "passed")] for result in results)


@pytest.mark.parametrize("subject", [None, "", "   ", 4])
def test_subject_is_required(passed, subject):
    with pytest.raises(ValueError, match="Subject"):
        run(build_graph(passed), subject=subject)


def test_missing_subject_cannot_start(passed):
    with pytest.raises(TypeError, match="subject"):
        run(build_graph(passed))


def test_subject_is_opaque_and_preserved(passed):
    subject = "custom:An/Opaque%20Target?x=1"
    assert run(build_graph(passed), subject=subject).subject == subject


def test_outcome_vocabularies_are_closed_and_distinct():
    assert {value.value for value in StepOutcome} == {
        "passed", "repaired", "failed", "awaiting_approval", "halted",
    }
    assert {value.value for value in AttemptOutcome} == {"passed", "failed", "escalated"}
    assert StepOutcome.PASSED != AttemptOutcome.PASSED
    with pytest.raises(ValueError):
        StepOutcome("escalated")
    with pytest.raises(ValueError):
        AttemptOutcome("repaired")


@pytest.mark.parametrize("outcome", list(AttemptOutcome) + ["passed"])
def test_step_rejects_attempt_outcomes(outcome):
    with pytest.raises(TypeError, match="StepOutcome"):
        Step("draft", outcome)


@pytest.mark.parametrize("outcome", list(StepOutcome) + ["failed"])
def test_attempt_rejects_step_outcomes(outcome):
    with pytest.raises(TypeError, match="AttemptOutcome"):
        Attempt(1, Tier.FAST, outcome, Verdict(()))


def test_fixture_exhaustion_is_an_error_not_a_success():
    with pytest.raises(ValueError, match="fixture exhausted"):
        run(build_graph(ControlledResponses(("wrong",))), subject="manual:short")


def test_inherited_tracing_is_disabled(passed, monkeypatch):
    from langsmith import get_tracing_context

    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    def model(source, tier, attempt):
        assert get_tracing_context()["enabled"] is False
        return passed(source, tier, attempt)

    assert run(build_graph(model), subject="manual:offline").output == "hello"
