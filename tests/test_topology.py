"""Optional published topology discovery: absent/unreachable/invalid/valid
classification, R3 fan-out interrupt warnings, and snapshot drift (#11).

No Docker or live model here; the well-known document is served by a tiny
local HTTP fixture per the issue's verification plan. See tests/test_aegra.py
for the reused model-free execution proof that discovery never gates a run.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

from agent_topology.spec import finalize_document
import pytest

from cord_runtime.connections import InvalidConnection
from cord_runtime.topology import (
    ABSENT,
    CHANGED,
    INVALID,
    NO_SNAPSHOT,
    UNCHANGED,
    UNREACHABLE,
    VALID,
    WELL_KNOWN_PATH,
    check_freshness,
    fetch_topology,
    get_snapshot,
    graph_by_id,
    graphs,
    parallel_interrupt_warnings,
    refresh_snapshot,
    snapshots_path,
)


# --- local test HTTP fixture ------------------------------------------------

@contextmanager
def publisher(response):
    """Serve `response` (a mutable dict a test may edit between requests) at
    the well-known topology path; any other path answers 404."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if response.get("delay"):
                time.sleep(response["delay"])
            if self.path != WELL_KNOWN_PATH:
                self.send_error(404)
                return
            status = response.get("status", 200)
            body = response.get("body", b"")
            self.send_response(status)
            self.send_header("Content-Type", response.get("content_type", "application/json"))
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


def unused_endpoint():
    """A port nothing is listening on, for a deterministic connection refusal."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --- synthetic public-format document fixtures ------------------------------

def _node(node_id, interrupts=None):
    node = {"id": node_id}
    if interrupts:
        node["interrupts"] = interrupts
    return node


def _edge(edge_id, source, target, kind="direct"):
    return {"id": edge_id, "source": source, "target": target, "kind": kind}


def _fanout_graph(graph_id="main", *, interrupted=True, edge_kind="direct", interpretation=None):
    """start -> draft -> {review_a, review_b}; review_b interrupts before."""
    structure = {
        "nodes": [_node("start"), _node("draft"), _node("review_a"),
                 _node("review_b", ["before"] if interrupted else None)],
        "edges": [
            _edge("e1", "start", "draft"),
            _edge("e2", "draft", "review_a", edge_kind),
            _edge("e3", "draft", "review_b", edge_kind),
        ],
        "joins": [],
        "entryNodeIds": ["start"],
        "exitNodeIds": ["review_a", "review_b"],
    }
    graph = {"id": graph_id, "structure": structure}
    if interpretation is not None:
        graph["x-topology-interpretation"] = interpretation
    return graph


def _sequential_graph(graph_id="sequential"):
    """A single direct edge into an interrupted node: no fan-out at all."""
    structure = {
        "nodes": [_node("start"), _node("gate", ["before"])],
        "edges": [_edge("e1", "start", "gate")],
        "joins": [],
        "entryNodeIds": ["start"],
        "exitNodeIds": ["gate"],
    }
    return {"id": graph_id, "structure": structure}


def document(graph_specs, *, gaps=None, producer_limitations=None):
    return finalize_document({
        "topologyVersion": "0.1",
        "provenance": {
            "generatedAt": "2026-09-15T00:00:00Z",
            "producer": {"name": "test-fixture", "version": "0.0.0"},
            "framework": {"name": "test", "version": "0.0.0"},
        },
        "producerLimitations": producer_limitations or [],
        "graphs": graph_specs,
        "completeness": {"status": "incomplete" if gaps else "complete", "gaps": gaps or []},
    })


CONFIRMED_INTERPRETATION = {
    "version": "1",
    "nodes": [{"nodeId": "draft", "branch": {"status": "known", "value": "all-declared"}}],
}
UNKNOWN_BRANCH_INTERPRETATION = {
    "version": "1",
    "nodes": [{"nodeId": "draft", "branch": {"status": "unknown"}}],
}
UNRECOGNIZED_REVISION_INTERPRETATION = {
    "version": "2",
    "nodes": [{"nodeId": "draft", "branch": {"status": "known", "value": "all-declared"}}],
}


# --- fetch classification ---------------------------------------------------

def test_absent_is_not_unreachable():
    with publisher({"status": 404}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.status == ABSENT
    assert reading.document is None


def test_unreachable_on_connection_refused():
    reading = fetch_topology(unused_endpoint(), timeout=1.0)
    assert reading.status == UNREACHABLE
    assert reading.reason == "connection_error"


def test_unreachable_on_server_error_status():
    with publisher({"status": 503, "body": b""}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.status == UNREACHABLE
    assert reading.reason == "http_503"


def test_unreachable_on_timeout():
    with publisher({"status": 200, "body": b"{}", "delay": 0.3}) as endpoint:
        reading = fetch_topology(endpoint, timeout=0.05)
    assert reading.status == UNREACHABLE
    assert reading.reason == "timeout"


def test_invalid_on_non_json_body():
    with publisher({"status": 200, "body": b"not json"}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.status == INVALID
    assert reading.errors


def test_invalid_on_schema_violation():
    with publisher({"status": 200, "body": json.dumps({"topologyVersion": "0.1"}).encode()}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.status == INVALID
    assert any("required" in e for e in reading.errors)


def test_fetch_rejects_invalid_endpoint():
    with pytest.raises(InvalidConnection):
        fetch_topology("not-a-url")


# --- valid documents: preserved, not renamed --------------------------------

def test_valid_single_graph_document():
    doc = document([_fanout_graph()])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.status == VALID
    assert [g["id"] for g in graphs(reading.document)] == ["main"]
    node_ids = {n["id"] for n in graph_by_id(reading.document, "main")["structure"]["nodes"]}
    assert node_ids == {"start", "draft", "review_a", "review_b"}
    assert graph_by_id(reading.document, "missing") is None


def test_valid_multi_graph_document_preserves_both_addresses():
    doc = document([_fanout_graph("alpha"), _sequential_graph("beta")])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.status == VALID
    assert [g["id"] for g in graphs(reading.document)] == ["alpha", "beta"]


def test_gaps_and_producer_limitations_pass_through_unchanged():
    gap = {"code": "orphaned-nodes", "message": "router target is undeclared",
           "element": {"graphId": "main", "kind": "node", "id": "draft"}}
    limitation = {"code": "dynamic-interrupts",
                 "message": "Interrupts raised inside node bodies cannot be observed statically."}
    doc = document([_fanout_graph()], gaps=[gap], producer_limitations=[limitation])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        reading = fetch_topology(endpoint)
    assert reading.document["completeness"]["gaps"] == [gap]
    assert reading.document["producerLimitations"] == [limitation]


# --- R3: static interrupt inside a direct fan-out ---------------------------

def test_parallel_interrupt_confirmed_with_recognized_interpretation():
    doc = document([_fanout_graph(interpretation=CONFIRMED_INTERPRETATION)])
    warnings = parallel_interrupt_warnings(doc)
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.graph_id == "main"
    assert warning.source_node_id == "draft"
    assert warning.fanout_target_ids == ("review_a", "review_b")
    assert warning.interrupted_target_ids == ("review_b",)
    assert warning.confirmed is True


def test_parallel_interrupt_unconfirmed_without_interpretation_extension():
    doc = document([_fanout_graph(interpretation=None)])
    warnings = parallel_interrupt_warnings(doc)
    assert len(warnings) == 1
    assert warnings[0].confirmed is False


def test_parallel_interrupt_unconfirmed_when_branch_status_unknown():
    doc = document([_fanout_graph(interpretation=UNKNOWN_BRANCH_INTERPRETATION)])
    warnings = parallel_interrupt_warnings(doc)
    assert len(warnings) == 1
    assert warnings[0].confirmed is False


def test_parallel_interrupt_unconfirmed_on_unrecognized_revision():
    doc = document([_fanout_graph(interpretation=UNRECOGNIZED_REVISION_INTERPRETATION)])
    warnings = parallel_interrupt_warnings(doc)
    assert len(warnings) == 1
    assert warnings[0].confirmed is False


def test_no_warning_for_conditional_fanout():
    doc = document([_fanout_graph(edge_kind="conditional", interpretation=CONFIRMED_INTERPRETATION)])
    assert parallel_interrupt_warnings(doc) == []


def test_no_warning_without_fanout():
    doc = document([_sequential_graph()])
    assert parallel_interrupt_warnings(doc) == []


def test_no_warning_without_any_interrupt():
    doc = document([_fanout_graph(interrupted=False, interpretation=CONFIRMED_INTERPRETATION)])
    assert parallel_interrupt_warnings(doc) == []


# --- snapshot storage and freshness -----------------------------------------

def test_refresh_snapshot_only_stores_a_valid_document(tmp_path):
    with publisher({"status": 404}) as endpoint:
        reading = refresh_snapshot(tmp_path, endpoint)
    assert reading.status == ABSENT
    assert get_snapshot(tmp_path, endpoint) is None
    assert not snapshots_path(tmp_path).exists()


def test_check_freshness_before_any_refresh_is_no_snapshot(tmp_path):
    doc = document([_fanout_graph()])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        check = check_freshness(tmp_path, endpoint)
    assert check.status == NO_SNAPSHOT


def test_check_freshness_unchanged_after_refresh(tmp_path):
    doc = document([_fanout_graph()])
    with publisher({"status": 200, "body": json.dumps(doc).encode()}) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        check = check_freshness(tmp_path, endpoint)
    assert check.status == UNCHANGED


def test_check_freshness_changed_does_not_overwrite_snapshot_until_refreshed(tmp_path):
    response = {"status": 200, "body": json.dumps(document([_fanout_graph()])).encode()}
    with publisher(response) as endpoint:
        first = refresh_snapshot(tmp_path, endpoint)
        stored_before = get_snapshot(tmp_path, endpoint)

        response["body"] = json.dumps(document([_sequential_graph()])).encode()
        check = check_freshness(tmp_path, endpoint)
        assert check.status == CHANGED
        # An unrefreshed drift check never mutates the stored snapshot.
        assert get_snapshot(tmp_path, endpoint) == stored_before
        assert stored_before["structureHash"] == first.document["structureHash"]
        assert stored_before["structureHash"] != check.reading.document["structureHash"]

        second = refresh_snapshot(tmp_path, endpoint)
        assert get_snapshot(tmp_path, endpoint)["structureHash"] == second.document["structureHash"]
        assert check_freshness(tmp_path, endpoint).status == UNCHANGED


def test_check_freshness_unreachable_leaves_snapshot_untouched(tmp_path):
    response = {"status": 200, "body": json.dumps(document([_fanout_graph()])).encode()}
    with publisher(response) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        stored = get_snapshot(tmp_path, endpoint)
        response["status"] = 503
        check = check_freshness(tmp_path, endpoint)
    assert check.status == UNREACHABLE
    assert get_snapshot(tmp_path, endpoint) == stored


def test_check_freshness_invalid_leaves_snapshot_untouched(tmp_path):
    response = {"status": 200, "body": json.dumps(document([_fanout_graph()])).encode()}
    with publisher(response) as endpoint:
        refresh_snapshot(tmp_path, endpoint)
        stored = get_snapshot(tmp_path, endpoint)
        response["body"] = b"not json"
        check = check_freshness(tmp_path, endpoint)
    assert check.status == INVALID
    assert get_snapshot(tmp_path, endpoint) == stored
