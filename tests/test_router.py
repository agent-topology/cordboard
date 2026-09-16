"""Generic Signal -> Rule -> Assistant routing path (#16).

Covers the acceptance criteria directly: all three sources reach the same
`route_signal`, a declared Rule is pure configuration (no router code
changes), mapped input/Subject reach the Assistant, and unmatched/invalid
signals are recorded without the payload. Uses a fixed clock and fake
`execute()` -- no live Aegra service.
"""

from datetime import datetime, timezone
import json

import pytest

from cord_runtime import router
from cord_runtime.connections import add_connection
from cord_runtime.rules import add_rule
from cord_runtime.signals import file_signal, manual_signal, schedule_signal

FIXED_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def board(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    return tmp_path


@pytest.fixture
def fake_execute(monkeypatch):
    calls = []

    def _execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120):
        calls.append(dict(endpoint=endpoint, assistant=assistant, subject=subject,
                           graph_input=graph_input, timeout=timeout))
        return {"run_id": "r-1", "thread_id": "t-1", "status": "success", "values": {}}

    monkeypatch.setattr(router, "execute", _execute)
    return calls


def _add_rule(board_dir, signal_type, **overrides):
    rule = {
        "name": overrides.pop("name", f"{signal_type}-rule"),
        "signal_type": signal_type,
        "connection": "aegra-local",
        "assistant": "triage-graph",
        "subject": "subject",
        "match": {},
        "input": {"body": "body"},
    }
    rule.update(overrides)
    add_rule(board_dir, rule)
    return rule


# --- AC1: all three sources reach the same generic path ------------------

def test_manual_signal_routes_through_generic_path(board, fake_execute):
    _add_rule(board, "manual")
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "routed"
    assert outcome["result"]["status"] == "success"
    assert fake_execute[0]["subject"] == "manual:demo"
    assert fake_execute[0]["graph_input"] == {"body": "hi"}


def test_file_signal_routes_through_generic_path(board, fake_execute, tmp_path):
    _add_rule(board, "file")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"subject": "file:notes/api.md", "body": "changed"}), encoding="utf-8")
    outcome = router.route_signal(board, file_signal(event_path))
    assert outcome["status"] == "routed"
    assert fake_execute[0]["subject"] == "file:notes/api.md"


def test_schedule_signal_routes_through_generic_path(board, fake_execute):
    _add_rule(board, "schedule", subject="schedule", input={"body": "schedule"})
    signal = schedule_signal("daily-digest", FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "routed"
    assert fake_execute[0]["subject"] == "daily-digest"


# --- AC2: adding a Rule changes behavior with no router code change -------

def test_new_rule_for_existing_signal_type_reroutes_without_code_change(board, fake_execute):
    _add_rule(board, "manual", name="rule-a", assistant="graph-a", match={"kind": "ordinary"})
    add_connection(board, "other-conn", "http://127.0.0.1:9999")
    add_rule(board, {
        "name": "rule-b", "signal_type": "manual", "connection": "other-conn",
        "assistant": "graph-b", "subject": "subject", "match": {"kind": "special"}, "input": {},
    })
    signal = manual_signal({"subject": "manual:demo", "body": "hi", "kind": "special"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["rule"] == "rule-b"
    assert fake_execute[0]["endpoint"] == "http://127.0.0.1:9999"
    assert fake_execute[0]["assistant"] == "graph-b"


def test_model_free_assistant_target_requires_no_model_declaration(board, fake_execute):
    rule = _add_rule(board, "manual", assistant="opaque-graph")
    assert "model" not in rule and "provider" not in rule
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "routed"


# --- AC3: mapped input and required Subject reach the Assistant -----------

def test_input_mapping_extracts_nested_fields(board, fake_execute):
    add_rule(board, {
        "name": "nested", "signal_type": "manual", "connection": "aegra-local",
        "assistant": "triage-graph", "subject": "issue.url",
        "match": {}, "input": {"title": "issue.title", "author": "issue.author.login"},
    })
    signal = manual_signal({"issue": {"url": "manual:issue/1", "title": "Bug",
                                       "author": {"login": "octocat"}}}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "routed"
    assert fake_execute[0]["subject"] == "manual:issue/1"
    assert fake_execute[0]["graph_input"] == {"title": "Bug", "author": "octocat"}


# --- AC4: unmatched signals and invalid mappings are visible, no payload --

def test_unmatched_signal_is_recorded_without_payload(board):
    signal = manual_signal({"secret": "shh", "subject": "manual:demo"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome == {"status": "unmatched", "signal_id": signal["id"]}
    recorded = json.loads(router._unmatched_path(board).read_text(encoding="utf-8"))
    assert recorded == [{"signal_type": "manual", "signal_id": signal["id"],
                          "reason": "no rule declared for this signal"}]
    assert "shh" not in router._unmatched_path(board).read_text(encoding="utf-8")


def test_nonmatching_rule_for_a_different_signal_type_is_unmatched(board, fake_execute):
    _add_rule(board, "file")
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "unmatched"
    assert fake_execute == []


def test_match_filter_excludes_non_matching_payloads(board, fake_execute):
    _add_rule(board, "manual", match={"kind": "special"})
    signal = manual_signal({"subject": "manual:demo", "body": "hi", "kind": "ordinary"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "unmatched"
    assert fake_execute == []


def test_invalid_mapping_missing_subject_field_is_recorded_without_payload(board, fake_execute):
    add_rule(board, {
        "name": "bad-subject", "signal_type": "manual", "connection": "aegra-local",
        "assistant": "triage-graph", "subject": "missing.path", "match": {}, "input": {},
    })
    signal = manual_signal({"secret": "shh"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "invalid_mapping"
    assert outcome["rule"] == "bad-subject"
    assert fake_execute == []
    diagnostics = router._unmatched_path(board).read_text(encoding="utf-8")
    assert "shh" not in diagnostics


def test_invalid_mapping_unknown_connection_is_recorded(board, fake_execute):
    add_rule(board, {
        "name": "dangling", "signal_type": "manual", "connection": "missing-conn",
        "assistant": "triage-graph", "subject": "subject", "match": {}, "input": {},
    })
    signal = manual_signal({"subject": "manual:demo"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "invalid_mapping"
    assert fake_execute == []


def test_empty_subject_value_is_invalid_mapping(board, fake_execute):
    _add_rule(board, "manual")
    signal = manual_signal({"subject": "   ", "body": "hi"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "invalid_mapping"
    assert fake_execute == []


# --- execution failures downstream of a well-matched, well-mapped Rule ----

def test_unreachable_target_is_execution_failed_not_a_crash(board, monkeypatch):
    _add_rule(board, "manual")

    def raising_execute(*_args, **_kwargs):
        raise RuntimeError("Aegra API request failed; check service readiness and input contract")

    monkeypatch.setattr(router, "execute", raising_execute)
    signal = manual_signal({"subject": "manual:demo", "body": "hi", "secret": "shh"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "execution_failed"
    assert "shh" not in outcome["error"]


# --- AC5: manual execution continues to work through the unified route ----

def test_manual_execution_end_to_end_success(board, fake_execute):
    _add_rule(board, "manual", assistant="minimal-graph")
    signal = manual_signal({"subject": "manual:2026-09-16", "body": "go"}, now=FIXED_NOW)
    outcome = router.route_signal(board, signal)
    assert outcome["status"] == "routed"
    assert outcome["result"] == {"run_id": "r-1", "thread_id": "t-1", "status": "success", "values": {}}
