"""`cord add` / `cord list` / `cord run` (#12): storage, reachability, execution.

No live Aegra/Postgres/Docker here (that stack is covered by tests/test_aegra.py).
The connection contract and command exit statuses are deterministic without it.
"""

import json
from pathlib import Path

import pytest
import requests

from cord_runtime import cli
from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.connections import (
    InvalidConnection,
    add_connection,
    connections_path,
    get_connection,
    is_managed,
    load_connections,
    validate_endpoint,
)
from cord_runtime.deployment_lifecycle import load_lifecycle
from cord_runtime.rules import load_rules
from cord_runtime.topology import ABSENT, FreshnessCheck, TopologyReading


# --- connections.py: storage contract -----------------------------------

def test_add_and_load_round_trip(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    data = load_connections(tmp_path)
    assert data == {"aegra-local": {"endpoint": "http://127.0.0.1:2026"}}
    assert connections_path(tmp_path).is_file()


def test_add_duplicate_alias_rejected_without_replace(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    with pytest.raises(InvalidConnection, match="already exists"):
        add_connection(tmp_path, "aegra-local", "http://127.0.0.1:9999")
    assert load_connections(tmp_path)["aegra-local"]["endpoint"] == "http://127.0.0.1:2026"


def test_add_duplicate_alias_replaced_explicitly(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:9999", replace=True)
    assert load_connections(tmp_path)["aegra-local"]["endpoint"] == "http://127.0.0.1:9999"


@pytest.mark.parametrize("endpoint", [
    "http://user:pass@127.0.0.1:2026",
    "ftp://127.0.0.1:2026",
    "not-a-url",
    "",
])
def test_add_rejects_invalid_endpoint(tmp_path, endpoint):
    with pytest.raises(InvalidConnection):
        add_connection(tmp_path, "aegra-local", endpoint)
    assert load_connections(tmp_path) == {}


def test_validate_endpoint_rejects_userinfo_even_without_password():
    with pytest.raises(InvalidConnection, match="userinfo"):
        validate_endpoint("http://token@127.0.0.1:2026")


def test_get_connection_unknown_alias(tmp_path):
    with pytest.raises(InvalidConnection, match="unknown alias"):
        get_connection(tmp_path, "missing")


def test_load_connections_rejects_malformed_file(tmp_path):
    path = connections_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(InvalidConnection, match="not valid JSON"):
        load_connections(tmp_path)


def test_stored_record_carries_no_extra_fields(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026/")
    raw = json.loads(connections_path(tmp_path).read_text(encoding="utf-8"))
    assert raw == {"aegra-local": {"endpoint": "http://127.0.0.1:2026"}}


# --- connections.py: managed Deployment opt-in (#17) ------------------------

def test_unmanaged_connection_has_no_launch_and_is_not_managed(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    assert is_managed(load_connections(tmp_path)["aegra-local"]) is False


def test_connection_with_launch_is_managed_with_default_idle_after(tmp_path):
    add_connection(tmp_path, "local", "http://127.0.0.1:2026", launch=["true"])
    record = load_connections(tmp_path)["local"]
    assert is_managed(record) is True
    assert record["launch"] == ["true"]
    assert record["idle_after"] == 600.0


def test_connection_with_launch_accepts_explicit_idle_after(tmp_path):
    add_connection(tmp_path, "local", "http://127.0.0.1:2026", launch=["true"], idle_after=30.0)
    assert load_connections(tmp_path)["local"]["idle_after"] == 30.0


def test_idle_after_without_launch_is_silently_ignored(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026", idle_after=30.0)
    raw = json.loads(connections_path(tmp_path).read_text(encoding="utf-8"))
    assert raw == {"aegra-local": {"endpoint": "http://127.0.0.1:2026"}}


@pytest.mark.parametrize("launch", [[], ["", "x"], "not-a-list", None])
def test_add_rejects_invalid_launch(tmp_path, launch):
    if launch is None:
        add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026", launch=None)
        return
    with pytest.raises(InvalidConnection):
        add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026", launch=launch)


def test_add_rejects_non_positive_idle_after(tmp_path):
    with pytest.raises(InvalidConnection, match="idle_after"):
        add_connection(tmp_path, "local", "http://127.0.0.1:2026", launch=["true"], idle_after=0)


# --- cli.py: cmd_add -----------------------------------------------------

def test_cmd_add_success_exit_zero(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "add", "aegra-local", "http://127.0.0.1:2026"])
    assert code == cli.EXIT_OK
    assert "added" in capsys.readouterr().out


def test_cmd_add_invalid_endpoint_exit_two(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "add", "aegra-local", "not-a-url"])
    assert code == cli.EXIT_USAGE
    assert "cord:" in capsys.readouterr().err


def test_cmd_add_with_launch_registers_a_managed_connection(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "add", "local", "http://127.0.0.1:2026",
                     "--launch", "aegra serve --port 2026", "--idle-after", "30"])
    assert code == cli.EXIT_OK
    assert "managed" in capsys.readouterr().out
    record = load_connections(tmp_path)["local"]
    assert record["launch"] == ["aegra", "serve", "--port", "2026"]
    assert record["idle_after"] == 30.0


# --- cli.py: cmd_deployment_sweep (#17) -------------------------------------

def test_cmd_deployment_sweep_reports_no_idle_deployments(tmp_path, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    code = cli.main(["--board", str(tmp_path), "deployment", "sweep"])
    assert code == cli.EXIT_OK
    assert "no idle managed deployments" in capsys.readouterr().out


def test_cmd_deployment_sweep_stops_an_idle_managed_deployment(tmp_path, capsys):
    from cord_runtime.deployment_lifecycle import ensure_started, release_active

    add_connection(tmp_path, "local", "http://127.0.0.1:2026", launch=["true"], idle_after=1.0)
    connection = load_connections(tmp_path)["local"]
    ensure_started(tmp_path, "local", connection, now=1_000.0,
                    launch=lambda alias, launch: 1234, health_check=lambda endpoint: True)
    release_active(tmp_path, "local", connection, now=1_000.0)
    code = cli.main(["--board", str(tmp_path), "deployment", "sweep"])
    assert code == cli.EXIT_OK
    # cmd_deployment_sweep uses the real wall clock; idle_after=1s has certainly elapsed.
    assert "stopped: local" in capsys.readouterr().out
    assert load_lifecycle(tmp_path)["local"]["status"] == "stopped"


# --- cli.py: cmd_list ------------------------------------------------------

class FakeGetResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_cmd_list_no_connections(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "list"])
    assert code == cli.EXIT_OK
    assert "no connections registered" in capsys.readouterr().out


def test_cmd_list_unknown_alias_exit_two(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    assert cli.main(["--board", str(tmp_path), "list", "missing"]) == cli.EXIT_USAGE


def test_cmd_list_reachable_shows_graphs(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")

    def fake_get(url, timeout=None):
        if url.endswith("/health"):
            return FakeGetResponse(200)
        if url.endswith("/assistants"):
            return FakeGetResponse(200, {"assistants": [{"graph_id": "minimal-graph"},
                                                        {"graph_id": "opaque-graph"}]})
        raise AssertionError(url)

    monkeypatch.setattr(cli.requests, "get", fake_get)
    code = cli.main(["--board", str(tmp_path), "list"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "reachable" in out and "minimal-graph, opaque-graph" in out


def test_cmd_list_unreachable_exit_one(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")

    def fake_get(url, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(cli.requests, "get", fake_get)
    code = cli.main(["--board", str(tmp_path), "list"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_FAILURE
    assert "unreachable" in out and "graphs=-" in out


# --- cli.py: cmd_view (#13) -------------------------------------------------

def _fake_probe_get(url, timeout=None):
    if url.endswith("/health"):
        return FakeGetResponse(200)
    if url.endswith("/assistants"):
        return FakeGetResponse(200, {"assistants": [{"graph_id": "fixture-a"}]})
    raise AssertionError(url)


def test_cmd_view_no_connections_no_archive(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "view"])
    assert code == cli.EXIT_OK
    assert "no Graphs" in capsys.readouterr().out


def test_cmd_view_renders_graph_before_any_run(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli.requests, "get", _fake_probe_get)
    monkeypatch.setattr(cli, "check_freshness",
                        lambda board_dir, endpoint, **kw: FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT)))

    code = cli.main(["--board", str(tmp_path), "view"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Graph fixture-a" in out
    assert "topology: absent" in out
    assert "no recorded Runs" in out


def test_cmd_view_unknown_alias_exit_two(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    assert cli.main(["--board", str(tmp_path), "view", "missing"]) == cli.EXIT_USAGE


def test_cmd_view_json_output_is_valid_json(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli.requests, "get", _fake_probe_get)
    monkeypatch.setattr(cli, "check_freshness",
                        lambda board_dir, endpoint, **kw: FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT)))

    code = cli.main(["--board", str(tmp_path), "view", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK
    assert payload["graphs"][0]["graph_id"] == "fixture-a"


def test_cmd_view_with_archive_shows_recorded_execution(tmp_path, monkeypatch, capsys):
    root = Path(__file__).parents[1]
    code = cli.main(["--board", str(tmp_path), "view",
                     "--archive", str(root / "examples/archive.graph-id.sample.otlp.jsonl"), "--json"])
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    graph_ids = {g["graph_id"] for g in payload["graphs"]}
    assert graph_ids  # at least the archived Graphs render with no connection


def test_cmd_view_invalid_archive_exit_two(tmp_path):
    bad = tmp_path / "bad.otlp.jsonl"
    bad.write_text("not json\n")
    code = cli.main(["--board", str(tmp_path), "view", "--archive", str(bad)])
    assert code == cli.EXIT_USAGE


# --- cli.py: cmd_run -------------------------------------------------------

def _input_file(tmp_path, payload):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cmd_run_unknown_alias_exit_two(tmp_path):
    input_path = _input_file(tmp_path, {})
    code = cli.main(["--board", str(tmp_path), "run", "missing", "assistant", "subj", str(input_path)])
    assert code == cli.EXIT_USAGE


def test_cmd_run_empty_subject_exit_two(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    input_path = _input_file(tmp_path, {})
    code = cli.main(["--board", str(tmp_path), "run", "aegra-local", "g", "  ", str(input_path)])
    assert code == cli.EXIT_USAGE


def test_cmd_run_invalid_input_json_exit_two_before_submission(tmp_path, monkeypatch):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    bad_input = tmp_path / "input.json"
    bad_input.write_text("{not json", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise AssertionError("execute() must not be called for invalid input JSON")

    monkeypatch.setattr(cli, "execute", fail)
    code = cli.main(["--board", str(tmp_path), "run", "aegra-local", "g", "subj", str(bad_input)])
    assert code == cli.EXIT_USAGE


def test_cmd_run_success_prints_result_exit_zero(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    input_path = _input_file(tmp_path, {"source": "hello"})
    captured = {}

    def fake_execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120):
        captured.update(endpoint=endpoint, assistant=assistant, subject=subject,
                        graph_input=graph_input, request_context=request_context, timeout=timeout)
        return {"run_id": "r-1", "thread_id": "t-1", "status": "success", "values": {"output": "hi"}}

    monkeypatch.setattr(cli, "execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "run", "aegra-local", "minimal-graph", "subj", str(input_path)])
    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"run_id": "r-1", "thread_id": "t-1",
                                                    "status": "success", "values": {"output": "hi"}}
    assert captured["endpoint"] == "http://127.0.0.1:2026"
    assert captured["graph_input"] == {"source": "hello"}
    assert captured["request_context"] is None


def test_cmd_run_context_file_passed_through(tmp_path, monkeypatch):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    input_path = _input_file(tmp_path, {})
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"tenant": "acme"}), encoding="utf-8")
    captured = {}

    def fake_execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120):
        captured["request_context"] = request_context
        return {"run_id": "r", "thread_id": "t", "status": "success", "values": {}}

    monkeypatch.setattr(cli, "execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "run", "aegra-local", "g", "subj", str(input_path),
                     "--context", str(context_path)])
    assert code == cli.EXIT_OK
    assert captured["request_context"] == {"tenant": "acme"}


def test_cmd_run_waiting_status_is_nonzero_but_not_an_error(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    input_path = _input_file(tmp_path, {})

    def fake_execute(*_args, **_kwargs):
        return {"run_id": "r-2", "thread_id": "t-2", "status": "waiting"}

    monkeypatch.setattr(cli, "execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "run", "aegra-local", "g", "subj", str(input_path)])
    assert code == cli.EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"run_id": "r-2", "thread_id": "t-2", "status": "waiting"}


def test_cmd_run_execution_failure_exit_one_no_payload_in_diagnostic(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    input_path = _input_file(tmp_path, {"secret": "shh"})

    def fake_execute(*_args, **_kwargs):
        raise RuntimeError("Aegra execution did not succeed; inspect metadata and archive")

    monkeypatch.setattr(cli, "execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "run", "aegra-local", "g", "subj", str(input_path)])
    assert code == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "did not succeed" in err
    assert "shh" not in err


# --- cli.py: cmd_rule_add (#16) --------------------------------------------

def test_cmd_rule_add_success_exit_zero(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "rule", "add", "issue-triage", "manual",
                     "aegra-local", "triage-graph", "subject",
                     "--match", "kind=issue", "--input", "body=body"])
    assert code == cli.EXIT_OK
    assert "added rule" in capsys.readouterr().out
    rules = load_rules(tmp_path)
    assert rules == [{"name": "issue-triage", "signal_type": "manual", "connection": "aegra-local",
                      "assistant": "triage-graph", "subject": "subject",
                      "match": {"kind": "issue"}, "input": {"body": "body"}}]


def test_cmd_rule_add_duplicate_without_replace_exit_two(tmp_path):
    args = ["--board", str(tmp_path), "rule", "add", "issue-triage", "manual",
            "aegra-local", "triage-graph", "subject"]
    assert cli.main(args) == cli.EXIT_OK
    assert cli.main(args) == cli.EXIT_USAGE


def test_cmd_rule_add_invalid_signal_type_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--board", str(tmp_path), "rule", "add", "r", "webhook", "aegra-local", "g", "subject"])


def test_cmd_rule_add_match_kv_parses_json_literals(tmp_path):
    cli.main(["--board", str(tmp_path), "rule", "add", "r", "manual", "aegra-local", "g", "subject",
             "--match", "count=3", "--match", "flag=true"])
    assert load_rules(tmp_path)[0]["match"] == {"count": 3, "flag": True}


# --- cli.py: cmd_signal_* (#16) --------------------------------------------

def _add_routing_rule(tmp_path, signal_type, **overrides):
    rule = {"name": f"{signal_type}-rule", "signal_type": signal_type, "connection": "aegra-local",
            "assistant": "triage-graph", "subject": "subject", "match": {}, "input": {"body": "body"}}
    rule.update(overrides)
    from cord_runtime.rules import add_rule
    add_rule(tmp_path, rule)


def test_cmd_signal_manual_routes_and_exits_zero_on_success(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    _add_routing_rule(tmp_path, "manual")
    input_path = _input_file(tmp_path, {"subject": "manual:demo", "body": "hi"})

    def fake_execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                     caused_by_run_id=None, cascade_depth=0):
        return {"run_id": "r", "thread_id": "t", "status": "success", "values": {}}

    monkeypatch.setattr("cord_runtime.router.execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "signal", "manual", str(input_path)])
    assert code == cli.EXIT_OK
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "routed"


def test_cmd_signal_manual_unmatched_exits_one(tmp_path, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    input_path = _input_file(tmp_path, {"subject": "manual:demo"})
    code = cli.main(["--board", str(tmp_path), "signal", "manual", str(input_path)])
    assert code == cli.EXIT_FAILURE
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "unmatched"


def test_cmd_signal_file_routes(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    _add_routing_rule(tmp_path, "file")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"subject": "file:notes/api.md", "body": "changed"}), encoding="utf-8")

    def fake_execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                     caused_by_run_id=None, cascade_depth=0):
        return {"run_id": "r", "thread_id": "t", "status": "success", "values": {}}

    monkeypatch.setattr("cord_runtime.router.execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "signal", "file", str(event_path)])
    assert code == cli.EXIT_OK


def test_cmd_signal_schedule_routes(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    _add_routing_rule(tmp_path, "schedule", subject="schedule", input={"body": "schedule"})

    def fake_execute(endpoint, assistant, subject, graph_input, *, request_context=None, timeout=120,
                     caused_by_run_id=None, cascade_depth=0):
        return {"run_id": "r", "thread_id": "t", "status": "success", "values": {}}

    monkeypatch.setattr("cord_runtime.router.execute", fake_execute)
    code = cli.main(["--board", str(tmp_path), "signal", "schedule", "daily-digest", "2026-09-16T09:00:00+00:00"])
    assert code == cli.EXIT_OK


def test_cmd_signal_schedule_invalid_timestamp_exit_two(tmp_path):
    code = cli.main(["--board", str(tmp_path), "signal", "schedule", "daily-digest", "not-a-timestamp"])
    assert code == cli.EXIT_USAGE


def test_cmd_signal_file_missing_file_exit_two(tmp_path):
    code = cli.main(["--board", str(tmp_path), "signal", "file", str(tmp_path / "missing.json")])
    assert code == cli.EXIT_USAGE


# --- cli.py: cmd_view --watch (#20) -----------------------------------------

def test_cmd_view_watch_unknown_alias_exit_two(tmp_path):
    code = cli.main(["--board", str(tmp_path), "view", "--watch", "missing:t-1:r-1"])
    assert code == cli.EXIT_USAGE


def test_cmd_view_watch_malformed_target_exit_two(tmp_path):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    code = cli.main(["--board", str(tmp_path), "view", "--watch", "not-a-triple"])
    assert code == cli.EXIT_USAGE


def test_cmd_view_watch_renders_a_live_run(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli.requests, "get", _fake_probe_get)
    monkeypatch.setattr(cli, "check_freshness",
                        lambda board_dir, endpoint, **kw: FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT)))
    monkeypatch.setattr(AegraExecutionBackend, "describe_run", lambda self, thread_id, run_id: {
        "run_id": run_id, "thread_id": thread_id, "assistant_id": "fixture-a",
        "status": "running", "subject": "subject-1",
    })
    monkeypatch.setattr(AegraExecutionBackend, "describe_assistant",
                        lambda self, assistant_id: {"graph_id": "fixture-a"})

    def fake_watch(self, thread_id, run_id, *, timeout):
        yield "metadata", {}, f"{run_id}_event_0"
        yield "end", {"status": "success"}, f"{run_id}_event_1"

    monkeypatch.setattr(AegraExecutionBackend, "watch", fake_watch)

    code = cli.main(["--board", str(tmp_path), "view", "--watch", "aegra-local:t-1:r-1"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Live Run r-1 (thread t-1) [live, success]" in out


def test_cmd_view_watch_unresolvable_assistant_is_omitted_not_fatal(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli.requests, "get", _fake_probe_get)
    monkeypatch.setattr(cli, "check_freshness",
                        lambda board_dir, endpoint, **kw: FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT)))
    monkeypatch.setattr(AegraExecutionBackend, "describe_run", lambda self, thread_id, run_id: {
        "run_id": run_id, "thread_id": thread_id, "assistant_id": "unknown-assistant",
        "status": "running", "subject": None,
    })

    def fake_describe_assistant(self, assistant_id):
        raise RuntimeError("Aegra API request failed; check service readiness and input contract")

    monkeypatch.setattr(AegraExecutionBackend, "describe_assistant", fake_describe_assistant)

    code = cli.main(["--board", str(tmp_path), "view", "--watch", "aegra-local:t-1:r-1"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "Live Run" not in out
    assert "could not resolve a Graph" in err


def test_cmd_view_watch_unreachable_run_is_reported_not_fatal(tmp_path, monkeypatch, capsys):
    add_connection(tmp_path, "aegra-local", "http://127.0.0.1:2026")
    monkeypatch.setattr(cli.requests, "get", _fake_probe_get)
    monkeypatch.setattr(cli, "check_freshness",
                        lambda board_dir, endpoint, **kw: FreshnessCheck(status=ABSENT, reading=TopologyReading(status=ABSENT)))

    def fake_describe_run(self, thread_id, run_id):
        raise RuntimeError("Aegra API request failed; check service readiness and input contract")

    monkeypatch.setattr(AegraExecutionBackend, "describe_run", fake_describe_run)

    code = cli.main(["--board", str(tmp_path), "view", "--watch", "aegra-local:t-1:r-1"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "Live Run" not in out
    assert "check service readiness" in err
