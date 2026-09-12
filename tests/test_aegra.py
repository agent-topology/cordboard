from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import os
import signal
import socket
import subprocess
import time
from uuid import uuid4

import pytest
import requests

from cord_runtime.aegra_client import execute
from cord_runtime.archive_check import check
from cord_runtime.archive_query import query
from examples.aegra_server import environment
from conftest import CREDENTIAL, PRIVATE_KEY, ROOT
from test_collector import archived_spans, attributes, collector, send
from test_proxy import provider, proxy
from test_telemetry import request, spans

POSTGRES_IMAGE = "postgres:16.10-alpine@sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297"


def test_aegra_environment_disables_bypass(monkeypatch):
    for name in ("OTEL_TARGETS", "OTEL_CONSOLE_EXPORT", "LANGSMITH_TRACING",
                 "LANGCHAIN_TRACING_V2", "DATABASE_URL", "AEGRA_CONFIG", "REDIS_URL"):
        monkeypatch.setenv(name, "untrusted")
    env = environment()
    assert env["OTEL_TARGETS"] == ""
    assert env["OTEL_CONSOLE_EXPORT"] == env["LANGSMITH_TRACING"] == "false"
    assert env["LANGCHAIN_TRACING_V2"] == "false"
    assert "DATABASE_URL" not in env and "REDIS_URL" not in env
    assert env["AEGRA_CONFIG"] == str(ROOT / "aegra/aegra.json")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def postgres():
    name = "cordboard-test-" + uuid4().hex
    target = port()
    # One disposable database service; no user's volumes or database are used.
    subprocess.run(["docker", "run", "--detach", "--rm", "--name", name,
                    "--publish", f"127.0.0.1:{target}:5432",
                    "--env", "POSTGRES_PASSWORD=fixture-only",
                    "--env", "POSTGRES_DB=cordboard", POSTGRES_IMAGE],
                   check=True, capture_output=True, timeout=90)
    try:
        deadline = time.monotonic() + 30
        while subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                             capture_output=True, timeout=5).returncode:
            assert time.monotonic() < deadline, "Postgres readiness timed out"
            time.sleep(.1)
        yield target
    finally:
        subprocess.run(["docker", "stop", "--time", "5", name],
                       check=True, capture_output=True, timeout=15)


@contextmanager
def aegra(postgres, proxy_url, collector_url, config=None):
    python = ROOT / "aegra/.venv/bin/python"
    assert python.is_file(), "run uv sync --locked --project aegra first"
    target = port()
    env = {**os.environ, "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": str(postgres),
           "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "fixture-only",
           "POSTGRES_DB": "cordboard"}
    command = [str(python), "-m", "examples.aegra_server", "--port", str(target),
               "--collector", collector_url]
    if proxy_url:
        command.extend(["--proxy", proxy_url])
    if config:
        command.extend(["--config", str(config)])
    process = subprocess.Popen(
        command,
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    endpoint = f"http://127.0.0.1:{target}"
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, "Aegra startup failed"
            try:
                if requests.get(endpoint + "/health", timeout=.3).status_code == 200:
                    break
            except requests.RequestException:
                pass
            assert time.monotonic() < deadline, "Aegra readiness timed out"
            time.sleep(.1)
        yield endpoint
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            output = process.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        assert CREDENTIAL.encode() not in output and PRIVATE_KEY.encode() not in output


def assert_tree(saved):
    indexed = {s["spanId"]: s for s in saved}
    for span in saved:
        assert attributes(span)["cord.redacted"] is True
        if span["name"] == "run":
            assert not span.get("parentSpanId")
            continue
        parent = indexed[span["parentSpanId"]]  # Any orphan fails, including proxy spans.
        assert span["traceId"] == parent["traceId"]
        assert int(parent["startTimeUnixNano"]) <= int(span["startTimeUnixNano"])
        assert int(span["endTimeUnixNano"]) <= int(parent["endTimeUnixNano"])
        expected = "attempt" if span["name"] == "chat" else (
            "step:draft" if span["name"] == "attempt" else "run")
        assert parent["name"] == expected


@pytest.mark.aegra
@pytest.mark.collector
def test_aegra_integrated_exit(tmp_path, cli, postgres):
    directory = tmp_path / "archive"
    subject = "opaque subject label"
    responses = ("wrong", "wrong", "wrong", "hello", "wrong", "wrong", "hello",
                 "hello.", "wrong", "hello")
    with collector(directory) as target, provider(responses) as (upstream, calls):
        with proxy(tmp_path, upstream, target, "openai/" + CREDENTIAL) as model_url:
            with aegra(postgres, model_url, target) as endpoint:
                results = [execute(endpoint, "minimal-graph", subject,
                                   {"subject": subject, "source": "hello"}) for _ in range(5)]
                # Read the first failed checkpoint after subsequent executions.
                old = requests.get(endpoint + f'/threads/{results[0]["thread_id"]}/state', timeout=5)
                assert old.status_code == 200
                assert old.json()["values"] == results[0]["values"]
    assert len(calls) == 10
    assert len({r["run_id"] for r in results}) == len({r["thread_id"] for r in results}) == 5
    drafts = [r["values"]["steps"][1] for r in results]
    assert [d["outcome"] for d in drafts] == ["failed", "passed", "passed", "repaired", "passed"]
    assert [len(d["attempts"]) for d in drafts] == [3, 1, 3, 1, 2]
    assert [r["values"]["output"] for r in results] == [None, "hello", "hello", "hello", "hello"]
    saved = archived_spans(directory)
    assert len(saved) == 40  # 5 Runs + 15 Steps + 10 Attempts + 10 proxy calls.
    assert_tree(saved)
    roots = [s for s in saved if s["name"] == "run"]
    assert {attributes(s)["cord.run.id"] for s in roots} == {r["run_id"] for r in results}
    assert len({s["traceId"] for s in roots}) == 5
    assert {attributes(s)["cord.subject.id"] for s in roots} == {subject}
    attempts = sorted([s for s in saved if s["name"] == "attempt" and
                       attributes(s)["cord.run.id"] == results[2]["run_id"]],
                      key=lambda s: attributes(s)["cord.step.attempt"])
    assert [attributes(s)["cord.outcome"] for s in attempts] == ["failed", "escalated", "passed"]
    expected = [{"graph": "minimal-graph", "node": "draft", "count": 2}]
    assert query([directory], reference=time.time_ns()) == expected
    # Re-delivery of this exact OTLP is deduplicated without changing the archive.
    files = list(directory.glob("*.otlp.jsonl"))
    assert query(files + files, reference=time.time_ns()) == expected
    raw = b"".join(p.read_bytes() for p in files)
    assert all(value.encode() not in raw for value in (CREDENTIAL, PRIVATE_KEY, "Copy exactly:", "wrong"))
    assert check([directory], cli) == 0


@pytest.mark.aegra
@pytest.mark.collector
def test_concurrent_aegra_runs(tmp_path, postgres):
    directory = tmp_path / "concurrent"
    with collector(directory) as target, provider(("hello", "hello")) as (upstream, _):
        with proxy(tmp_path, upstream, target) as model_url:
            with aegra(postgres, model_url, target) as endpoint, ThreadPoolExecutor(2) as pool:
                def invoke(subject):
                    return execute(endpoint, "minimal-graph", subject,
                                   {"subject": subject, "source": "hello"})
                results = list(pool.map(invoke, ("first subject", "second subject")))
    assert len({r["thread_id"] for r in results}) == 2
    saved = archived_spans(directory)
    assert len(saved) == 12
    assert_tree(saved)
    assert len({s["traceId"] for s in saved}) == 2
    for result, subject in zip(results, ("first subject", "second subject")):
        own = [attributes(s) for s in saved if attributes(s).get("cord.run.id") == result["run_id"]]
        assert len(own) == 5
        assert all(s["cord.subject.id"] == subject for s in own)


@pytest.mark.aegra
@pytest.mark.collector
@pytest.mark.parametrize("blocked", [False, True])
def test_aegra_redaction_and_proxy_block(tmp_path, cli, postgres, blocked):
    directory = tmp_path / "redaction"
    subject = "fixture:" + CREDENTIAL
    model = PRIVATE_KEY if blocked else CREDENTIAL
    with collector(directory) as target, provider(("hello",)) as (upstream, _):
        with proxy(tmp_path, upstream, target, "openai/" + model) as model_url:
            with aegra(postgres, model_url, target) as endpoint:
                result = execute(endpoint, "minimal-graph", subject,
                                 {"subject": subject, "source": "hello"})
    assert result["values"]["output"] == "hello"
    saved = archived_spans(directory)
    assert len(saved) == (5 if blocked else 6)
    assert_tree(saved)
    assert sum(s["name"] == "chat" for s in saved) == (0 if blocked else 1)
    assert all(CREDENTIAL not in attributes(s).get("cord.subject.id", "") for s in saved)
    raw = b"".join(p.read_bytes() for p in directory.glob("*.otlp.jsonl"))
    assert CREDENTIAL.encode() not in raw and PRIVATE_KEY.encode() not in raw
    assert query([directory], reference=time.time_ns()) == []
    assert check([directory], cli) == 0


@pytest.mark.collector
@pytest.mark.parametrize("service", ["cordboard-aegra", "cordboard-proxy"])
@pytest.mark.parametrize("stamp", [None, False])
def test_deployment_gate_in_fresh_archive(tmp_path, service, stamp):
    # Deliberately bypass processing at the wire boundary. This measures the
    # shared Collector gate, separately from the integrated positive trace.
    directory = tmp_path / "negative"
    req = request(CREDENTIAL)
    req.resource_spans[0].resource.attributes.add(key="service.name").value.string_value = service
    if stamp is not None:
        spans(req)[0].attributes.add(key="cord.redacted").value.bool_value = stamp
    with collector(directory) as target:
        send(target, req)
    assert archived_spans(directory) == []
    assert all(not p.read_bytes() for p in directory.glob("*.otlp.jsonl"))


@pytest.mark.aegra
@pytest.mark.collector
def test_graph_execution_without_model_configuration(tmp_path, cli, postgres):
    # The caller knows this graph's payload; the shared client/runtime do not.
    # No provider fixture, LiteLLM process, model mapping or API key is involved.
    directory = tmp_path / "archive"
    with collector(directory) as target:
        with aegra(postgres, None, target, ROOT / "aegra/opaque.json") as endpoint:
            first = execute(endpoint, "opaque-graph", "opaque grouping label",
                            {"items": [CREDENTIAL]})
            second = execute(endpoint, "opaque-graph", "opaque grouping label",
                             {"items": []})
    assert first["values"]["size"] == 1 and second["values"]["size"] == 0
    assert first["run_id"] != second["run_id"]
    assert first["thread_id"] != second["thread_id"]
    saved = archived_spans(directory)
    assert len(saved) == 6
    indexed = {s["spanId"]: s for s in saved}
    roots = [s for s in saved if s["name"] == "run"]
    assert {attributes(s)["cord.run.id"] for s in roots} == {first["run_id"], second["run_id"]}
    assert len({s["traceId"] for s in roots}) == 2
    for span in saved:
        attrs = attributes(span)
        assert attrs["cord.redacted"] is True
        assert attrs["cord.graph.id"] == "opaque-graph"
        assert "cord.tier" not in attrs
        assert not any(k.startswith("gen_ai.") for k in attrs)
        if span["name"] == "run":
            assert not span.get("parentSpanId")
        else:
            parent = indexed[span["parentSpanId"]]
            assert parent["traceId"] == span["traceId"]
            assert parent["name"] == ("step:count" if span["name"] == "attempt" else "run")
            assert int(parent["startTimeUnixNano"]) <= int(span["startTimeUnixNano"])
            assert int(span["endTimeUnixNano"]) <= int(parent["endTimeUnixNano"])
    assert query([directory], reference=time.time_ns()) == []
    assert CREDENTIAL.encode() not in b"".join(p.read_bytes() for p in directory.glob("*.otlp.jsonl"))
    assert check([directory], cli) == 0
