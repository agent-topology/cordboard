"""One connection-scoped readiness diagnostic (#72): required execution vs.
every optional capability, passive-only checks, bounded classified results.
"""

import json
from pathlib import Path

from cord_runtime import cli, connection_diagnostics as diag
from cord_runtime.archive_health import HealthStatus
from cord_runtime.deployment_lifecycle import RUNNING, START_FAILED, STOPPED, lifecycle_path
from cord_runtime.topology import FreshnessCheck, TopologyReading, VALID

FIXTURES = Path(__file__).parent / "fixtures/escalations"
COMPLETE_LINE = (FIXTURES / "window.otlp.jsonl").read_text().splitlines()[0]


def _write_lifecycle(board_dir, alias, status, **extra):
    path = lifecycle_path(board_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({alias: {"status": status, "pid": None, "active_runs": 0,
                                        "last_active": 0.0, "last_error": None, **extra}}),
                    encoding="utf-8")


# --- execution (required) ---------------------------------------------------

def test_execution_ready_when_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": ["minimal-graph"]})
    connection = {"endpoint": "http://127.0.0.1:2026"}
    result = diag.diagnose_connection(tmp_path, "aegra-local", connection)
    execution = result.capabilities.execution
    assert execution.required is True
    assert execution.state == diag.READY
    assert execution.status == "reachable"
    assert execution.detail == {"graphs": ["minimal-graph"]}


def test_execution_unavailable_when_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": False, "graphs": []})
    connection = {"endpoint": "http://127.0.0.1:2026"}
    result = diag.diagnose_connection(tmp_path, "aegra-local", connection)
    execution = result.capabilities.execution
    assert execution.state == diag.UNAVAILABLE
    assert execution.status == "unreachable"
    assert execution.detail is None


# --- topology (optional) -----------------------------------------------------

def _fake_freshness(status, **kw):
    def fake(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status=status, reading=TopologyReading(status=VALID, document=kw.get("document")))
    return fake


def test_topology_absent_is_not_configured(tmp_path, monkeypatch):
    from cord_runtime.topology import ABSENT

    def fake(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT))

    monkeypatch.setattr(diag, "check_freshness", fake)
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"})
    topology = result.capabilities.topology
    assert topology.required is False
    assert topology.state == diag.NOT_CONFIGURED
    assert topology.status == "absent"


def test_topology_unreachable_is_unavailable(tmp_path, monkeypatch):
    from cord_runtime.topology import UNREACHABLE

    def fake(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status=UNREACHABLE, reading=TopologyReading(status=UNREACHABLE, reason="timeout"))

    monkeypatch.setattr(diag, "check_freshness", fake)
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"})
    assert result.capabilities.topology.state == diag.UNAVAILABLE
    assert result.capabilities.topology.status == "unreachable"


def test_topology_invalid_document_is_unavailable(tmp_path, monkeypatch):
    from cord_runtime.topology import INVALID

    def fake(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status=INVALID, reading=TopologyReading(status=INVALID, errors=("bad schema",)))

    monkeypatch.setattr(diag, "check_freshness", fake)
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"})
    assert result.capabilities.topology.state == diag.UNAVAILABLE
    assert result.capabilities.topology.status == "invalid"


def test_topology_never_calls_refresh_snapshot(tmp_path, monkeypatch):
    """A passive diagnostic must never write a topology snapshot (AC2)."""
    from cord_runtime.topology import ABSENT

    def fake(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT))

    def boom(*_args, **_kwargs):
        raise AssertionError("refresh_snapshot must not be called by a passive diagnostic")

    monkeypatch.setattr(diag, "check_freshness", fake)
    monkeypatch.setattr("cord_runtime.topology.refresh_snapshot", boom)
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"})


# --- telemetry.archive / telemetry.collector (optional) ---------------------

def test_telemetry_archive_not_configured_without_archive_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    monkeypatch.setattr(diag, "check_freshness",
                        lambda *a, **k: FreshnessCheck(status="absent", reading=TopologyReading(status="absent")))
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"})
    archive = result.capabilities.telemetry.archive
    assert archive.required is False
    assert archive.state == diag.NOT_CONFIGURED
    assert archive.status == "not_configured"


def test_telemetry_archive_healthy_with_spans(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "2026-09-17.otlp.jsonl").write_text(COMPLETE_LINE + "\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    monkeypatch.setattr(diag, "check_freshness",
                        lambda *a, **k: FreshnessCheck(status="absent", reading=TopologyReading(status="absent")))
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"},
                                      archive_paths=(archive_dir,))
    archive = result.capabilities.telemetry.archive
    assert archive.state == diag.READY
    assert archive.status == HealthStatus.HEALTHY.value


def test_telemetry_archive_failed_is_unavailable(tmp_path, monkeypatch):
    record = json.loads(COMPLETE_LINE)
    record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "not-hex"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "2026-09-17.otlp.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    monkeypatch.setattr(diag, "check_freshness",
                        lambda *a, **k: FreshnessCheck(status="absent", reading=TopologyReading(status="absent")))
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"},
                                      archive_paths=(archive_dir,))
    archive = result.capabilities.telemetry.archive
    assert archive.state == diag.UNAVAILABLE
    assert archive.status == HealthStatus.FAILED.value


def test_telemetry_collector_always_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})
    monkeypatch.setattr(diag, "check_freshness",
                        lambda *a, **k: FreshnessCheck(status="absent", reading=TopologyReading(status="absent")))
    result = diag.diagnose_connection(tmp_path, "aegra-local", {"endpoint": "http://x"})
    collector = result.capabilities.telemetry.collector
    assert collector.required is False
    assert collector.state == diag.UNSUPPORTED
    assert collector.status == "unsupported"


# --- managed_lifecycle (optional) -------------------------------------------

def _diagnose_lifecycle(tmp_path, monkeypatch, connection, *, reachable=True):
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": reachable, "graphs": []})
    monkeypatch.setattr(diag, "check_freshness",
                        lambda *a, **k: FreshnessCheck(status="absent", reading=TopologyReading(status="absent")))
    return diag.diagnose_connection(tmp_path, "aegra-local", connection)


def test_lifecycle_external_when_unmanaged(tmp_path, monkeypatch):
    result = _diagnose_lifecycle(tmp_path, monkeypatch, {"endpoint": "http://x"})
    lifecycle = result.capabilities.managed_lifecycle
    assert lifecycle.required is False
    assert lifecycle.state == diag.NOT_CONFIGURED
    assert lifecycle.status == "external"


def test_lifecycle_never_started_when_managed_without_a_record(tmp_path, monkeypatch):
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection)
    lifecycle = result.capabilities.managed_lifecycle
    assert lifecycle.state == diag.READY
    assert lifecycle.status == "never_started"


def test_lifecycle_running_is_ready(tmp_path, monkeypatch):
    _write_lifecycle(tmp_path, "aegra-local", RUNNING, pid=123)
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection)
    assert result.capabilities.managed_lifecycle.state == diag.READY
    assert result.capabilities.managed_lifecycle.status == RUNNING


def test_lifecycle_running_but_unreachable_is_stale_not_a_false_ready(tmp_path, monkeypatch):
    """The tracked record still claims RUNNING, but this same connection's
    execution probe (the required capability, checked independently above)
    just found it unreachable: the tracked process crashed outside
    Cordboard's control. This must surface as a distinguishable, truthful
    "stale" fact rather than a contradictory "ready" -- the next execution's
    `ensure_started` re-verifies and relaunches it without a manual state-file
    edit (#75); this diagnostic only makes that staleness visible."""
    _write_lifecycle(tmp_path, "aegra-local", RUNNING, pid=123)
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection, reachable=False)
    lifecycle = result.capabilities.managed_lifecycle
    assert lifecycle.state == diag.UNAVAILABLE
    assert lifecycle.status == "stale"


def test_lifecycle_stopped_is_still_ready_restarts_on_demand(tmp_path, monkeypatch):
    _write_lifecycle(tmp_path, "aegra-local", STOPPED)
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection)
    assert result.capabilities.managed_lifecycle.state == diag.READY
    assert result.capabilities.managed_lifecycle.status == STOPPED


def test_lifecycle_start_failed_is_unavailable(tmp_path, monkeypatch):
    _write_lifecycle(tmp_path, "aegra-local", START_FAILED, last_error="did not become healthy")
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection)
    lifecycle = result.capabilities.managed_lifecycle
    assert lifecycle.state == diag.UNAVAILABLE
    assert lifecycle.status == START_FAILED
    # Sanitized: the raw last_error string never appears in the bounded fact.
    assert lifecycle.detail is None


def test_lifecycle_stopped_and_unreachable_is_still_plain_stopped_not_stale(tmp_path, monkeypatch):
    """"stale" only distinguishes a RUNNING record contradicted by an
    unreachable probe; a deliberately STOPPED (idle-swept) Deployment being
    unreachable is expected, not a crash to flag."""
    _write_lifecycle(tmp_path, "aegra-local", STOPPED)
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection, reachable=False)
    lifecycle = result.capabilities.managed_lifecycle
    assert lifecycle.state == diag.READY
    assert lifecycle.status == STOPPED


def test_lifecycle_never_calls_ensure_started(tmp_path, monkeypatch):
    """A passive diagnostic must never start a managed Deployment (AC2)."""
    def boom(*_args, **_kwargs):
        raise AssertionError("ensure_started must not be called by a passive diagnostic")

    monkeypatch.setattr("cord_runtime.deployment_lifecycle.ensure_started", boom)
    connection = {"endpoint": "http://x", "launch": ["true"], "idle_after": 600.0}
    _diagnose_lifecycle(tmp_path, monkeypatch, connection)


# --- approval_authorization (optional) --------------------------------------

def test_approval_authorization_not_configured_without_auth_endpoint(tmp_path, monkeypatch):
    result = _diagnose_lifecycle(tmp_path, monkeypatch, {"endpoint": "http://x"})
    approval = result.capabilities.approval_authorization
    assert approval.required is False
    assert approval.state == diag.NOT_CONFIGURED
    assert approval.status == "not_configured"


def test_approval_authorization_ready_when_configured(tmp_path, monkeypatch):
    connection = {"endpoint": "http://x", "auth_endpoint": "http://127.0.0.1:9999"}
    result = _diagnose_lifecycle(tmp_path, monkeypatch, connection)
    approval = result.capabilities.approval_authorization
    assert approval.state == diag.READY
    assert approval.status == "configured"


def test_approval_authorization_never_calls_authorize(tmp_path, monkeypatch):
    """A passive diagnostic must never submit a fabricated authorization
    decision (AC2); only configuration presence is reported."""
    def boom(*_args, **_kwargs):
        raise AssertionError("authorize must not be called by a passive diagnostic")

    monkeypatch.setattr("cord_runtime.entity_auth.HttpEntityAuthBoundary.authorize", boom)
    connection = {"endpoint": "http://x", "auth_endpoint": "http://127.0.0.1:9999"}
    _diagnose_lifecycle(tmp_path, monkeypatch, connection)


# --- CLI: cord diagnose (#72) ------------------------------------------------

def test_cmd_diagnose_unknown_alias_exit_two(tmp_path):
    assert cli.main(["--board", str(tmp_path), "diagnose", "missing"]) == cli.EXIT_USAGE


def test_cmd_diagnose_unreachable_exit_one(tmp_path, monkeypatch, capsys):
    from cord_runtime.connections import add_connection

    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": False, "graphs": []})
    code = cli.main(["--board", str(tmp_path), "diagnose", "aegra-local"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_FAILURE
    assert "execution [required] -> unavailable" in out


def test_cmd_diagnose_ready_with_no_optional_capabilities_configured_exit_zero(tmp_path, monkeypatch, capsys):
    from cord_runtime.connections import add_connection

    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": ["minimal-graph"]})

    def fake_freshness(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status="absent", reading=TopologyReading(status="absent"))

    monkeypatch.setattr(diag, "check_freshness", fake_freshness)
    code = cli.main(["--board", str(tmp_path), "diagnose", "aegra-local"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "execution [required] -> ready" in out
    assert "topology [optional] -> not_configured" in out
    assert "telemetry.archive [optional] -> not_configured" in out
    assert "telemetry.collector [optional] -> unsupported" in out
    assert "managed_lifecycle [optional] -> not_configured" in out
    assert "approval_authorization [optional] -> not_configured" in out


def test_cmd_diagnose_json_output_matches_the_same_facts_as_text(tmp_path, monkeypatch, capsys):
    from cord_runtime.connections import add_connection

    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": []})

    def fake_freshness(board_dir, endpoint, *, timeout=5.0, session=None):
        return FreshnessCheck(status="absent", reading=TopologyReading(status="absent"))

    monkeypatch.setattr(diag, "check_freshness", fake_freshness)
    code = cli.main(["--board", str(tmp_path), "diagnose", "aegra-local", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK
    assert payload["alias"] == "aegra-local"
    assert payload["capabilities"]["execution"]["state"] == diag.READY
    assert payload["capabilities"]["topology"]["state"] == diag.NOT_CONFIGURED
    assert payload["capabilities"]["telemetry"]["archive"]["state"] == diag.NOT_CONFIGURED
    assert payload["capabilities"]["telemetry"]["collector"]["state"] == diag.UNSUPPORTED
