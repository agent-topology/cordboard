"""Chaining Run completions through run.finished Signals (#18).

Two tiny Assistants ("graph-a", "graph-b") stand in for the two tiny graphs
in the issue's own Verification note; a fake `execute()` gives each a
distinct run_id per call so a chain can be told apart hop by hop, without a
live Aegra service or any real model call. `test_rules.py` covers the
static, add-time half of self-loop rejection; this file covers the runtime
half: outcome filtering, depth-bounded cyclic Rules, and cascade dedupe.
"""

from datetime import datetime, timezone
import itertools
import json

import pytest

from cord_runtime import router
from cord_runtime.connections import add_connection
from cord_runtime.rules import add_rule
from cord_runtime.signals import manual_signal, run_finished_signal

FIXED_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def board(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    return tmp_path


@pytest.fixture
def fake_execute(monkeypatch):
    calls = []
    counter = itertools.count(1)

    def _execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                 caused_by_run_id=None, cascade_depth=0):
        run_id = f"r-{assistant}-{next(counter)}"
        calls.append(dict(endpoint=endpoint, assistant=assistant, subject=subject,
                           graph_input=graph_input, timeout=timeout,
                           caused_by_run_id=caused_by_run_id, cascade_depth=cascade_depth,
                           run_id=run_id))
        return {"run_id": run_id, "thread_id": f"t-{run_id}", "status": "success", "values": {}}

    monkeypatch.setattr(router, "execute", _execute)
    return calls


def _add_start_rule(board_dir, assistant="graph-a"):
    add_rule(board_dir, {
        "name": "start", "signal_type": "manual", "connection": "aegra-local",
        "assistant": assistant, "subject": "subject", "match": {}, "input": {},
    })


# --- AC1: a matching completion starts the intended Assistant with a new ---
# --- Run/trace and the correct causing Run identifier -----------------------

def test_completion_of_graph_a_starts_graph_b_with_the_causing_run_id(board, fake_execute):
    _add_start_rule(board)
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a", "status": "success"},
    })
    signal = manual_signal({"subject": "urn:demo"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal, now=FIXED_NOW)

    assert outcome["status"] == "routed"
    assert len(fake_execute) == 2
    first, second = fake_execute
    assert first["assistant"] == "graph-a"
    assert first["caused_by_run_id"] is None
    assert first["cascade_depth"] == 0
    assert second["assistant"] == "graph-b"
    assert second["caused_by_run_id"] == first["run_id"]
    assert second["cascade_depth"] == 1

    cascade = outcome["cascade"]
    assert cascade["status"] == "routed"
    assert cascade["rule"] == "cascade-b"
    assert cascade["result"]["run_id"] == second["run_id"]
    # graph-b's own completion cascades again but nothing declares a Rule for it.
    assert cascade["cascade"]["status"] == "unmatched"


# --- AC2: Subject inheritance and explicit override -------------------------

def test_cascade_rule_inherits_the_completed_runs_subject_by_default(board, fake_execute):
    _add_start_rule(board)
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"},
    })
    signal = manual_signal({"subject": "urn:demo"}, now=FIXED_NOW)
    router.route_signal(board, signal, now=FIXED_NOW)
    first, second = fake_execute
    assert second["subject"] == first["subject"] == "urn:demo"


def test_cascade_rule_can_explicitly_override_the_inherited_subject(board, fake_execute):
    _add_start_rule(board)
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"}, "subject": "run_id",
    })
    signal = manual_signal({"subject": "urn:demo"}, now=FIXED_NOW)
    router.route_signal(board, signal, now=FIXED_NOW)
    first, second = fake_execute
    assert second["subject"] == first["run_id"]
    assert second["subject"] != first["subject"]


# --- AC3: missing when, self-loop, and excessive cascade depth are ----------
# --- prevented with actionable diagnostics -----------------------------------
# (missing `when` and a declared self-loop are rejected at `add_rule` time;
# see tests/test_rules.py. This covers the runtime cascade-depth bound.)

def test_cascade_depth_at_the_maximum_is_blocked_before_matching_any_rule(board, fake_execute):
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"},
    })
    signal = run_finished_signal(run_id="r-deep", subject="urn:demo", connection="aegra-local",
                                  assistant="graph-a", status="success",
                                  cascade_depth=router.MAX_CASCADE_DEPTH)
    outcome = router.route_signal(board, signal, now=FIXED_NOW)
    assert outcome == {"status": "cascade_depth_exceeded", "signal_id": signal["id"],
                        "depth": router.MAX_CASCADE_DEPTH}
    assert fake_execute == []
    diagnostics = json.loads(router._unmatched_path(board).read_text(encoding="utf-8"))
    assert diagnostics == [{"signal_type": "run.finished", "signal_id": signal["id"],
                             "reason": f"cascade depth {router.MAX_CASCADE_DEPTH} is at the "
                                       f"maximum of {router.MAX_CASCADE_DEPTH}"}]


def test_a_longer_cascade_cycle_is_bounded_by_max_depth_not_infinite(board, fake_execute):
    """A -> B -> A -> ... is not a statically declared self-loop (#18's
    depth-boundary fixture): each Rule's own 'when' names the *other*
    assistant, so add_rule accepts both, and only the runtime depth bound
    stops the chain."""
    _add_start_rule(board, assistant="graph-a")
    add_rule(board, {
        "name": "a-to-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"},
    })
    add_rule(board, {
        "name": "b-to-a", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-a", "when": {"assistant": "graph-b"},
    })
    signal = manual_signal({"subject": "urn:demo"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal, now=FIXED_NOW)

    assert outcome["status"] == "routed"
    # the root Run plus exactly MAX_CASCADE_DEPTH cascade hops, then it stops.
    assert len(fake_execute) == 1 + router.MAX_CASCADE_DEPTH

    node = outcome
    while "cascade" in node:
        node = node["cascade"]
    assert node["status"] == "cascade_depth_exceeded"
    assert node["depth"] == router.MAX_CASCADE_DEPTH


# --- AC4: duplicate completion delivery does not create duplicate child -----
# --- execution, through the established #17 dedupe path ---------------------

def test_duplicate_completion_delivery_does_not_start_a_second_child_run(board, fake_execute):
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"},
    })
    finished = run_finished_signal(run_id="r-a-1", subject="urn:demo", connection="aegra-local",
                                    assistant="graph-a", status="success")
    first = router.route_signal(board, finished, now=FIXED_NOW)
    second = router.route_signal(board, finished, now=FIXED_NOW)
    assert first["status"] == "routed"
    assert second == {"status": "duplicate", "signal_id": finished["id"], "rule": "cascade-b"}
    assert len(fake_execute) == 1


def test_redelivered_completion_after_a_restart_still_sees_the_same_claim(board, fake_execute):
    """A restart is just a fresh call against the same on-disk board_dir."""
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"},
    })
    finished = run_finished_signal(run_id="r-a-1", subject="urn:demo", connection="aegra-local",
                                    assistant="graph-a", status="success")
    router.route_signal(board, finished, now=FIXED_NOW)
    outcome = router.route_signal(board, finished, now=FIXED_NOW)
    assert outcome["status"] == "duplicate"


# --- AC5: completion outcome filtering avoids starting a child for an -------
# --- unmatched terminal result -----------------------------------------------

def test_when_condition_on_outcome_excludes_a_non_matching_terminal_result(board, fake_execute):
    add_rule(board, {
        "name": "only-failed", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a", "status": "failed"},
    })
    finished = run_finished_signal(run_id="r-a-1", subject="urn:demo", connection="aegra-local",
                                    assistant="graph-a", status="success")
    outcome = router.route_signal(board, finished, now=FIXED_NOW)
    assert outcome["status"] == "unmatched"
    assert fake_execute == []


# --- a pause never fabricates a completion -----------------------------------

def test_a_waiting_result_never_emits_a_run_finished_cascade(board, fake_execute, monkeypatch):
    _add_start_rule(board)
    add_rule(board, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": "aegra-local",
        "assistant": "graph-b", "when": {"assistant": "graph-a"},
    })

    def waiting_execute(*_args, **_kwargs):
        return {"run_id": "r-1", "thread_id": "t-1", "status": "waiting"}

    monkeypatch.setattr(router, "execute", waiting_execute)
    signal = manual_signal({"subject": "urn:demo"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal, now=FIXED_NOW)
    assert outcome["status"] == "routed"
    assert "cascade" not in outcome
