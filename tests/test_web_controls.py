"""Real browser + real ``cord serve`` process, scripted Aegra/entity HTTP only
(#49, ADR-0019 SS9) -- the same ``requests.Session.request`` monkeypatch
``tests/test_aegra_client.py`` already uses for the CLI/library layer,
extended to also script the entity ``auth_endpoint`` port. The browser and
the Cordboard HTTP server are both real, talking over real loopback sockets;
only the upstream Aegra/entity processes are faked.
"""
from contextlib import contextmanager
import threading

import pytest
import requests

from cord_runtime.connections import add_connection
from cord_runtime.web.observation import Observation
from cord_runtime.web.server import make_server

ENDPOINT = "http://127.0.0.1:9"  # never dialed; requests are faked below.
AUTH_ENDPOINT = "http://127.0.0.1:9/auth"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


_real_request = requests.Session.request


def _install(monkeypatch, script):
    """script maps a URL suffix to a FakeResponse, or a list consumed in order.

    Only requests to the faked Aegra/entity endpoints are intercepted; a
    request to the real ``cord serve`` process under test (any other host)
    passes through to the real ``requests.Session.request``, so this can
    coexist with the test's own HTTP client talking to the local server.
    """
    calls = []

    def fake_request(self, method, url, **kwargs):
        if not (url.startswith(ENDPOINT) or url.startswith(AUTH_ENDPOINT)):
            return _real_request(self, method, url, **kwargs)
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        for suffix, response in script.items():
            if url.endswith(suffix):
                return response.pop(0) if isinstance(response, list) else response
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def _base_script(*, accept=True):
    return {
        "/health": FakeResponse(200, {}),
        "/assistants": FakeResponse(200, {"assistants": [
            {"assistant_id": "demo-assistant", "graph_id": "g", "name": "Demo"}]}),
        "/.well-known/agent-topology.manifest.json": FakeResponse(404, {}),
        "/assistants/demo-assistant": FakeResponse(200, {"graph_id": "g"}),
        "/threads": [FakeResponse(200, {"thread_id": "t-1"})],
        "/threads/t-1/runs": [FakeResponse(200, {"run_id": "r-1"}), FakeResponse(200, {"run_id": "r-2"})],
        "/threads/t-1/runs/r-1": FakeResponse(200, {"status": "interrupted", "assistant_id": "demo-assistant",
                                                     "subject": "sub-1"}),
        "/threads/t-1/runs/r-2": FakeResponse(200, {"status": "success", "assistant_id": "demo-assistant"}),
        "/threads/t-1/state": FakeResponse(200, {
            "tasks": [{"interrupts": [{"id": "int-1", "value": {"question": "approve?"}}]}],
            "checkpoint": {"checkpoint_id": "chk-1"}}),
        "/auth/authorize": FakeResponse(200, {"accepted": accept, "reason": "policy" if not accept else "ok"}),
    }


@contextmanager
def running(observation):
    with make_server(observation) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def _origin_and_cookies(session, base):
    """A GET establishes the CSRF cookie; returns (origin header, cookie)."""
    response = session.get(base + "/")
    cookie = session.cookies.get("cord_csrf")
    return {"Origin": base}, cookie


def test_submit_pause_authorized_resume_via_browser(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    add_connection(tmp_path, "demo", ENDPOINT, auth_endpoint=AUTH_ENDPOINT)
    calls = _install(monkeypatch, _base_script(accept=True))
    observation = Observation(tmp_path)
    with running(observation) as base, playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(base + "/connections/demo/graphs/g/execute")
        page.get_by_label("Subject").fill("sub-1")
        page.get_by_label("Input (JSON)").fill('{"x": 1}')
        page.get_by_role("button", name="Submit execution").click()
        page.wait_for_url("**/runs/r-1")  # execute() succeeded and recorded the submission (AC1)

        page.goto(base + "/approvals")
        page.get_by_role("link", name="Review and respond").click()
        assert page.get_by_text("approve?").count() == 1  # bounded business content, not invented (SS7)
        page.get_by_label("Approver").fill("alice")
        page.get_by_label("Response (JSON)").fill("true")
        page.get_by_role("button", name="Submit response").click()
        assert page.get_by_text("resumed").count() >= 1  # the durable disposition, not a guess (AC3)
        browser.close()
    observation.close()
    # The interrupt-response port was consulted, not bypassed; the browser's
    # `approver` field was never treated as authority on its own.
    assert any(c["url"].endswith("/auth/authorize") for c in calls)


def test_rejected_response_via_browser(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    add_connection(tmp_path, "demo", ENDPOINT, auth_endpoint=AUTH_ENDPOINT)
    calls = _install(monkeypatch, _base_script(accept=False))
    observation = Observation(tmp_path)
    with running(observation) as base, playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(base + "/connections/demo/graphs/g/execute")
        page.get_by_label("Subject").fill("sub-1")
        page.get_by_label("Input (JSON)").fill("{}")
        page.get_by_role("button", name="Submit execution").click()
        page.wait_for_url("**/runs/r-1")

        page.goto(base + "/approvals")
        page.get_by_role("link", name="Review and respond").click()
        page.get_by_label("Approver").fill("mallory")
        page.get_by_label("Response (JSON)").fill("true")
        page.get_by_role("button", name="Submit response").click()
        assert page.get_by_text("rejected").count() >= 1
        assert page.get_by_text("policy").count() >= 1
        browser.close()
    observation.close()
    # A rejected decision must never reach resume: no second POST to /threads/t-1/runs.
    resume_posts = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/threads/t-1/runs")]
    assert len(resume_posts) == 1  # only the original execute submission, no resume attempt


def test_rejected_response_reason_is_labeled_and_bounded(tmp_path, monkeypatch):
    """The HTML respond path renders the cataloged `submission_result` label
    and explanation (#71), and never forwards the boundary's own `reason`
    unbounded -- a misbehaving auth_endpoint cannot flood the page."""
    add_connection(tmp_path, "demo", ENDPOINT, auth_endpoint=AUTH_ENDPOINT)
    long_reason = "x" * 500
    script = _base_script(accept=False)
    script["/auth/authorize"] = FakeResponse(200, {"accepted": False, "reason": long_reason})
    _install(monkeypatch, script)
    observation = Observation(tmp_path)
    with running(observation) as base:
        with requests.Session() as session:
            headers, cookie = _origin_and_cookies(session, base)
            form_page = session.get(base + "/connections/demo/graphs/g/execute", headers=headers).text
            nonce = form_page.split('name="nonce" value="')[1].split('"')[0]
            body = {"csrf_token": cookie, "nonce": nonce, "assistant": "demo-assistant",
                    "subject": "sub-1", "input": "{}", "context": ""}
            session.post(base + "/connections/demo/graphs/g/execute", data=body,
                        headers=headers, allow_redirects=False)
            detail_page = session.get(base + "/approvals/demo/t-1/int-1", headers=headers).text
            revision = detail_page.split('name="revision" value="')[1].split('"')[0]
            respond_body = {"csrf_token": cookie, "revision": revision,
                           "approver": "mallory", "response_value": "true"}
            response = session.post(base + "/approvals/demo/t-1/int-1", data=respond_body,
                                    headers=headers, allow_redirects=False)
    observation.close()
    page = response.text
    assert response.status_code == 409
    assert 'data-submission-status="rejected"' in page
    assert "Rejected." in page
    assert "configured authorization boundary declined" in page
    assert ("x" * 200) in page
    assert ("x" * 201) not in page  # bounded, never the full raw boundary text


def test_unknown_transport_outcome_via_browser(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    add_connection(tmp_path, "demo", ENDPOINT, auth_endpoint=AUTH_ENDPOINT)
    script = _base_script(accept=True)
    del script["/threads/t-1/runs"]  # resume's POST is scripted below to fail ambiguously.

    def fake_request(self, method, url, **kwargs):
        if not (url.startswith(ENDPOINT) or url.startswith(AUTH_ENDPOINT)):
            return _real_request(self, method, url, **kwargs)
        if url.endswith("/threads/t-1/runs") and kwargs.get("json", {}).get("command"):
            raise requests.ConnectionError("simulated ambiguous transport failure")
        for suffix, response in script.items():
            if url.endswith(suffix):
                return response.pop(0) if isinstance(response, list) else response
        if url.endswith("/threads/t-1/runs"):
            return FakeResponse(200, {"run_id": "r-1"})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    observation = Observation(tmp_path)
    with running(observation) as base, playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(base + "/connections/demo/graphs/g/execute")
        page.get_by_label("Subject").fill("sub-1")
        page.get_by_label("Input (JSON)").fill("{}")
        page.get_by_role("button", name="Submit execution").click()
        page.wait_for_url("**/runs/r-1")

        page.goto(base + "/approvals")
        page.get_by_role("link", name="Review and respond").click()
        page.get_by_label("Approver").fill("alice")
        page.get_by_label("Response (JSON)").fill("true")
        page.get_by_role("button", name="Submit response").click()
        assert page.get_by_text("unknown").count() >= 1  # never a guessed success (Verification)
        browser.close()
    observation.close()


def test_execute_double_submit_does_not_duplicate_thread(tmp_path, monkeypatch):
    add_connection(tmp_path, "demo", ENDPOINT)
    calls = _install(monkeypatch, _base_script())
    observation = Observation(tmp_path)
    with running(observation) as base:
        with requests.Session() as session:
            headers, cookie = _origin_and_cookies(session, base)
            form_page = session.get(base + "/connections/demo/graphs/g/execute", headers=headers).text
            nonce = form_page.split('name="nonce" value="')[1].split('"')[0]
            body = {"csrf_token": cookie, "nonce": nonce, "assistant": "demo-assistant",
                    "subject": "sub-1", "input": "{}", "context": ""}
            first = session.post(base + "/connections/demo/graphs/g/execute", data=body,
                                 headers=headers, allow_redirects=False)
            assert first.status_code == 303
            second = session.post(base + "/connections/demo/graphs/g/execute", data=body,
                                  headers=headers, allow_redirects=False)
            assert second.status_code == 409  # nonce already consumed; no second execution attempted
    observation.close()
    thread_posts = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/threads")]
    assert len(thread_posts) == 1  # exactly one Thread was ever created


@pytest.mark.parametrize("wrong_token", ["not-the-cookie-value", "\u2603"])
def test_cross_origin_and_missing_csrf_post_rejected(tmp_path, monkeypatch, wrong_token):
    add_connection(tmp_path, "demo", ENDPOINT)
    calls = _install(monkeypatch, _base_script())
    observation = Observation(tmp_path)
    with running(observation) as base:
        with requests.Session() as session:
            session.get(base + "/")  # establishes the cord_csrf cookie
            body = {"csrf_token": wrong_token, "nonce": "irrelevant",
                    "assistant": "demo-assistant", "subject": "sub-1", "input": "{}"}
            wrong_csrf = session.post(base + "/connections/demo/graphs/g/execute", data=body,
                                      headers={"Origin": base})
            assert wrong_csrf.status_code == 403

            cookie = session.cookies.get("cord_csrf")
            body["csrf_token"] = cookie
            missing_origin = session.post(base + "/connections/demo/graphs/g/execute", data=body)
            assert missing_origin.status_code == 403

            wrong_origin = session.post(base + "/connections/demo/graphs/g/execute", data=body,
                                        headers={"Origin": "http://evil.example"})
            assert wrong_origin.status_code == 403
    observation.close()
    # No execute() call ever reached the backend: no /threads POST at all.
    assert not any(c["url"].endswith("/threads") and c["method"] == "POST" for c in calls)


@pytest.mark.parametrize("token", ["forged-token", "\u2603"])
def test_forged_matching_csrf_tokens_rejected(tmp_path, monkeypatch, token):
    add_connection(tmp_path, "demo", ENDPOINT)
    calls = _install(monkeypatch, _base_script())
    observation = Observation(tmp_path)
    with running(observation) as base:
        response = requests.post(base + "/connections/demo/graphs/g/execute",
                                 headers={"Origin": base, "Cookie": "cord_csrf=forged-token"},
                                 data={"csrf_token": token})
        assert response.status_code == 403
    observation.close()
    assert not calls


def test_execute_rejects_assistant_from_another_graph(tmp_path, monkeypatch):
    add_connection(tmp_path, "demo", ENDPOINT)
    script = _base_script()
    script["/assistants"] = FakeResponse(200, {"assistants": [
        {"assistant_id": "demo-assistant", "graph_id": "g", "name": "Demo"},
        {"assistant_id": "other-assistant", "graph_id": "other", "name": "Other"}]})
    calls = _install(monkeypatch, script)
    observation = Observation(tmp_path)
    with running(observation) as base, requests.Session() as session:
        headers, cookie = _origin_and_cookies(session, base)
        path = base + "/connections/demo/graphs/g/execute"
        html = session.get(path).text
        assert 'value="other-assistant"' not in html
        nonce = html.split('name="nonce" value="')[1].split('"')[0]
        response = session.post(path, headers=headers, data={
            "csrf_token": cookie, "nonce": nonce, "assistant": "other-assistant",
            "subject": "sub-1", "input": "{}"})
        assert response.status_code == 400
    observation.close()
    assert not any(c["method"] == "POST" for c in calls)
