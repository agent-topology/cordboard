"""Generic catalog and topology/recorded-execution viewer (#13).

Uses two small, unrelated, model-free fixture Graphs throughout (never a
real example Graph's Node names), a real local topology HTTP fixture (same
pattern as tests/test_topology.py) for drift, and hand-built normalized spans
for recorded execution -- the same shape cord_runtime.archive_query.read_spans
produces, so build_execution_tree is exercised exactly as the CLI uses it.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from agent_topology.spec import finalize_document
import pytest

from cord_runtime.archive_query import ArchiveError
from cord_runtime.live_reconciliation import INGESTION_FAILED, INGESTION_PENDING, LIVE
from cord_runtime.topology import (
    WELL_KNOWN_PATH,
    check_freshness,
    refresh_snapshot,
)
from cord_runtime.viewer import (
    CURRENT,
    NO_CONNECTION,
    NOT_CHECKED,
    STALE,
    build_catalog,
    build_execution_tree,
    correlate_topology,
    format_catalog_text,
)


# --- local topology HTTP fixture (mirrors tests/test_topology.py) ----------

@contextmanager
def publisher(response):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != WELL_KNOWN_PATH:
                self.send_error(404)
                return
            status = response.get("status", 200)
            body = response.get("body", b"")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _document(graph_ids, *, nodes=("alpha", "omega"), gaps=None, producer_limitations=None):
    def graph(gid):
        return {
            "id": gid,
            "structure": {
                "nodes": [{"id": n} for n in nodes],
                "edges": [{"id": "e1", "source": nodes[0], "target": nodes[-1], "kind": "direct"}],
                "joins": [],
                "entryNodeIds": [nodes[0]],
                "exitNodeIds": [nodes[-1]],
            },
        }
    return finalize_document({
        "topologyVersion": "0.1",
        "provenance": {
            "generatedAt": "2026-09-15T00:00:00Z",
            "producer": {"name": "test-fixture", "version": "0.0.0"},
            "framework": {"name": "test", "version": "0.0.0"},
        },
        "producerLimitations": producer_limitations or [],
        "graphs": [graph(gid) for gid in graph_ids],
        "completeness": {"status": "incomplete" if gaps else "complete", "gaps": gaps or []},
    })


# --- hand-built normalized spans (read_spans's output shape) ---------------

_COUNTER = iter(range(1, 10_000))


def _sid():
    return format(next(_COUNTER), "016x")


def _span(kind, *, span_id, parent_id, trace_id, graph_id, run_id, subject_type="fixture",
         subject_id="urn:test:1", node=None, outcome=None, attempt=None, tier=None,
         semconv="0.3.0", start=1000, end=2000, resumed_from=None):
    attrs = {"cord.graph.id": graph_id, "cord.run.id": run_id,
             "cord.subject.type": subject_type, "cord.subject.id": subject_id}
    if kind == "run":
        name = "run"
        attrs["cord.semconv.version"] = semconv
    elif kind == "step":
        name = f"step:{node}"
        attrs["cord.node.name"] = node
        attrs["cord.outcome"] = outcome
        if resumed_from is not None:
            attrs["cord.resumed_from"] = resumed_from
    else:
        name = "attempt"
        attrs["cord.node.name"] = node
        attrs["cord.outcome"] = outcome
        attrs["cord.step.attempt"] = attempt
        if tier is not None:
            attrs["cord.tier"] = tier
    span = {"spanId": span_id, "parentSpanId": parent_id, "traceId": trace_id, "name": name,
            "startTimeUnixNano": start, "endTimeUnixNano": end, "attributes": attrs}
    return span_id, (span, "test")


def _run_tree(graph_id, run_id, *, node="draft", outcomes=("passed",), tiers=(None,),
             subject_id="urn:test:1", semconv="0.3.0"):
    """One Run with one Step and len(outcomes) Attempts, as a spans dict."""
    trace_id = "a" * 32
    run_sid, run_entry = _span("run", span_id=_sid(), parent_id=None, trace_id=trace_id,
                                graph_id=graph_id, run_id=run_id, subject_id=subject_id, semconv=semconv)
    step_sid, step_entry = _span("step", span_id=_sid(), parent_id=run_sid, trace_id=trace_id,
                                  graph_id=graph_id, run_id=run_id, subject_id=subject_id,
                                  node=node, outcome="passed", start=1100, end=1900)
    spans = dict([(run_sid, run_entry), (step_sid, step_entry)])
    for i, (outcome, tier) in enumerate(zip(outcomes, tiers), start=1):
        a_sid, a_entry = _span("attempt", span_id=_sid(), parent_id=step_sid, trace_id=trace_id,
                               graph_id=graph_id, run_id=run_id, subject_id=subject_id,
                               node=node, outcome=outcome, attempt=i, tier=tier,
                               start=1100 + i, end=1100 + i + 1)
        spans[a_sid] = a_entry
    return spans


def _merge(*span_dicts):
    merged = {}
    for d in span_dicts:
        merged.update(d)
    return merged


# --- build_execution_tree ---------------------------------------------------

def test_execution_tree_groups_run_step_attempt_and_orders_by_start_time():
    spans = _run_tree("fixture-a", "run-1", node="draft", outcomes=("failed", "passed"), tiers=("fast", "deep"))
    runs = build_execution_tree(spans)
    assert len(runs) == 1
    run = runs[0]
    assert run["graph_id"] == "fixture-a" and run["run_id"] == "run-1"
    assert run["subject_type"] == "fixture" and run["subject_id"] == "urn:test:1"
    assert len(run["steps"]) == 1
    step = run["steps"][0]
    assert step["node"] == "draft" and step["outcome"] == "passed"
    assert [a["number"] for a in step["attempts"]] == [1, 2]
    assert [a["outcome"] for a in step["attempts"]] == ["failed", "passed"]


def test_execution_tree_retains_subject_grouping_without_tier_or_gen_ai():
    # Run semconv 0.3.0, no cord.tier anywhere: declared outcomes must remain visible.
    spans = _run_tree("fixture-a", "run-1", outcomes=("passed",), tiers=(None,))
    run = build_execution_tree(spans)[0]
    attempt = run["steps"][0]["attempts"][0]
    assert attempt["tier"] is None
    assert attempt["outcome"] == "passed"


def test_execution_tree_orders_multiple_runs_by_start_time():
    early = _run_tree("fixture-a", "run-early")
    later = dict((k, (dict(v[0], startTimeUnixNano=v[0]["startTimeUnixNano"] + 100_000,
                          endTimeUnixNano=v[0]["endTimeUnixNano"] + 100_000), v[1]))
                 for k, v in _run_tree("fixture-a", "run-later").items())
    runs = build_execution_tree(_merge(early, later))
    assert [r["run_id"] for r in runs] == ["run-early", "run-later"]


def test_execution_tree_empty_spans_is_empty():
    assert build_execution_tree({}) == []


def test_execution_tree_reuses_archive_contract_validation():
    spans = _run_tree("fixture-a", "run-1")
    # Corrupt the Step's Node identity the same way test_archive_query.py does.
    step_span = next(s for s, _ in spans.values() if s["name"].startswith("step:"))
    step_span["attributes"]["cord.node.name"] = "other"
    with pytest.raises(ArchiveError, match="Node identity mismatch"):
        build_execution_tree(spans)


# --- repeated-execution evidence (#15) ---------------------------------------

def _resumed_step_tree(graph_id, run_id, run_sid, trace_id, *, node, resumed_from, start):
    """One additional Step, linked to `run_sid`, claiming to resume `resumed_from`."""
    step_sid, step_entry = _span("step", span_id=_sid(), parent_id=run_sid, trace_id=trace_id,
                                  graph_id=graph_id, run_id=run_id, node=node, outcome="passed",
                                  start=start, end=start + 1, resumed_from=resumed_from)
    return {step_sid: step_entry}


def test_normal_single_resume_is_not_flagged_repeated():
    spans = _run_tree("fixture-a", "run-1", node="approve")
    run_sid = next(sid for sid, (s, _) in spans.items() if s["name"] == "run")
    trace_id = "a" * 32
    resumed = _resumed_step_tree("fixture-a", "run-1", run_sid, trace_id,
                                  node="approve", resumed_from="orig-span", start=2000)
    run = build_execution_tree(_merge(spans, resumed))[0]
    assert [(s["node"], s["resumed_from"], s["repeated_execution"]) for s in run["steps"]] == [
        ("approve", None, False),
        ("approve", "orig-span", False),
    ]


def test_two_steps_resuming_the_same_span_flag_the_later_one_as_repeated():
    spans = _run_tree("fixture-a", "run-1", node="approve")
    run_sid = next(sid for sid, (s, _) in spans.items() if s["name"] == "run")
    trace_id = "a" * 32
    first_resume = _resumed_step_tree("fixture-a", "run-1", run_sid, trace_id,
                                       node="approve", resumed_from="orig-span", start=2000)
    duplicate_resume = _resumed_step_tree("fixture-a", "run-1", run_sid, trace_id,
                                           node="approve", resumed_from="orig-span", start=3000)
    run = build_execution_tree(_merge(spans, first_resume, duplicate_resume))[0]
    flags = {(s["start_ns"]): s["repeated_execution"] for s in run["steps"]}
    assert flags == {1100: False, 2000: False, 3000: True}


def test_execution_tree_empty_run_steps_carry_resumed_from_none_by_default():
    run = build_execution_tree(_run_tree("fixture-a", "run-1"))[0]
    assert run["steps"][0]["resumed_from"] is None
    assert run["steps"][0]["repeated_execution"] is False


# --- correlate_topology ------------------------------------------------------

def test_correlate_topology_not_checked_when_no_freshness_given():
    result = correlate_topology(None)
    assert result == {"status": NOT_CHECKED, "node_ids": (), "warnings": (), "correlated": False}


def test_correlate_topology_absent(tmp_path):
    with publisher({"status": 404}) as endpoint:
        check = check_freshness(tmp_path, endpoint)
    result = correlate_topology(check)
    assert result["status"] == "absent"
    assert result["correlated"] is False
    assert result["node_ids"] == ()


def test_correlate_topology_current_single_graph_exposes_nodes(tmp_path):
    doc = _document(["main"], nodes=("draft", "verify"))
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        check = check_freshness(tmp_path, endpoint)
    result = correlate_topology(check)
    assert result["status"] == CURRENT
    assert result["correlated"] is True
    assert result["node_ids"] == ("draft", "verify")


def test_correlate_topology_multi_graph_document_withholds_nodes(tmp_path):
    doc = _document(["a", "b"])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        check = check_freshness(tmp_path, endpoint)
    result = correlate_topology(check)
    assert result["status"] == CURRENT
    assert result["correlated"] is False
    assert result["node_ids"] == ()
    assert any("more than one graph" in w for w in result["warnings"])


def test_correlate_topology_drifted_is_stale_and_not_correlated(tmp_path):
    response = {"status": 200, "body": json.dumps(_document(["main"], nodes=("draft",))).encode()}
    with publisher(response) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        response["body"] = json.dumps(_document(["main"], nodes=("draft", "verify"))).encode()
        check = check_freshness(tmp_path, endpoint)
    assert check.status == "changed"
    result = correlate_topology(check)
    assert result["status"] == STALE
    assert result["correlated"] is False
    assert result["node_ids"] == ()
    assert "not used to correlate" in result["warnings"][0]


def test_correlate_topology_surfaces_gaps_producer_limitations_and_fanout(tmp_path):
    gap = {"code": "orphaned-nodes", "message": "target is undeclared",
           "element": {"graphId": "main", "kind": "node", "id": "verify"}}
    limitation = {"code": "dynamic-interrupts", "message": "cannot observe runtime interrupts"}
    doc = _document(["main"], nodes=("draft", "verify"), gaps=[gap], producer_limitations=[limitation])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        check = check_freshness(tmp_path, endpoint)
    warnings = correlate_topology(check)["warnings"]
    assert any("orphaned-nodes" in w for w in warnings)
    assert any("dynamic-interrupts" in w for w in warnings)


# --- build_catalog ------------------------------------------------------------

def _connection(alias, endpoint, graph_ids, *, reachable=True):
    return alias, {"endpoint": endpoint, "reachable": reachable, "graphs": list(graph_ids)}


def test_two_unrelated_graphs_render_before_any_run_exists():
    connections = dict([_connection("a", "http://a.example", ["fixture-a"]),
                        _connection("b", "http://b.example", ["fixture-b"])])
    catalog = build_catalog(connections, {}, {})
    assert [g["graph_id"] for g in catalog["graphs"]] == ["fixture-a", "fixture-b"]
    assert all(g["subjects"] == [] for g in catalog["graphs"])


def test_graph_without_manifest_shows_recorded_execution_and_no_topology(tmp_path):
    with publisher({"status": 404}) as endpoint:
        freshness = check_freshness(tmp_path, endpoint)
    connections = dict([_connection("a", endpoint, ["fixture-a"])])
    spans = _run_tree("fixture-a", "run-1")
    catalog = build_catalog(connections, {endpoint: freshness}, spans)
    graph = catalog["graphs"][0]
    assert graph["topology_status"] == "absent"
    assert graph["topology_nodes"] == ()
    assert len(graph["subjects"][0]["runs"]) == 1


def test_drifted_manifest_never_looks_current_in_catalog(tmp_path):
    response = {"status": 200, "body": json.dumps(_document(["main"], nodes=("draft",))).encode()}
    with publisher(response) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        response["body"] = json.dumps(_document(["main"], nodes=("draft", "verify"))).encode()
        freshness = check_freshness(tmp_path, endpoint)
    connections = dict([_connection("a", endpoint, ["fixture-a"])])
    spans = _run_tree("fixture-a", "run-1")
    catalog = build_catalog(connections, {endpoint: freshness}, spans)
    graph = catalog["graphs"][0]
    assert graph["topology_status"] == STALE
    assert graph["topology_nodes"] == ()
    # Recorded execution is still shown -- only correlation is withheld.
    assert len(graph["subjects"][0]["runs"]) == 1


def test_unreachable_deployment_remains_visible():
    connections = dict([_connection("a", "http://a.example", ["fixture-a"], reachable=False)])
    catalog = build_catalog(connections, {}, {})
    graph = catalog["graphs"][0]
    assert graph["reachable"] is False
    assert graph["graph_id"] == "fixture-a"


def test_unmatched_recorded_node_is_flagged_not_dropped(tmp_path):
    doc = _document(["main"], nodes=("draft",))
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        freshness = check_freshness(tmp_path, endpoint)
    connections = dict([_connection("a", endpoint, ["fixture-a"])])
    spans = _run_tree("fixture-a", "run-1", node="undeclared-node")
    catalog = build_catalog(connections, {endpoint: freshness}, spans)
    graph = catalog["graphs"][0]
    assert graph["unmatched_node_names"] == ("undeclared-node",)
    assert len(graph["subjects"][0]["runs"]) == 1  # evidence preserved, not dropped


def test_connected_graph_without_a_topology_probe_is_not_checked_not_no_connection():
    # Distinguish "connection exists but this call skipped probing its topology"
    # from "no connection was ever registered for this Graph" (build_catalog
    # never conflates the two, even though both start from freshness=None).
    connections = dict([_connection("a", "http://a.example", ["fixture-a"])])
    catalog = build_catalog(connections, {}, {})
    assert catalog["graphs"][0]["topology_status"] == NOT_CHECKED


def test_recorded_execution_without_any_connection_remains_visible():
    spans = _run_tree("orphan-graph", "run-1")
    catalog = build_catalog({}, {}, spans)
    graph = catalog["graphs"][0]
    assert graph["graph_id"] == "orphan-graph"
    assert graph["deployment_alias"] is None
    assert graph["reachable"] is None
    assert graph["topology_status"] == NO_CONNECTION
    assert len(graph["subjects"][0]["runs"]) == 1


def test_subject_grouping_across_multiple_runs():
    spans = _merge(
        _run_tree("fixture-a", "run-1", subject_id="urn:test:1"),
        _run_tree("fixture-a", "run-2", subject_id="urn:test:1"),
        _run_tree("fixture-a", "run-3", subject_id="urn:test:2"),
    )
    connections = dict([_connection("a", "http://a.example", ["fixture-a"])])
    catalog = build_catalog(connections, {}, spans)
    subjects = {s["subject_id"]: s for s in catalog["graphs"][0]["subjects"]}
    assert len(subjects["urn:test:1"]["runs"]) == 2
    assert len(subjects["urn:test:2"]["runs"]) == 1


def test_empty_catalog_renders_without_error():
    text = format_catalog_text(build_catalog({}, {}, {}))
    assert "no Graphs" in text


def test_format_catalog_text_includes_key_facts():
    connections = dict([_connection("a", "http://a.example", ["fixture-a"])])
    spans = _run_tree("fixture-a", "run-1", outcomes=("failed", "passed"), tiers=("fast", "deep"))
    text = format_catalog_text(build_catalog(connections, {}, spans))
    assert "Graph fixture-a" in text
    assert "Subject fixture:urn:test:1" in text
    assert "Run run-1" in text
    assert "Step draft -> passed" in text
    assert "Attempt 1, tier=fast -> failed" in text


# --- live runs (#20) --------------------------------------------------------

def _live_view(run_id, graph_id, *, source=LIVE, status="running", seconds=None,
               thread_id="t-1", subject="subject-1"):
    return {"run_id": run_id, "thread_id": thread_id, "graph_id": graph_id,
            "assistant_id": graph_id, "subject": subject,
            "aegra_status": status, "source": source, "seconds_since_completion": seconds}


def test_live_run_appears_under_its_resolved_graph_when_not_yet_recorded():
    live_runs = {"r-1": _live_view("r-1", "orphan-graph")}
    catalog = build_catalog({}, {}, {}, live_runs=live_runs)
    graph = catalog["graphs"][0]
    assert graph["graph_id"] == "orphan-graph"
    assert graph["topology_status"] == NO_CONNECTION
    assert graph["subjects"] == []
    assert graph["live_runs"] == [live_runs["r-1"]]


def test_live_run_already_recorded_is_dropped_from_the_catalog():
    """#20 AC4: arrival of the recorded result replaces the live placeholder."""
    spans = _run_tree("fixture-a", "r-1")
    live_runs = {"r-1": _live_view("r-1", "fixture-a")}
    catalog = build_catalog({}, {}, spans, live_runs=live_runs)
    graph = catalog["graphs"][0]
    assert graph["live_runs"] == []
    assert len(graph["subjects"][0]["runs"]) == 1


def test_live_run_alongside_a_different_recorded_run_on_the_same_graph():
    spans = _run_tree("fixture-a", "r-1")
    live_runs = {"r-2": _live_view("r-2", "fixture-a")}
    catalog = build_catalog({}, {}, spans, live_runs=live_runs)
    graph = catalog["graphs"][0]
    assert [v["run_id"] for v in graph["live_runs"]] == ["r-2"]
    assert len(graph["subjects"][0]["runs"]) == 1


def test_live_run_without_a_resolvable_graph_id_is_omitted():
    live_runs = {"r-1": {**_live_view("r-1", None)}}
    catalog = build_catalog({}, {}, {}, live_runs=live_runs)
    assert catalog["graphs"] == []


def test_live_runs_default_to_empty_when_not_given():
    catalog = build_catalog({}, {}, {})
    assert catalog == {"graphs": []}


def test_format_catalog_text_renders_a_running_live_run():
    catalog = build_catalog({}, {}, {}, live_runs={"r-1": _live_view("r-1", "g1")})
    text = format_catalog_text(catalog)
    assert "Live Run r-1 (thread t-1) [live, running]" in text


def test_format_catalog_text_renders_ingestion_pending():
    view = _live_view("r-1", "g1", source=INGESTION_PENDING, status="success", seconds=12.0)
    text = format_catalog_text(build_catalog({}, {}, {}, live_runs={"r-1": view}))
    assert "ingestion pending 12s" in text


def test_format_catalog_text_renders_ingestion_failed_as_a_bounded_visible_diagnostic():
    """#20 AC5: a bounded ingestion failure is visible, not an endless loading state."""
    view = _live_view("r-1", "g1", source=INGESTION_FAILED, status="success", seconds=312.0)
    text = format_catalog_text(build_catalog({}, {}, {}, live_runs={"r-1": view}))
    assert "ingestion FAILED after 312s" in text


def test_format_catalog_text_a_live_run_suppresses_the_no_recorded_runs_line():
    catalog = build_catalog({}, {}, {}, live_runs={"r-1": _live_view("r-1", "g1")})
    text = format_catalog_text(catalog)
    assert "no recorded Runs" not in text
