from concurrent.futures import ThreadPoolExecutor
import socket
import threading

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


# --- #19: two Subjects with deterministic overlap, and retry isolation ----

def test_two_subjects_execute_with_barrier_controlled_overlap_and_isolated_state():
    """A `threading.Barrier(2)` forces both Runs to be mid-Attempt at the same
    instant -- deterministic overlap, not a timing-dependent race -- while a
    per-Subject call ledger proves neither Run's Attempt is ever recorded
    under the other's Subject.
    """
    barrier = threading.Barrier(2)
    calls: dict[str, list] = {"first": [], "second": []}

    def model(source, tier, attempt):
        barrier.wait(timeout=5)
        calls[source].append((tier, attempt))
        return source  # an exact echo of this Run's own fixture source passes validation

    graph = build_graph(model)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, graph, subject="manual:first", source="first")
        second = executor.submit(run, graph, subject="manual:second", source="second")
        result_first, result_second = first.result(timeout=5), second.result(timeout=5)

    assert result_first.subject == "manual:first" and result_first.output == "first"
    assert result_second.subject == "manual:second" and result_second.output == "second"
    assert calls == {"first": [(Tier.FAST, 1)], "second": [(Tier.FAST, 1)]}


def test_retry_in_one_subject_leaves_the_concurrent_subjects_state_and_counters_unchanged():
    """One Subject retries (fails once, then passes at the same tier) while,
    with deterministic first-Attempt overlap via the barrier, a second
    Subject passes immediately. The extra Attempt belongs only to the
    retrying Subject's own ledger and result.
    """
    entry_barrier = threading.Barrier(2)
    calls: dict[str, list] = {"retrying": [], "passing": []}

    def model(source, tier, attempt):
        if attempt == 1:
            entry_barrier.wait(timeout=5)
        calls[source].append((tier, attempt))
        return "wrong" if source == "retrying" and attempt < 2 else source

    graph = build_graph(model)
    with ThreadPoolExecutor(max_workers=2) as executor:
        retrying = executor.submit(run, graph, subject="manual:retrying", source="retrying")
        passing = executor.submit(run, graph, subject="manual:passing", source="passing")
        retrying_result, passing_result = retrying.result(timeout=5), passing.result(timeout=5)

    assert calls["retrying"] == [(Tier.FAST, 1), (Tier.FAST, 2)]
    assert calls["passing"] == [(Tier.FAST, 1)]
    assert retrying_result.subject == "manual:retrying"
    assert passing_result.subject == "manual:passing"
    assert sequence(retrying_result) == [("fast", "failed"), ("fast", "passed")]
    assert sequence(passing_result) == [("fast", "passed")]
    assert retrying_result.output == "retrying"
    assert passing_result.output == "passing"


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
