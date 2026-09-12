from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest
import requests

from cord_runtime.archive_check import check
from cord_runtime.archive_query import query
from examples.model_proxy.config import environment, load, validate
from examples.proxy_graph import execute
from examples.provider_smoke import smoke
from conftest import CREDENTIAL, PRIVATE_KEY, ROOT
from test_collector import collector, archived_spans, attributes


def config():
    return load(ROOT / "examples/model_proxy/config.yaml")


@pytest.mark.parametrize("field", ["fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"])
def test_cross_alias_fallback_rejected(field):
    value = config()
    value["router_settings"][field] = [{"fast": ["deep"]}]
    with pytest.raises(ValueError, match="graphs own escalation"):
        validate(value)


@pytest.mark.parametrize("field", ["callbacks", "success_callback", "failure_callback", "input_callback"])
def test_direct_callback_rejected(field):
    value = config()
    value["litellm_settings"][field] = ["langfuse"]
    with pytest.raises(ValueError, match="bypass the gate"):
        validate(value)


def test_same_alias_deployments_allowed():
    value = config()
    second = deepcopy(value["model_list"][0])
    second["litellm_params"].update(model="openai/replacement", order=1)
    value["model_list"].append(second)
    validate(value)


def test_inherited_configuration_cannot_bypass_gate(monkeypatch):
    for name in ("EXAMPLE_FAST_MODEL", "EXAMPLE_DEEP_MODEL", "EXAMPLE_PROVIDER_BASE", "EXAMPLE_PROVIDER_KEY"):
        monkeypatch.setenv(name, "fixture")
    for name in ("DATABASE_URL", "DEBUG_OTEL", "LANGFUSE_SECRET_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.setenv(name, "unwanted")
    env = environment(config(), "http://127.0.0.1:4318/v1/traces")
    assert not {"DATABASE_URL", "DEBUG_OTEL", "LANGFUSE_SECRET_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT"} & env.keys()


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(general_settings={"database_url": "unsupported"}),
    lambda c: c["litellm_settings"].update(turn_off_message_logging=False),
    lambda c: c["model_list"][0]["litellm_params"].update(fallbacks=["deep"]),
    lambda c: c["model_list"][0].update(model_name="standard"),
    lambda c: c["model_list"].pop(),
    lambda c: c["model_list"][0]["litellm_params"].update(api_key="literal-not-allowed"),
    lambda c: c["router_settings"].update(num_retries=3),
])
def test_unsupported_configuration_fails_closed(mutate):
    value = config()
    mutate(value)
    with pytest.raises(ValueError):
        validate(value)


@contextmanager
def provider(responses, response_model=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            index = len(calls)
            calls.append(body["model"])
            if index >= len(responses):
                self.send_error(500, "fixture exhausted")
                return
            if responses[index] is None:
                data = json.dumps({"error": {"message": CREDENTIAL, "type": "invalid_request_error"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            data = json.dumps({"id": f"fixture-{index}", "object": "chat.completion", "created": 1,
                               "model": response_model or body["model"],
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": responses[index]}}],
                               "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def proxy(tmp_path, upstream, collector_url, fast_model="openai/fixture-fast"):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "EXAMPLE_PROVIDER_BASE": upstream, "EXAMPLE_PROVIDER_KEY": "fixture-only",
           "EXAMPLE_FAST_MODEL": fast_model, "EXAMPLE_DEEP_MODEL": "openai/fixture-deep"}
    process = subprocess.Popen(
        [sys.executable, "-m", "examples.model_proxy.config", "--config", str(ROOT / "examples/model_proxy/config.yaml"),
         "--port", str(port), "--collector", collector_url],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, "proxy startup failed"
            try:
                if requests.get(endpoint + "/health/liveliness", timeout=.2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            assert time.monotonic() < deadline, "proxy readiness timed out"
            time.sleep(.1)
        yield endpoint
        # LiteLLM's callback scheduling is asynchronous even with a sync exporter.
        time.sleep(1)
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert CREDENTIAL.encode() not in output and PRIVATE_KEY.encode() not in output


@pytest.mark.collector
def test_proxy_escalation_archive(tmp_path, cli):
    directory = tmp_path / "archive"
    with collector(directory) as target, provider(("wrong", "still wrong", "hello")) as (upstream, calls):
        with proxy(tmp_path, upstream, target, "openai/" + CREDENTIAL) as endpoint:
            result = execute(endpoint, target)
    assert result.escalation_count == 1 and result.output == "hello"
    assert calls == [CREDENTIAL, CREDENTIAL, "fixture-deep"]
    saved = archived_spans(directory)
    attempts = sorted([s for s in saved if s["name"] == "attempt"], key=lambda s: attributes(s)["cord.step.attempt"])
    assert [attributes(s)["cord.outcome"] for s in attempts] == ["failed", "escalated", "passed"]
    assert all(attributes(s)["cord.redacted"] is True for s in saved)
    models = [s for s in saved if "gen_ai.request.model" in attributes(s)]
    assert len(models) == 3, [(s["name"], list(attributes(s))) for s in saved]
    assert all(attributes(s)["gen_ai.request.model"] != CREDENTIAL for s in models)
    assert {s["parentSpanId"] for s in models} == {s["spanId"] for s in attempts}
    assert len({s["traceId"] for s in saved}) == 1
    for model in models:
        parent = next(s for s in attempts if s["spanId"] == model["parentSpanId"])
        assert int(parent["startTimeUnixNano"]) <= int(model["startTimeUnixNano"]) <= int(model["endTimeUnixNano"]) <= int(parent["endTimeUnixNano"])
    raw = b"".join(p.read_bytes() for p in directory.glob("*.jsonl"))
    assert CREDENTIAL.encode() not in raw and b"Copy exactly:" not in raw and b"still wrong" not in raw
    assert check([directory], cli) == 0
    assert query([directory], reference=time.time_ns()) == [{"graph": "minimal-graph", "node": "draft", "count": 1}]


@pytest.mark.collector
@pytest.mark.parametrize("fast_model", ["openai/fixture-fast", "openai/replacement-fast"])
def test_provider_mapping_does_not_escalate(tmp_path, fast_model):
    directory = tmp_path / "archive"
    with collector(directory) as target, provider(("wrong", "hello")) as (upstream, calls):
        with proxy(tmp_path, upstream, target, fast_model) as endpoint:
            result = execute(endpoint, target)
    assert result.escalation_count == 0
    assert calls == [fast_model.removeprefix("openai/")] * 2
    assert query([directory], reference=time.time_ns()) == []


@pytest.mark.collector
def test_proxy_block_suppresses_model_span(tmp_path, cli):
    directory = tmp_path / "archive"
    with collector(directory) as target, provider(("hello",)) as (upstream, _):
        with proxy(tmp_path, upstream, target, "openai/" + PRIVATE_KEY) as endpoint:
            execute(endpoint, target)
    saved = archived_spans(directory)
    assert len(saved) == 5  # Run + three Steps + Attempt; blocked model span absent.
    assert not any(s["name"] == "chat" for s in saved)
    assert check([directory], cli) == 0


@pytest.mark.collector
def test_smoke_harness_with_controlled_responses(tmp_path, cli):
    directory = tmp_path / "archive"
    # Different arbitrary outputs still pass: this measures compatibility, not verdicts.
    with collector(directory) as target, provider(("arbitrary one", "arbitrary two")) as (upstream, _):
        with proxy(tmp_path, upstream, target) as endpoint:
            assert smoke(endpoint, target, directory, cli) == {
                "compatible": True, "aliases": 2, "attempts": 2, "model_spans": 2, "escalations": 0}


@pytest.mark.collector
def test_concurrent_proxy_runs_keep_attempt_parents(tmp_path):
    directory = tmp_path / "archive"
    with collector(directory) as target, provider(("hello", "hello")) as (upstream, _):
        with proxy(tmp_path, upstream, target) as endpoint, ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: execute(endpoint, target), range(2)))
    assert all(r.escalation_count == 0 for r in results)
    saved = archived_spans(directory)
    attempts = {s["spanId"]: s for s in saved if s["name"] == "attempt"}
    models = [s for s in saved if s["name"] == "chat"]
    assert len(models) == len(attempts) == 2
    assert len({s["traceId"] for s in models}) == 2
    assert {s["parentSpanId"] for s in models} == attempts.keys()
    assert all(s["traceId"] == attempts[s["parentSpanId"]]["traceId"] for s in models)


@pytest.mark.collector
def test_failure_telemetry_and_missing_context(tmp_path, cli):
    directory = tmp_path / "archive"
    with collector(directory) as target, provider((None, "hello")) as (upstream, _):
        with proxy(tmp_path, upstream, target) as endpoint:
            with pytest.raises(RuntimeError, match="model request failed"):
                execute(endpoint, target)
            response = requests.post(endpoint + "/v1/chat/completions", timeout=10,
                                     json={"model": "fast", "messages": [{"role": "user", "content": "hello"}]})
            assert response.status_code == 200
    saved = archived_spans(directory)
    attempts = [s for s in saved if s["name"] == "attempt"]
    models = [s for s in saved if s["name"] == "chat"]
    assert len(models) == len(attempts) == 1  # Unparented second request is suppressed.
    assert models[0]["parentSpanId"] == attempts[0]["spanId"]
    assert models[0]["status"]["code"] == 2
    assert attributes(attempts[0])["cord.outcome"] == "failed"
    assert query([directory], reference=time.time_ns()) == []
    assert check([directory], cli) == 0
