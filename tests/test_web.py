"""HTTP, rendering and shared-model contracts for the packaged browser surface."""
from contextlib import contextmanager
import json
import threading

import pytest
import requests

from cord_runtime.live_reconciliation import LiveRun
from cord_runtime.viewer import build_catalog
from cord_runtime.web.observation import Observation
from cord_runtime.web.presentation import graph_url, layout, timeline
from cord_runtime.web.server import make_server, serve
from test_viewer import _document, _run_tree, _merge, _span


class StaticObservation:
    def __init__(self, catalog):
        self.catalog = catalog
        self.updates = []

    def snapshot(self):
        return self.catalog

    def diagnostics(self):
        return []

    def run_updates(self, _):
        yield from self.updates


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


def catalog_fixture():
    spans = _merge(_run_tree("g", "success"),
                   _run_tree("g", "retry", outcomes=("failed", "passed"), tiers=(None, None)),
                   _run_tree("g", "interrupt"))
    for span, _ in spans.values():
        if span["attributes"]["cord.run.id"] == "interrupt" and span["name"].startswith("step:"):
            span["attributes"]["cord.outcome"] = "awaiting_approval"
    catalog = build_catalog({"demo": {"endpoint": "http://localhost", "graphs": ["g", "idle"],
                                     "reachable": True}}, {}, spans)
    for graph in catalog["graphs"]:
        graph["topology_status"] = "current"
        graph["topology_structure"] = _document(["main"])["graphs"][0]["structure"]
    return catalog


def test_distinct_routes_escaping_and_exact_json():
    connections = {alias: {"endpoint": "http://localhost", "graphs": ["g/<>"], "reachable": True}
                   for alias in ["a/b", "ambiguous"]}
    catalog = build_catalog(connections, {}, _run_tree("g/<>", "r/1"))
    assert len({graph_url(g) for g in catalog["graphs"]}) == 3
    with running(StaticObservation(catalog)) as base:
        for graph in catalog["graphs"]:
            url = base + graph_url(graph)
            assert requests.get(url + "?format=json").json() == json.loads(json.dumps(graph))
            response = requests.get(url)
            assert response.status_code == 200
            assert "g/&lt;&gt;" in response.text
            assert "g/<>" not in response.text
        ambiguous = next(g for g in catalog["graphs"] if g["topology_status"] == "ambiguous")
        assert requests.get(base + graph_url(ambiguous) + "/runs/r%2F1?format=json").json()["run_id"] == "r/1"
        assert requests.get(base + graph_url(catalog["graphs"][0]) + "/runs/r%2F1").status_code == 404


@pytest.mark.parametrize("state", ["absent", "invalid", "stale", "unreachable", "not_checked",
                                  "no_connection", "ambiguous", "current"])
def test_topology_states_keep_execution(state):
    catalog = build_catalog({}, {}, _run_tree("g", "r"))
    graph = catalog["graphs"][0]
    graph["topology_status"] = state
    with running(StaticObservation(catalog)) as base:
        response = requests.get(base + graph_url(graph))
        assert f'data-topology-status="{state}"' in response.text
        assert "Run r" in response.text
        assert "Topology unavailable for correlation" in response.text


def test_tree_timeline_filters_and_read_only():
    catalog = catalog_fixture()
    with running(StaticObservation(catalog)) as base:
        path = "/connections/demo/graphs/g"
        page = requests.get(base + path + "?status=failed").text
        assert "Run retry" in page
        assert "Run success" not in page
        page = requests.get(base + path + "/runs/interrupt").text
        assert "Awaiting resume" in page and 'class="cord-action-slot"' in page
        assert "Timeline incomplete" in page
        assert "Attempt 1" in page
        # Observation routes reject POST outright; only /execute and /approvals/... accept it (#49).
        assert requests.post(base + path, json={}).status_code == 404
        assert requests.get(base + "/static/../server.py").status_code == 404
        assert requests.get(base + path, headers={"Accept": "application/json"}).json()["runs"]


def test_sse_monotonic_reconnect_and_only_changed_snapshots():
    observation = StaticObservation(catalog_fixture())
    update = {"source": "live", "run": {"run_id": "success", "seconds_since_completion": 0}, "diagnostics": []}
    observation.updates = [update, update, {**update, "source": "recorded"}]
    with running(observation) as base:
        response = requests.get(base + "/connections/demo/graphs/g/runs/success/events",
                                headers={"Last-Event-ID": "41"})
        assert response.headers["Content-Type"] == "text/event-stream"
        assert "id: 42" in response.text and "id: 43" in response.text
        assert response.text.count("event: snapshot") == 2
        assert ": heartbeat" in response.text


def test_loopback_boundary(tmp_path):
    for host in ("0.0.0.0", "192.0.2.1", "example.com"):
        with pytest.raises(ValueError, match="loopback"):
            serve(tmp_path, host=host)


def test_layout_keeps_cycles_and_and_join_edges():
    structure = _document(["main"])["graphs"][0]["structure"]
    structure["edges"].append({"id": "back", "source": "omega", "target": "alpha", "kind": "conditional"})
    structure["joins"] = [{"id": "j", "sources": ["alpha"], "target": "omega"}]
    diagram = layout(structure)
    assert len(diagram["nodes"]) == 2 and len(diagram["edges"]) == 3
    assert diagram["edges"][-1]["kind"] == "AND join"


def test_wait_interval_is_exact_not_open_wait_duration():
    run = catalog_fixture()["graphs"][0]["runs"][0]
    run["steps"][0].update(approval_wait_ns=50)
    row = next(r for r in timeline(run) if r["kind"] == "wait")
    assert row["end"] - row["start"] == 50


def test_alias_scope_does_not_reassign_ambiguous_records(tmp_path, monkeypatch):
    from cord_runtime.connections import add_connection
    from cord_runtime import cli
    import cord_runtime.web.observation as module
    add_connection(tmp_path, "a", "http://localhost:1111")
    add_connection(tmp_path, "b", "http://localhost:2222")
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": ["g"]})
    monkeypatch.setattr(module, "check_freshness", lambda *a, **k: None)
    monkeypatch.setattr(module, "read_active_spans", lambda _: (_run_tree("g", "r"), ()))
    observation = Observation(tmp_path, [tmp_path], alias="a")
    catalog = observation.snapshot()
    own, ambiguous = catalog["graphs"]
    assert own["deployment_alias"] == "a" and own["runs"] == []
    assert ambiguous["topology_status"] == "ambiguous" and ambiguous["runs"][0]["run_id"] == "r"
    observation.close()


def test_disconnect_retains_graph_identity(tmp_path, monkeypatch):
    from cord_runtime.connections import add_connection
    from cord_runtime import cli
    import cord_runtime.web.observation as module
    add_connection(tmp_path, "a", "http://localhost:1111")
    monkeypatch.setattr(module, "check_freshness", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": True, "graphs": ["g"]})
    observation = Observation(tmp_path)
    observation.snapshot()
    monkeypatch.setattr(cli, "_probe", lambda _: {"reachable": False, "graphs": []})
    graph = observation.snapshot()["graphs"][0]
    assert graph["graph_id"] == "g" and graph["reachable"] is False
    observation.close()


def test_browser_dom_and_narrow_screenshots(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with running(StaticObservation(catalog_fixture())) as base, playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(base)
        page.get_by_role("link", name="g", exact=True).last.click()
        assert page.get_by_role("img", name="Published topology for g").count() == 1
        page.get_by_label("Status / declared outcome").fill("failed")
        page.get_by_role("button", name="Apply filters").click()
        assert page.get_by_role("link", name="Run retry", exact=True).count() == 1
        assert page.get_by_role("link", name="Run success", exact=True).count() == 0
        for run in ("success", "retry", "interrupt"):
            page.goto(base + "/connections/demo/graphs/g/runs/" + run)
            assert page.get_by_role("heading", name="Execution timeline").count() == 1
            page.screenshot(path=str(tmp_path / f"{run}-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 667})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / f"{run}-narrow.png"), full_page=True)
            page.keyboard.press("Tab")
            assert page.locator(":focus").count() == 1
            page.set_viewport_size({"width": 1280, "height": 900})
        assert errors == []
        browser.close()


def test_empty_archive_is_visible_before_execution(tmp_path):
    observation = Observation(tmp_path, [tmp_path])
    assert observation.snapshot() == {"graphs": []}
    assert observation.diagnostics() == ["No recorded spans yet"]
    observation.close()


def test_live_ingestion_deadlines_and_archive_replacement(tmp_path, monkeypatch):
    import cord_runtime.web.observation as module
    observation = Observation(tmp_path)
    live = LiveRun("r", "t", graph_id="g")
    live.status = "success"
    live.completed_at = 0
    observation.live["r"] = live
    monkeypatch.setattr(module.time, "monotonic", lambda: 6)
    stream = observation.run_updates("r")
    assert next(stream)["source"] == "ingestion_pending"
    monkeypatch.setattr(module.time, "monotonic", lambda: 301)
    assert next(stream)["source"] == "ingestion_failed"
    # Changing archive identity forces an actual tree read, then durable wins.
    archive = tmp_path / "new.otlp.jsonl"
    archive.write_text("placeholder")
    observation.archives = [archive]
    monkeypatch.setattr(observation, "read_archive", lambda: _run_tree("g", "r"))
    result = next(stream)
    assert result["source"] == "recorded" and result["run"]["steps"]
    observation.close()


def test_browser_poll_fallback_updates_without_stealing_focus():
    playwright = pytest.importorskip("playwright.sync_api")
    observation = StaticObservation(catalog_fixture())
    with running(observation) as base, playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.add_init_script("window.EventSource = undefined")
        page.goto(base + "/connections/demo/graphs/g")
        field = page.get_by_label("Subject", exact=True)
        field.fill("in-progress input")
        graph = observation.catalog["graphs"][0]
        graph["runs"][0]["subject_id"] = "updated-subject"
        page.wait_for_timeout(3500)
        assert field.input_value() == "in-progress input"
        assert field.evaluate("(e) => e === document.activeElement")
        page.locator("h1").click()
        playwright.expect(page.get_by_text("Subject: updated-subject", exact=True)).to_be_visible(timeout=10000)
        browser.close()


def test_errors_are_legible_and_do_not_echo_payload():
    class Failed(StaticObservation):
        def snapshot(self):
            raise ValueError("sensitive payload must not be shown")
    with running(Failed({})) as base:
        response = requests.get(base)
        assert response.status_code == 503
        assert "Observation unavailable" in response.text
        assert "sensitive payload" not in response.text
        assert 'role="alert"' in response.text
