"""Generic Signal -> Rule -> Assistant routing path (#16).

Covers the acceptance criteria directly: all three sources reach the same
`route_signal`, a declared Rule is pure configuration (no router code
changes), mapped input/Subject reach the Assistant, and unmatched/invalid
signals are recorded without the payload. Uses a fixed clock and fake
`execute()` -- no live Aegra service.
"""

from datetime import datetime, timedelta, timezone
import json
import socket

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

    def _execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                 caused_by_run_id=None, cascade_depth=0):
        calls.append(dict(endpoint=endpoint, assistant=assistant, subject=subject,
                           graph_input=graph_input, timeout=timeout,
                           caused_by_run_id=caused_by_run_id, cascade_depth=cascade_depth))
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


# --- #17 AC1: repeated Signal delivery does not create duplicate work -----

def test_repeated_delivery_of_the_same_signal_id_is_a_duplicate_not_a_second_run(board, fake_execute):
    _add_rule(board, "manual")
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="fixed-id", now=FIXED_NOW)
    first = router.route_signal(board, signal, now=FIXED_NOW)
    second = router.route_signal(board, signal, now=FIXED_NOW)
    assert first["status"] == "routed"
    assert second == {"status": "duplicate", "signal_id": "fixed-id", "rule": "manual-rule"}
    assert len(fake_execute) == 1


def test_redelivery_after_an_ambiguous_submission_failure_is_still_a_duplicate(board, monkeypatch):
    """execute() raising is ambiguous transport (aegra_client's own contract):
    the Run may have been accepted, so a retry must not resubmit it."""
    _add_rule(board, "manual")
    calls = []

    def raising_execute(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("Aegra API request failed; check service readiness and input contract")

    monkeypatch.setattr(router, "execute", raising_execute)
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="fixed-id", now=FIXED_NOW)
    first = router.route_signal(board, signal, now=FIXED_NOW)
    second = router.route_signal(board, signal, now=FIXED_NOW)
    assert first["status"] == "execution_failed"
    assert second == {"status": "duplicate", "signal_id": "fixed-id", "rule": "manual-rule"}
    assert len(calls) == 1


def test_redelivery_after_a_restart_still_sees_the_same_duplicate_claim(board, fake_execute):
    """A restart is just a fresh call against the same on-disk board_dir."""
    _add_rule(board, "manual")
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="fixed-id", now=FIXED_NOW)
    router.route_signal(board, signal, now=FIXED_NOW)
    outcome = router.route_signal(board, signal, now=FIXED_NOW)
    assert outcome["status"] == "duplicate"


def test_redelivery_of_an_unmatched_signal_is_not_blocked_as_a_duplicate(board, fake_execute):
    """Nothing was submitted for an unmatched Signal, so a corrected Rule must
    still be able to route a later delivery of the same Signal id."""
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="fixed-id", now=FIXED_NOW)
    first = router.route_signal(board, signal, now=FIXED_NOW)
    assert first["status"] == "unmatched"
    _add_rule(board, "manual")
    second = router.route_signal(board, signal, now=FIXED_NOW)
    assert second["status"] == "routed"


def test_rapid_file_notifications_coalesce_by_content_but_distinct_content_still_runs(board, fake_execute,
                                                                                        tmp_path):
    """`file_signal`'s id is a content hash (ADR-0002): several rapid
    notifications of the *same unchanged* file content (a common inotify
    burst for one save) share one id and coalesce to one Run; a notification
    that actually carries new content gets its own Run. Known counts: 3
    deliveries, 2 distinct contents, 1 coalesced pair -> 2 Runs."""
    _add_rule(board, "file")
    event_path = tmp_path / "event.json"

    event_path.write_text(json.dumps({"subject": "file:notes/api.md", "body": "v1"}), encoding="utf-8")
    first_signal = file_signal(event_path)
    outcome_1 = router.route_signal(board, first_signal, now=FIXED_NOW)

    # A second, rapid notification of the unchanged file: same content hash, same id.
    second_signal = file_signal(event_path)
    outcome_2 = router.route_signal(board, second_signal, now=FIXED_NOW)

    event_path.write_text(json.dumps({"subject": "file:notes/api.md", "body": "v2"}), encoding="utf-8")
    third_signal = file_signal(event_path)
    outcome_3 = router.route_signal(board, third_signal, now=FIXED_NOW)

    assert first_signal["id"] == second_signal["id"]
    assert third_signal["id"] != first_signal["id"]
    assert outcome_1["status"] == "routed"
    assert outcome_2 == {"status": "duplicate", "signal_id": first_signal["id"], "rule": "file-rule"}
    assert outcome_3["status"] == "routed"
    assert len(fake_execute) == 2


# --- #17 AC2: declared (Assistant, Subject) conflicts hold under concurrency

def test_concurrent_arrival_for_the_same_assistant_and_subject_is_skipped(board, monkeypatch):
    _add_rule(board, "manual")
    holder = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="holder", now=FIXED_NOW)
    latecomer = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="latecomer", now=FIXED_NOW)

    def blocking_execute(*_args, **_kwargs):
        # Simulate the claim still being held mid-flight by re-entering the
        # router for a second Signal before the first call returns.
        inner = router.route_signal(board, latecomer, now=FIXED_NOW)
        assert inner == {"status": "concurrency_skipped", "signal_id": "latecomer", "rule": "manual-rule"}
        return {"run_id": "r-1", "thread_id": "t-1", "status": "success", "values": {}}

    monkeypatch.setattr(router, "execute", blocking_execute)
    outcome = router.route_signal(board, holder, now=FIXED_NOW)
    assert outcome["status"] == "routed"


def test_a_skipped_concurrency_conflict_does_not_consume_the_signal_dedupe_claim(board, fake_execute):
    _add_rule(board, "manual")
    from cord_runtime.concurrency import try_claim

    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="fixed-id", now=FIXED_NOW)
    try_claim(board, "triage-graph", "manual:demo", now=FIXED_NOW.timestamp(), stale_after=180)
    skipped = router.route_signal(board, signal, now=FIXED_NOW, timeout=10)
    assert skipped["status"] == "concurrency_skipped"
    assert fake_execute == []

    from cord_runtime.concurrency import release
    release(board, "triage-graph", "manual:demo")
    routed = router.route_signal(board, signal, now=FIXED_NOW)
    assert routed["status"] == "routed"


def test_different_subjects_execute_concurrently_without_conflict(board, fake_execute):
    _add_rule(board, "manual")
    signal_a = manual_signal({"subject": "manual:a", "body": "hi"}, signal_id="a", now=FIXED_NOW)
    signal_b = manual_signal({"subject": "manual:b", "body": "hi"}, signal_id="b", now=FIXED_NOW)
    outcome_a = router.route_signal(board, signal_a, now=FIXED_NOW)
    outcome_b = router.route_signal(board, signal_b, now=FIXED_NOW)
    assert outcome_a["status"] == "routed"
    assert outcome_b["status"] == "routed"


# --- #19: one same-Subject pair covers both the skip policy and restart reclaim

def test_same_subject_pair_covers_conflict_skip_and_restart_claim_reclaim(board, fake_execute):
    """A single (Assistant, Subject) pair exercises both declared behaviors
    together: a live claim still turns away a new arrival right after a
    router restart, and once that claim ages past its own stale window the
    same pair is safely reclaimed rather than locked out forever.

    `stale_after` for this call is `timeout(10) + _HEALTH_TIMEOUT(30) +
    _STALE_CLAIM_BUFFER(30) = 70`. Seeding the claim 50s before `now` lands
    inside that window for the first check and past it for the second,
    without any real waiting.
    """
    _add_rule(board, "manual")
    from cord_runtime.concurrency import try_claim

    # A prior holder crashed before its `finally` released the claim.
    try_claim(board, "triage-graph", "manual:demo", now=FIXED_NOW.timestamp() - 50, stale_after=70)

    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="after-restart", now=FIXED_NOW)

    # A router restarting immediately after the crash still honors the live claim.
    still_conflicted = router.route_signal(board, signal, now=FIXED_NOW, timeout=10)
    assert still_conflicted == {"status": "concurrency_skipped", "signal_id": "after-restart", "rule": "manual-rule"}
    assert fake_execute == []

    # Once the crashed claim ages past stale_after, the same pair is reclaimed.
    later = FIXED_NOW + timedelta(seconds=25)
    reclaimed = router.route_signal(board, signal, now=later, timeout=10)
    assert reclaimed["status"] == "routed"
    assert fake_execute[0]["subject"] == "manual:demo"


# --- #17 AC4/AC5: managed Deployment startup, health failure stays visible -

def _closed_local_port() -> int:
    """A port nothing listens on: bind-then-close, rather than a fixed
    number that may collide with a real service already running locally."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def managed_board(tmp_path):
    add_connection(tmp_path, "local", f"http://127.0.0.1:{_closed_local_port()}", launch=["true"])
    return tmp_path


def test_managed_deployment_starts_before_execution(managed_board, monkeypatch, fake_execute):
    import cord_runtime.router as router_module

    started = []

    def fake_ensure_started(board_dir, alias, connection, *, now, health_timeout):
        started.append(alias)
        return {"status": "running", "pid": 1}

    released = []
    monkeypatch.setattr(router_module, "ensure_started", fake_ensure_started)
    monkeypatch.setattr(router_module, "release_active", lambda *a, **k: released.append(a[1]))
    _add_rule(managed_board, "manual", connection="local")
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, now=FIXED_NOW)
    outcome = router.route_signal(managed_board, signal, now=FIXED_NOW)
    assert outcome["status"] == "routed"
    assert started == ["local"]
    assert released == ["local"]


def test_managed_deployment_startup_failure_is_visible_and_signal_stays_retryable(managed_board, fake_execute,
                                                                                   monkeypatch):
    # Real ensure_started()/health check, but with a tiny timeout: nothing
    # listens on this endpoint, so startup must fail fast, not for real 30s.
    monkeypatch.setattr(router, "_HEALTH_TIMEOUT", 0.05)
    _add_rule(managed_board, "manual", connection="local")
    signal = manual_signal({"subject": "manual:demo", "body": "hi"}, signal_id="fixed-id", now=FIXED_NOW)
    outcome = router.route_signal(managed_board, signal, now=FIXED_NOW)
    assert outcome["status"] == "deployment_unavailable"
    assert "error" in outcome
    assert fake_execute == []
    # Nothing was submitted, so the same Signal id must still be retryable
    # once the Deployment is reachable -- the failure never claimed dedupe.
    from cord_runtime.signal_dedupe import is_duplicate
    assert is_duplicate(managed_board, "fixed-id", now=FIXED_NOW.timestamp()) is False
