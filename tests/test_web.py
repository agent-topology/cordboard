"""HTTP, rendering and shared-model contracts for the packaged browser surface."""
from contextlib import contextmanager
import json
from pathlib import Path
import threading

import pytest
import requests

from cord_runtime.live_reconciliation import LiveRun
from cord_runtime.run_continuity import record_submission
from cord_runtime.viewer import build_catalog, build_execution_tree
from cord_runtime.web.observation import Observation
from cord_runtime.web.presentation import (
    STATE_CATALOG,
    describe,
    graph_url,
    layout,
    run_topology,
    scenario_summary,
    timeline,
)
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


class BoardObservation(StaticObservation):
    """A StaticObservation that also resolves the authorized-inbox action slot (#69),
    exercising the same board/connections path a real Observation would."""
    def __init__(self, catalog, board, connections):
        super().__init__(catalog)
        self.board = board
        self._connections = connections

    def connections(self):
        return self._connections


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
        # "draft" matches the recorded Steps (#70); "verify" stays unobserved.
        graph["topology_structure"] = _document(["main"], nodes=("draft", "verify"))["graphs"][0]["structure"]
    return catalog


def test_scenario_summary_names_evidence_from_generic_vocabulary_only():
    recorded = catalog_fixture()["graphs"][0]["runs"]
    by_id = {r["run_id"]: r for r in recorded}
    assert scenario_summary(by_id["success"]) == "Recorded Run: passed"
    assert scenario_summary(by_id["retry"]) == "Recorded Run: 2 Attempts (failed, passed)"
    assert scenario_summary(by_id["interrupt"]) == "Recorded Run: paused, awaiting approval"
    live = {"run_id": "live-1", "aegra_status": "interrupted"}
    assert scenario_summary(live) == "Run: waiting on approval"
    assert scenario_summary({"run_id": "x", "aegra_status": "success"}) == "Run: finished successfully"


def test_index_first_run_orientation_offers_scenario_choices_with_bounded_actions(tmp_path):
    catalog = catalog_fixture()
    record_submission(tmp_path, "demo", "thread-1", "interrupt")
    connections = {"demo": {"endpoint": "http://localhost", "auth_endpoint": None}}
    with running(BoardObservation(catalog, tmp_path, connections)) as base:
        page = requests.get(base + "/").text
        # States what Cordboard connects and records (AC1).
        assert "Cordboard connects to registered Graph deployments and records" in page
        # Each recorded/live Run is an explicit, human-readable choice (AC1/AC2).
        assert 'Recorded Run: passed</a>' in page
        assert 'Recorded Run: 2 Attempts (failed, passed)</a>' in page
        assert 'Recorded Run: paused, awaiting approval</a>' in page
        # Exact identifiers stay available, in secondary markup (AC3).
        assert "<code>interrupt</code>" in page and "<code>success</code>" in page
        assert "Thread <code>thread-1</code>" in page
        # Links to the existing safe action surface, or a bounded reason it is absent (AC2).
        assert "/connections/demo/graphs/g/execute" in page
        assert "No approval authority configured for this deployment." in page
        # An idle graph (no recorded Runs) gets a bounded next action, not bare internal status (AC4).
        assert "No recorded Runs yet." in page and page.count("Submit an execution</a>") == 2
        # The shared JSON model is unaffected by the new HTML orientation (AC5 JSON stability).
        assert requests.get(base + "/?format=json").json() == json.loads(json.dumps(catalog))


def test_index_empty_catalog_gives_bounded_next_action():
    with running(StaticObservation({"graphs": []})) as base:
        page = requests.get(base + "/").text
        assert "cord add" in page and "No connections or recorded graphs yet" in page


def test_index_action_slot_links_directly_to_a_ready_approval(tmp_path, monkeypatch):
    from cord_runtime import approval_inbox
    catalog = catalog_fixture()
    record_submission(tmp_path, "demo", "thread-1", "interrupt")
    connections = {"demo": {"endpoint": "http://localhost", "auth_endpoint": "http://localhost:9"}}

    class Waiting:
        interrupt_id = "i-1"
    monkeypatch.setattr(approval_inbox, "discover_waiting_for_thread", lambda *a, **k: [Waiting()])
    with running(BoardObservation(catalog, tmp_path, connections)) as base:
        page = requests.get(base + "/").text
        assert "Respond to this approval</a>" in page
        assert "interrupt <code>i-1</code>" in page


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
        # An uncorrelated topology never hides the Run itself (#70 AC4): the
        # observed-path section falls back but the timeline/tree stay intact.
        run_response = requests.get(base + graph_url(graph) + "/runs/r")
        assert "Observed execution path" in run_response.text
        assert "Topology unavailable for correlation" in run_response.text
        assert "Execution timeline" in run_response.text


def test_run_topology_marks_only_observed_nodes_and_withholds_without_correlation():
    catalog = catalog_fixture()
    graph = catalog["graphs"][0]
    run = next(r for r in graph["runs"] if r["run_id"] == "success")
    path = run_topology(graph["topology_structure"], run)
    by_id = {node["id"]: node for node in path["nodes"]}
    assert by_id["draft"]["status"] == "passed" and len(by_id["draft"]["steps"]) == 1
    assert by_id["verify"]["status"] == "not_observed" and by_id["verify"]["steps"] == []
    assert path["unmatched_steps"] == []
    assert run_topology(None, run) is None


def test_run_topology_shows_a_paused_node_without_collapsing_its_evidence():
    catalog = catalog_fixture()
    graph = catalog["graphs"][0]
    run = next(r for r in graph["runs"] if r["run_id"] == "interrupt")
    path = run_topology(graph["topology_structure"], run)
    node = next(n for n in path["nodes"] if n["id"] == "draft")
    assert node["status"] == "paused" and node["steps"][0]["outcome"] == "awaiting_approval"


def test_run_topology_preserves_repeated_same_node_executions_instead_of_collapsing():
    trace_id = "b" * 32
    spans = dict([
        _span("run", span_id="2001", parent_id=None, trace_id=trace_id, graph_id="g", run_id="loop"),
        _span("step", span_id="2002", parent_id="2001", trace_id=trace_id, graph_id="g", run_id="loop",
              node="draft", outcome="passed", start=1100, end=1300),
        _span("step", span_id="2003", parent_id="2001", trace_id=trace_id, graph_id="g", run_id="loop",
              node="draft", outcome="failed", start=1400, end=1600),
    ])
    run = build_execution_tree(spans)[0]
    structure = _document(["main"], nodes=("draft",))["graphs"][0]["structure"]
    path = run_topology(structure, run)
    node = path["nodes"][0]
    assert node["status"] == "repeated"
    assert [step["outcome"] for step in node["steps"]] == ["passed", "failed"]


def test_run_topology_surfaces_unmatched_steps_without_guessing_a_node():
    spans = _run_tree("g", "r", node="undeclared")
    run = build_execution_tree(spans)[0]
    structure = _document(["main"], nodes=("draft",))["graphs"][0]["structure"]
    path = run_topology(structure, run)
    assert path["nodes"][0]["steps"] == [] and path["nodes"][0]["status"] == "not_observed"
    assert [step["node"] for step in path["unmatched_steps"]] == ["undeclared"]


def test_run_page_relates_observed_nodes_to_topology_with_a_text_alternative():
    catalog = catalog_fixture()
    graph = catalog["graphs"][0]
    with running(StaticObservation(catalog)) as base:
        page = requests.get(base + graph_url(graph) + "/runs/success").text
        assert "Observed execution path" in page
        assert 'data-node-status="passed"' in page and 'data-node-status="not_observed"' in page
        assert "draft — Passed" in page  # non-color text alternative (#70)
        assert "verify — Not observed" in page
        page = requests.get(base + graph_url(graph) + "/runs/interrupt").text
        assert 'data-node-status="paused"' in page
        assert "(awaiting resume)" in page
        # The shared JSON model is unaffected by the new HTML overlay (JSON stability).
        run = next(r for r in graph["runs"] if r["run_id"] == "success")
        response = requests.get(base + graph_url(graph) + "/runs/success?format=json")
        assert response.json() == json.loads(json.dumps(run))


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
        assert page.get_by_role("heading", name="What Cordboard shows here").count() == 1
        assert page.get_by_role("link", name="Recorded Run: passed", exact=True).count() == 1
        assert page.get_by_role("link", name="No recorded Runs yet.", exact=False).count() == 0  # prose, not a link
        page.screenshot(path=str(tmp_path / "index-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 375, "height": 667})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / "index-narrow.png"), full_page=True)
        page.keyboard.press("Tab")
        assert page.locator(":focus").count() == 1
        page.set_viewport_size({"width": 1280, "height": 900})
        page.get_by_role("link", name="g", exact=True).last.click()
        assert page.get_by_role("img", name="Published topology for g").count() == 1
        page.get_by_label("Status / declared outcome").fill("failed")
        page.get_by_role("button", name="Apply filters").click()
        assert page.get_by_role("link", name="Run retry", exact=True).count() == 1
        assert page.get_by_role("link", name="Run success", exact=True).count() == 0
        for run in ("success", "retry", "interrupt"):
            page.goto(base + "/connections/demo/graphs/g/runs/" + run)
            assert page.get_by_role("heading", name="Execution timeline").count() == 1
            assert page.get_by_role("img", name=f"Observed execution path for Run {run}").count() == 1
            page.screenshot(path=str(tmp_path / f"{run}-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 667})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / f"{run}-narrow.png"), full_page=True)
            page.keyboard.press("Tab")
            assert page.locator(":focus").count() == 1
            page.set_viewport_size({"width": 1280, "height": 900})
        assert errors == []
        browser.close()


def test_state_catalog_entries_are_all_complete():
    """Every cataloged state has the four facts #71 requires: a stable
    label, an explanation, an evidence/ownership cue, and a safe next
    action or an explicit statement that none is needed."""
    for family, entries in STATE_CATALOG.items():
        for key, entry in entries.items():
            for field in ("label", "explain", "evidence", "action"):
                value = entry.get(field)
                assert value, f"{family}.{key} is missing '{field}'"


def test_describe_reports_an_uncataloged_state_as_an_explicit_contract_gap():
    """A value the catalog does not know is a visible gap, never a guessed
    explanation or action (#71 Blockers and handoff)."""
    info = describe("topology_status", "some_future_status_not_yet_cataloged")
    assert info["label"] == "some_future_status_not_yet_cataloged"
    assert "contract gap" in info["action"]
    assert describe("nonexistent_family", "anything")["evidence"] == "not classified"


def test_empty_archive_is_visible_before_execution(tmp_path):
    observation = Observation(tmp_path, [tmp_path])
    assert observation.snapshot() == {"graphs": []}
    [message] = observation.diagnostics()
    assert "No archive files exist yet" in message
    assert "No action needed" in message
    observation.close()


_ARCHIVE_FIXTURES = Path(__file__).parent / "fixtures/escalations"
_ARCHIVE_COMPLETE_LINE = (_ARCHIVE_FIXTURES / "window.otlp.jsonl").read_text().splitlines()[0]


def test_incomplete_archive_write_is_visible_and_distinguishable(tmp_path):
    (tmp_path / "archive-2026-01-01.otlp.jsonl").write_text(_ARCHIVE_COMPLETE_LINE[:10])
    observation = Observation(tmp_path, [tmp_path])
    assert observation.snapshot() == {"graphs": []}
    [message] = observation.diagnostics()
    assert "still in progress" in message
    observation.close()


def test_corrupt_archive_record_is_a_distinguishable_failed_state_not_a_503(tmp_path):
    """archive_health's FAILED classification (#45/#71), not an uncaught
    ArchiveError 503ing the whole page: execution stays visible/empty and the
    failure is a labeled, bounded diagnostic instead."""
    record = json.loads(_ARCHIVE_COMPLETE_LINE)
    record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "not-hex"
    (tmp_path / "archive-2026-01-01.otlp.jsonl").write_text(json.dumps(record) + "\n")
    observation = Observation(tmp_path, [tmp_path])
    assert observation.snapshot() == {"graphs": []}  # no exception propagates
    [message] = observation.diagnostics()
    assert "failed validation" in message
    assert "Check the archive files" in message
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
