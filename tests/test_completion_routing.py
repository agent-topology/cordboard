"""Completion routing and managed lifecycle across asynchronous runs (#50).

Two tiny Assistants ("graph-a", "graph-b") stand in for the two tiny graphs
the issue's own Verification note names; a fake `execute()` distinguishes
`waiting_reason` ("deadline" vs "interrupted") the same way the real
`aegra_client.execute` now does, and `router.sweep_pending`'s injected
`status_check` stands in for a later poll against the same Deployment --
no live Aegra service, no real waiting.
"""

from datetime import datetime, timezone

import pytest

from cord_runtime import router
from cord_runtime.concurrency import try_claim
from cord_runtime.connections import add_connection
from cord_runtime.deployment_lifecycle import load_lifecycle, stop_idle
from cord_runtime.pending_completions import get_pending
from cord_runtime.rules import add_rule
from cord_runtime.signals import manual_signal

FIXED_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def board(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    return tmp_path


def _add_start_rule(board_dir, assistant="graph-a", connection="aegra-local"):
    add_rule(board_dir, {
        "name": "start", "signal_type": "manual", "connection": connection,
        "assistant": assistant, "subject": "subject", "match": {}, "input": {},
    })


def _add_cascade_rule(board_dir, connection="aegra-local"):
    add_rule(board_dir, {
        "name": "cascade-b", "signal_type": "run.finished", "connection": connection,
        "assistant": "graph-b", "when": {"assistant": "graph-a", "status": "success"},
    })


@pytest.fixture
def fake_execute(monkeypatch):
    """graph-a always comes back `waiting`/`deadline` on its first call (still
    genuinely running past the wait budget); any other assistant succeeds
    immediately, each with its own fresh Thread/run id."""
    calls = []

    def _execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                 caused_by_run_id=None, cascade_depth=0):
        n = len(calls) + 1
        call = dict(endpoint=endpoint, assistant=assistant, subject=subject, graph_input=graph_input,
                    timeout=timeout, caused_by_run_id=caused_by_run_id, cascade_depth=cascade_depth)
        calls.append(call)
        if assistant == "graph-a":
            call["run_id"], call["thread_id"] = "r-a-1", "t-a-1"
            return {"run_id": "r-a-1", "thread_id": "t-a-1", "status": "waiting", "waiting_reason": "deadline"}
        call["run_id"], call["thread_id"] = f"r-{assistant}-{n}", f"t-{assistant}-{n}"
        return {"run_id": call["run_id"], "thread_id": call["thread_id"], "status": "success", "values": {}}

    monkeypatch.setattr(router, "execute", _execute)
    return calls


def _status_check(statuses):
    """A `sweep_pending` status_check double: `statuses` maps thread_id to the
    raw Aegra status string it should report."""
    def check(endpoint, thread_id, invocation_id):
        return statuses[thread_id]
    return check


# --- AC1: a run that initially exceeds the wait budget and later completes -
# --- starts one matching child with a fresh Thread/trace and the correct ---
# --- causing logical Run id -------------------------------------------------

def test_deadline_reached_run_later_swept_to_success_cascades_exactly_once(board, fake_execute):
    _add_start_rule(board)
    _add_cascade_rule(board)
    signal = manual_signal({"subject": "urn:demo"}, now=FIXED_NOW)

    outcome = router.route_signal(board, signal, now=FIXED_NOW)
    assert outcome["status"] == "routed"
    assert outcome["result"]["status"] == "waiting"
    assert "cascade" not in outcome  # no false completion (#50 AC2)
    assert len(fake_execute) == 1

    swept = router.sweep_pending(board, now=FIXED_NOW, status_check=_status_check({"t-a-1": "success"}))
    assert len(swept) == 1
    assert swept[0]["status"] == "success"

    # Exactly one child, a fresh Thread, and the correct causing Run id.
    assert len(fake_execute) == 2
    child = fake_execute[1]
    assert child["assistant"] == "graph-b"
    assert child["thread_id"] != "t-a-1"
    assert child["caused_by_run_id"] == "r-a-1"
    assert child["cascade_depth"] == 1
    assert swept[0]["cascade"]["status"] == "routed"

    # Both claims this Run held are released, and it is no longer tracked.
    assert get_pending(board, "aegra-local", "t-a-1") is None
    assert try_claim(board, "graph-a", "urn:demo", now=FIXED_NOW.timestamp(), stale_after=180) is True


def test_deadline_reached_run_still_running_is_left_pending_by_a_sweep(board, fake_execute):
    _add_start_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)

    swept = router.sweep_pending(board, now=FIXED_NOW, status_check=_status_check({"t-a-1": "running"}))
    assert swept == []
    entry = get_pending(board, "aegra-local", "t-a-1")
    assert entry is not None
    assert entry["concurrency_held"] is True
    assert entry["deployment_held"] is True


def test_a_terminal_failure_swept_releases_claims_without_a_fabricated_cascade(board, fake_execute):
    _add_start_rule(board)
    _add_cascade_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)

    swept = router.sweep_pending(board, now=FIXED_NOW, status_check=_status_check({"t-a-1": "error"}))
    assert len(swept) == 1
    assert swept[0]["status"] == "error"
    assert "cascade" not in swept[0]
    assert len(fake_execute) == 1  # no child started for graph-b
    assert get_pending(board, "aegra-local", "t-a-1") is None
    assert try_claim(board, "graph-a", "urn:demo", now=FIXED_NOW.timestamp(), stale_after=180) is True


def test_an_unreachable_deployments_pending_entries_are_left_for_a_later_sweep(board, fake_execute):
    _add_start_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)

    def raising_status_check(endpoint, thread_id, invocation_id):
        raise RuntimeError("Aegra API request failed; check service readiness and input contract")

    swept = router.sweep_pending(board, now=FIXED_NOW, status_check=raising_status_check)
    assert swept == []
    assert get_pending(board, "aegra-local", "t-a-1") is not None


# --- AC2: pause/resume boundaries never emit a false completion or ----------
# --- prematurely release the deployment's activity claim --------------------

def test_deadline_reached_never_releases_either_claim_immediately(board, monkeypatch, fake_execute):
    released_deployment = []
    released_concurrency = []
    monkeypatch.setattr(router, "release_active", lambda *a, **k: released_deployment.append(a[1]))
    monkeypatch.setattr(router, "release_concurrency", lambda *a, **k: released_concurrency.append(a[1:]))
    _add_start_rule(board)

    router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)
    assert released_deployment == []
    assert released_concurrency == []


def test_interrupted_releases_both_claims_immediately_same_as_before(board, monkeypatch):
    released_deployment = []
    released_concurrency = []
    monkeypatch.setattr(router, "release_active", lambda *a, **k: released_deployment.append(a[1]))
    monkeypatch.setattr(router, "release_concurrency", lambda *a, **k: released_concurrency.append(a[1:]))

    def interrupted_execute(*_args, **_kwargs):
        return {"run_id": "r-1", "thread_id": "t-1", "status": "waiting", "waiting_reason": "interrupted"}

    monkeypatch.setattr(router, "execute", interrupted_execute)
    _add_start_rule(board)
    outcome = router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)

    assert outcome["status"] == "routed"
    assert released_deployment == ["aegra-local"]
    assert released_concurrency == [("graph-a", "urn:demo")]
    # Still tracked (unheld) so a later resume has this identity (#50 AC5).
    entry = get_pending(board, "aegra-local", "t-1")
    assert entry == {
        "deployment": "aegra-local", "thread_id": "t-1", "invocation_id": "r-1",
        "assistant": "graph-a", "subject": "urn:demo", "cascade_depth": 0,
        "concurrency_held": False, "deployment_held": False, "recorded_at": FIXED_NOW.timestamp(),
    }


def test_a_sweep_that_discovers_an_interrupt_releases_and_downgrades_but_keeps_tracking(board, fake_execute):
    _add_start_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)

    swept = router.sweep_pending(board, now=FIXED_NOW, status_check=_status_check({"t-a-1": "interrupted"}))
    assert len(swept) == 1 and swept[0]["status"] == "interrupted"
    entry = get_pending(board, "aegra-local", "t-a-1")
    assert entry["concurrency_held"] is False
    assert entry["deployment_held"] is False
    assert try_claim(board, "graph-a", "urn:demo", now=FIXED_NOW.timestamp(), stale_after=180) is True


# --- AC3: duplicate completion delivery across restart creates no duplicate
# --- child; self-loop/depth guards and Subject inheritance stay intact -----

def test_redelivering_the_sweep_produced_completion_after_a_restart_is_a_duplicate(board, fake_execute):
    _add_start_rule(board)
    _add_cascade_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)
    router.sweep_pending(board, now=FIXED_NOW, status_check=_status_check({"t-a-1": "success"}))
    assert len(fake_execute) == 2

    from cord_runtime.signals import run_finished_signal
    redelivered = run_finished_signal(run_id="r-a-1", subject="urn:demo", connection="aegra-local",
                                       assistant="graph-a", status="success")
    outcome = router.route_signal(board, redelivered, now=FIXED_NOW)
    assert outcome["status"] == "duplicate"
    assert len(fake_execute) == 2  # no second child


# --- AC4: two distinct overlapping Subjects stay independent, while the ----
# --- same-target/Subject conflict policy holds across processes -----------

def test_two_distinct_subjects_each_retain_their_own_claim_independently(board, monkeypatch):
    def fake_execute_both(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                          caused_by_run_id=None, cascade_depth=0):
        return {"run_id": f"r-{subject}", "thread_id": f"t-{subject}", "status": "waiting",
                "waiting_reason": "deadline"}

    monkeypatch.setattr(router, "execute", fake_execute_both)
    _add_start_rule(board)
    a = router.route_signal(board, manual_signal({"subject": "urn:a"}, signal_id="a", now=FIXED_NOW),
                             now=FIXED_NOW)
    b = router.route_signal(board, manual_signal({"subject": "urn:b"}, signal_id="b", now=FIXED_NOW),
                             now=FIXED_NOW)
    assert a["status"] == "routed" and b["status"] == "routed"
    assert get_pending(board, "aegra-local", "t-urn:a") is not None
    assert get_pending(board, "aegra-local", "t-urn:b") is not None


def test_a_second_arrival_for_the_still_retained_subject_is_skipped_across_a_fresh_call(board, fake_execute):
    """Simulates two separate process invocations against the same board_dir:
    the first call's own claim is still live when a second, independent
    `route_signal` call for the same (Assistant, Subject) arrives."""
    _add_start_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, signal_id="first", now=FIXED_NOW),
                         now=FIXED_NOW)
    second = router.route_signal(board, manual_signal({"subject": "urn:demo"}, signal_id="second", now=FIXED_NOW),
                                  now=FIXED_NOW)
    assert second == {"status": "concurrency_skipped", "signal_id": "second", "rule": "start"}
    assert len(fake_execute) == 1


def test_sweep_renewal_keeps_a_long_running_claim_from_going_stale(board, fake_execute):
    """Without `sweep_pending` renewing the retained claim, a Run that
    outlives the original `timeout + health + buffer` stale-claim window
    would be reclaimed by a conflicting arrival while still genuinely
    running -- exactly the #50 AC4 regression this test pins down."""
    _add_start_rule(board)
    router.route_signal(board, manual_signal({"subject": "urn:demo"}, signal_id="first", now=FIXED_NOW,
                                              ), now=FIXED_NOW, timeout=10)
    stale_after = 10 + router._HEALTH_TIMEOUT + router._STALE_CLAIM_BUFFER

    from datetime import timedelta
    just_before_stale = FIXED_NOW + timedelta(seconds=stale_after - 5)
    router.sweep_pending(board, now=just_before_stale, status_check=_status_check({"t-a-1": "running"}))

    past_original_stale_window = FIXED_NOW + timedelta(seconds=stale_after + 5)
    still_conflicted = router.route_signal(
        board, manual_signal({"subject": "urn:demo"}, signal_id="second", now=past_original_stale_window),
        now=past_original_stale_window, timeout=10)
    assert still_conflicted["status"] == "concurrency_skipped"
    assert len(fake_execute) == 1


# --- AC5: idle sweep does not stop a Deployment a #50 pending completion ---
# --- still legitimately claims ----------------------------------------------

def test_idle_sweep_leaves_a_deployment_alone_while_a_pending_completion_holds_it(tmp_path, monkeypatch):
    """A genuine `ensure_started`/`release_active` round trip (only the
    process launch and health probe are faked): a `"deadline"` result must
    leave `active_runs` at 1 so `stop_idle` -- already proven not to touch a
    sibling Graph's active claim or an external connection -- also leaves
    this one alone."""
    import cord_runtime.deployment_lifecycle as deployment_lifecycle

    monkeypatch.setattr(deployment_lifecycle, "_default_launch", lambda alias, launch: 4242)
    monkeypatch.setattr(deployment_lifecycle, "_default_health_check", lambda endpoint, *, health_timeout: True)

    add_connection(tmp_path, "local", "http://127.0.0.1:9", launch=["true"])

    def fake_execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                     caused_by_run_id=None, cascade_depth=0):
        return {"run_id": "r-a-1", "thread_id": "t-a-1", "status": "waiting", "waiting_reason": "deadline"}

    monkeypatch.setattr(router, "execute", fake_execute)
    _add_start_rule(tmp_path, connection="local")
    outcome = router.route_signal(tmp_path, manual_signal({"subject": "urn:demo"}, now=FIXED_NOW), now=FIXED_NOW)
    assert outcome["status"] == "routed"

    lifecycle = load_lifecycle(tmp_path)
    assert lifecycle["local"]["active_runs"] == 1  # never released while genuinely still running

    from cord_runtime.connections import load_connections
    stopped = stop_idle(tmp_path, load_connections(tmp_path), now=FIXED_NOW.timestamp() + 10_000)
    assert stopped == []
    assert load_lifecycle(tmp_path)["local"]["active_runs"] == 1
