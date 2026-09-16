"""Explicit opt-in managed Deployment startup and idle shutdown (#17).

Most cases use controlled ids, an injected clock, and fake launch/health
callables -- no real process -- to make startup failure, idle-after, and the
multi-Graph "siblings with active Runs" case deterministic. One test exercises
a tiny test-owned process (a bare `http.server` with a `health` file as its
docroot) rather than any operator's real deployment, to verify the default
launcher/health-checker actually start and stop a process.
"""

import contextlib
import http.client
import os
import socket
import sys
import time

import pytest

from cord_runtime.deployment_lifecycle import (
    RUNNING,
    START_FAILED,
    ensure_started,
    load_lifecycle,
    release_active,
    stop_idle,
)

NOW = 1_800_000_000.0

EXTERNAL_CONNECTION = {"endpoint": "http://127.0.0.1:9"}
MANAGED_CONNECTION = {"endpoint": "http://127.0.0.1:9", "launch": ["true"], "idle_after": 600.0}


def _ok_launch(alias, launch):
    return 4242


def _ok_health(endpoint):
    return True


def _failing_launch(alias, launch):
    raise OSError("no such executable")


def _failing_health(endpoint):
    return False


# --- external connections are never started or stopped --------------------

def test_external_connection_is_never_started(tmp_path):
    result = ensure_started(tmp_path, "ext", EXTERNAL_CONNECTION, now=NOW)
    assert result == {"status": "external"}
    assert load_lifecycle(tmp_path) == {}


def test_external_connection_release_is_a_no_op(tmp_path):
    release_active(tmp_path, "ext", EXTERNAL_CONNECTION, now=NOW)  # does not raise
    assert load_lifecycle(tmp_path) == {}


def test_external_connection_is_never_swept(tmp_path):
    stopped = stop_idle(tmp_path, {"ext": EXTERNAL_CONNECTION}, now=NOW + 10_000)
    assert stopped == []


# --- managed startup --------------------------------------------------------

def test_managed_connection_starts_and_becomes_running(tmp_path):
    result = ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                             launch=_ok_launch, health_check=_ok_health)
    assert result == {"status": RUNNING, "pid": 4242}
    record = load_lifecycle(tmp_path)["local"]
    assert record["status"] == RUNNING
    assert record["active_runs"] == 1


def test_already_running_deployment_is_not_relaunched_but_bumps_active_runs(tmp_path):
    calls = []

    def counting_launch(alias, launch):
        calls.append(1)
        return 4242

    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=counting_launch, health_check=_ok_health)
    result = ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW + 1,
                             launch=counting_launch, health_check=_ok_health)
    assert result["status"] == RUNNING
    assert len(calls) == 1
    assert load_lifecycle(tmp_path)["local"]["active_runs"] == 2


def test_launch_failure_is_recorded_and_visible_not_raised(tmp_path):
    result = ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                             launch=_failing_launch, health_check=_ok_health)
    assert result["status"] == START_FAILED
    assert "no such executable" in result["error"]
    record = load_lifecycle(tmp_path)["local"]
    assert record["status"] == START_FAILED
    assert record["active_runs"] == 0


def test_health_check_failure_is_recorded_and_visible_not_raised(tmp_path):
    result = ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                             launch=_ok_launch, health_check=_failing_health)
    assert result["status"] == START_FAILED
    assert "healthy" in result["error"]
    assert load_lifecycle(tmp_path)["local"]["status"] == START_FAILED


def test_a_start_failure_can_be_retried_later(tmp_path):
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_failing_launch, health_check=_ok_health)
    result = ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW + 10,
                             launch=_ok_launch, health_check=_ok_health)
    assert result["status"] == RUNNING


# --- idle shutdown -----------------------------------------------------------

def test_idle_managed_deployment_with_no_active_runs_is_stopped(tmp_path):
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_ok_launch, health_check=_ok_health)
    release_active(tmp_path, "local", MANAGED_CONNECTION, now=NOW)
    stopped_calls = []
    stopped = stop_idle(tmp_path, {"local": MANAGED_CONNECTION}, now=NOW + 601,
                         stop=lambda alias, record: stopped_calls.append(alias))
    assert stopped == ["local"]
    assert stopped_calls == ["local"]
    assert load_lifecycle(tmp_path)["local"]["status"] == "stopped"


def test_not_yet_idle_managed_deployment_is_left_running(tmp_path):
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_ok_launch, health_check=_ok_health)
    release_active(tmp_path, "local", MANAGED_CONNECTION, now=NOW)
    stopped = stop_idle(tmp_path, {"local": MANAGED_CONNECTION}, now=NOW + 10, stop=lambda a, r: None)
    assert stopped == []
    assert load_lifecycle(tmp_path)["local"]["status"] == RUNNING


def test_a_deployment_with_an_active_run_is_never_stopped_even_when_idle_by_clock(tmp_path):
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_ok_launch, health_check=_ok_health)
    # No release_active(): one active_runs claim is still held.
    stopped = stop_idle(tmp_path, {"local": MANAGED_CONNECTION}, now=NOW + 10_000, stop=lambda a, r: None)
    assert stopped == []


def test_one_graph_going_idle_does_not_stop_a_sibling_graphs_deployment(tmp_path):
    """Two Graphs share one Deployment alias; active_runs is tracked per alias."""
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_ok_launch, health_check=_ok_health)  # Graph A starts it
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_ok_launch, health_check=_ok_health)  # Graph B bumps active_runs to 2
    release_active(tmp_path, "local", MANAGED_CONNECTION, now=NOW + 700)  # Graph A finishes and goes idle
    stopped = stop_idle(tmp_path, {"local": MANAGED_CONNECTION}, now=NOW + 1_400, stop=lambda a, r: None)
    assert stopped == []  # Graph B's claim is still held
    assert load_lifecycle(tmp_path)["local"]["active_runs"] == 1


def test_stop_idle_ignores_aliases_no_longer_in_connections(tmp_path):
    ensure_started(tmp_path, "local", MANAGED_CONNECTION, now=NOW,
                    launch=_ok_launch, health_check=_ok_health)
    release_active(tmp_path, "local", MANAGED_CONNECTION, now=NOW)
    stopped = stop_idle(tmp_path, {}, now=NOW + 10_000, stop=lambda a, r: None)
    assert stopped == []
    assert load_lifecycle(tmp_path)["local"]["status"] == RUNNING


# --- a tiny test-owned real process, not an operator's deployment ----------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Reap ``pid``, since this test process is its direct parent (as the
    launching `cord` CLI invocation would be in production) and nothing else
    will collect it. ``waitpid(WNOHANG)`` also distinguishes "exited" from
    "zombie, not yet reaped" the way a bare ``kill(pid, 0)`` liveness check
    cannot.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if reaped_pid == pid:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def tiny_server(tmp_path):
    docroot = tmp_path / "www"
    docroot.mkdir()
    (docroot / "health").write_text("ok", encoding="utf-8")
    port = _free_port()
    launch = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(docroot)]
    connection = {"endpoint": f"http://127.0.0.1:{port}", "launch": launch, "idle_after": 600.0}
    yield connection


def test_default_launch_and_health_check_start_a_real_process(tmp_path, tiny_server):
    result = ensure_started(tmp_path, "local", tiny_server, now=NOW, health_timeout=10.0)
    try:
        assert result["status"] == RUNNING
        pid = result["pid"]
        os.kill(pid, 0)  # still alive; raises ProcessLookupError otherwise
        port = int(tiny_server["endpoint"].rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health")
        assert conn.getresponse().status == 200
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(result["pid"], 15)


def test_default_stop_terminates_the_real_process(tmp_path, tiny_server):
    result = ensure_started(tmp_path, "local", tiny_server, now=NOW, health_timeout=10.0)
    assert result["status"] == RUNNING
    release_active(tmp_path, "local", tiny_server, now=NOW)
    stopped = stop_idle(tmp_path, {"local": tiny_server}, now=NOW + 601)
    assert stopped == ["local"]
    assert _wait_for_exit(result["pid"], timeout=5.0)
