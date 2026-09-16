"""Declarative Rule storage (#16): configuration-only expansion, no code changes."""

import json

import pytest

from cord_runtime.rules import InvalidRule, add_rule, load_rules, rules_path

VALID_RULE = {
    "name": "issue-triage",
    "signal_type": "file",
    "connection": "aegra-local",
    "assistant": "triage-graph",
    "subject": "issue.url",
    "match": {"issue.repo": "cordboard-proto"},
    "input": {"body": "issue.body"},
}


def test_add_and_load_round_trip(tmp_path):
    add_rule(tmp_path, VALID_RULE)
    assert load_rules(tmp_path) == [VALID_RULE]
    assert rules_path(tmp_path).is_file()


def test_load_rules_defaults_to_empty_list(tmp_path):
    assert load_rules(tmp_path) == []


def test_add_rule_fills_optional_match_and_input_defaults(tmp_path):
    add_rule(tmp_path, {
        "name": "manual-echo", "signal_type": "manual", "connection": "aegra-local",
        "assistant": "echo-graph", "subject": "subject",
    })
    assert load_rules(tmp_path)[0]["match"] == {}
    assert load_rules(tmp_path)[0]["input"] == {}


@pytest.mark.parametrize("signal_type", ["manual", "file", "schedule"])
def test_add_rule_accepts_all_declared_signal_types(tmp_path, signal_type):
    add_rule(tmp_path, {**VALID_RULE, "name": f"rule-{signal_type}", "signal_type": signal_type})
    assert load_rules(tmp_path)[-1]["signal_type"] == signal_type


def test_add_rule_rejects_unknown_signal_type(tmp_path):
    with pytest.raises(InvalidRule, match="signal_type"):
        add_rule(tmp_path, {**VALID_RULE, "signal_type": "webhook"})


def test_add_rule_rejects_missing_name(tmp_path):
    rule = dict(VALID_RULE)
    del rule["name"]
    with pytest.raises(InvalidRule, match="name"):
        add_rule(tmp_path, rule)


def test_add_rule_rejects_missing_subject(tmp_path):
    rule = dict(VALID_RULE)
    del rule["subject"]
    with pytest.raises(InvalidRule, match="subject"):
        add_rule(tmp_path, rule)


def test_add_rule_rejects_non_string_input_mapping_values(tmp_path):
    with pytest.raises(InvalidRule, match="input"):
        add_rule(tmp_path, {**VALID_RULE, "input": {"body": 123}})


def test_add_duplicate_name_rejected_without_replace(tmp_path):
    add_rule(tmp_path, VALID_RULE)
    with pytest.raises(InvalidRule, match="already exists"):
        add_rule(tmp_path, VALID_RULE)


def test_add_duplicate_name_replaced_explicitly(tmp_path):
    add_rule(tmp_path, VALID_RULE)
    add_rule(tmp_path, {**VALID_RULE, "assistant": "other-graph"}, replace=True)
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0]["assistant"] == "other-graph"


def test_load_rules_rejects_malformed_file(tmp_path):
    path = rules_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(InvalidRule, match="not valid JSON"):
        load_rules(tmp_path)


def test_load_rules_rejects_non_array_file(tmp_path):
    path = rules_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")
    with pytest.raises(InvalidRule, match="JSON array"):
        load_rules(tmp_path)


# --- run.finished cascade rules (#18) ---------------------------------------

RUN_FINISHED_RULE = {
    "name": "cascade-b",
    "signal_type": "run.finished",
    "connection": "aegra-local",
    "assistant": "graph-b",
    "when": {"assistant": "graph-a", "status": "success"},
}


def test_add_run_finished_rule_requires_a_non_empty_when(tmp_path):
    with pytest.raises(InvalidRule, match="when"):
        add_rule(tmp_path, {**RUN_FINISHED_RULE, "when": {}})


def test_add_run_finished_rule_rejects_missing_when(tmp_path):
    rule = dict(RUN_FINISHED_RULE)
    del rule["when"]
    with pytest.raises(InvalidRule, match="when"):
        add_rule(tmp_path, rule)


def test_add_run_finished_rule_rejects_match_field(tmp_path):
    with pytest.raises(InvalidRule, match="when"):
        add_rule(tmp_path, {**RUN_FINISHED_RULE, "match": {"status": "success"}})


def test_add_non_run_finished_rule_rejects_when_field(tmp_path):
    with pytest.raises(InvalidRule, match="when"):
        add_rule(tmp_path, {**VALID_RULE, "when": {"status": "success"}})


def test_add_run_finished_rule_subject_defaults_to_inherited_subject(tmp_path):
    add_rule(tmp_path, RUN_FINISHED_RULE)
    assert load_rules(tmp_path)[0]["subject"] == "subject"


def test_add_run_finished_rule_accepts_explicit_subject_override(tmp_path):
    add_rule(tmp_path, {**RUN_FINISHED_RULE, "subject": "run_id"})
    assert load_rules(tmp_path)[0]["subject"] == "run_id"


def test_add_run_finished_rule_rejects_self_loop_same_connection_and_assistant(tmp_path):
    """A cascade Rule whose 'when' pins the exact (connection, assistant) it
    also targets would immediately re-trigger the Run that produced it."""
    with pytest.raises(InvalidRule, match="self-loop"):
        add_rule(tmp_path, {
            "name": "cyclic", "signal_type": "run.finished", "connection": "aegra-local",
            "assistant": "graph-a", "when": {"assistant": "graph-a"},
        })


def test_add_run_finished_rule_self_loop_check_defaults_when_connection_to_own_connection(tmp_path):
    """A 'when' with no explicit 'connection' key is still checked against
    the Rule's own connection, since most boards route within one connection."""
    with pytest.raises(InvalidRule, match="self-loop"):
        add_rule(tmp_path, {
            "name": "cyclic", "signal_type": "run.finished", "connection": "aegra-local",
            "assistant": "graph-a", "when": {"assistant": "graph-a", "status": "success"},
        })


def test_add_run_finished_rule_same_assistant_different_connection_is_not_a_self_loop(tmp_path):
    """Same assistant name on a *different* connection is a distinct target,
    not a self-loop -- the check is on the (connection, assistant) pair."""
    add_rule(tmp_path, {
        "name": "not-cyclic", "signal_type": "run.finished", "connection": "other-conn",
        "assistant": "graph-a", "when": {"connection": "aegra-local", "assistant": "graph-a"},
    })
    assert load_rules(tmp_path)[0]["assistant"] == "graph-a"
