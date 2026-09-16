"""`cord add` / `cord list` / `cord run` (#12): storage, reachability, execution.

No live Aegra/Postgres/Docker here (that stack is covered by tests/test_aegra.py).
The connection contract and command exit statuses are deterministic without it.
"""

import json

import pytest
import requests

from cord_runtime import cli
from cord_runtime.connections import (
    InvalidConnection,
    add_connection,
    connections_path,
    get_connection,
    load_connections,
    validate_endpoint,
)


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


# --- cli.py: cmd_add -----------------------------------------------------

def test_cmd_add_success_exit_zero(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "add", "aegra-local", "http://127.0.0.1:2026"])
    assert code == cli.EXIT_OK
    assert "added" in capsys.readouterr().out


def test_cmd_add_invalid_endpoint_exit_two(tmp_path, capsys):
    code = cli.main(["--board", str(tmp_path), "add", "aegra-local", "not-a-url"])
    assert code == cli.EXIT_USAGE
    assert "cord:" in capsys.readouterr().err


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
