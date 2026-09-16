"""Generic catalog and topology/recorded-execution viewer (#13).

Consumes only public contracts already implemented elsewhere: a connected
Deployment's reachable Graphs (`cord list`'s `/assistants` probe), optional
published topology (`cord_runtime.topology`), and recorded execution read
through the shared archive contract (`cord_runtime.archive_query.role`/
`parent`, reused rather than reimplemented). This module never imports graph
business logic and never hard-codes an example Graph or Node name; every
identifier it renders comes from `cord.*` span attributes, a connection
record, or a published topology document.

Graph identity is the Deployment's own reported `graph_id`
(`GET /assistants`), which is the same string a graph stamps as
`cord.graph.id` (ADR-0002/ADR-0003). A topology document's `graphs[].id` is a
different, document-local address (ADR-0014) and is compared to nothing here;
a document is only used to attach Node structure when it is unambiguous --
exactly one graph inside it -- because no explicit per-graph association
storage exists yet. A document with more than one graph is still shown as
present, just not correlated (see `correlate_topology`).
"""

from typing import Any

from cord_runtime.archive_query import ArchiveError, parent, query_spans, role
from cord_runtime.topology import ABSENT, CHANGED, INVALID, UNREACHABLE, FreshnessCheck, graphs, parallel_interrupt_warnings

# Topology status values this module reports for display, distinct from
# cord_runtime.topology's fetch/freshness vocabulary so the viewer can add
# "stale", "no_connection", and "not_checked" without overloading that
# module's contract.
STALE = "stale"
CURRENT = "current"
NO_CONNECTION = "no_connection"  # no registered Deployment connection at all for this Graph
NOT_CHECKED = "not_checked"  # a connection exists but its topology was not probed this call


def build_execution_tree(spans: dict) -> list[dict[str, Any]]:
    """Group validated Run -> Step -> Attempt spans into per-Run records,
    each ordered by start time, using only explicit `cord.*` identity.

    Reuses `query_spans` for its full identity/hierarchy validation (the
    archive contract) rather than re-checking it here, then walks the same
    validated spans a second time with the shared `role`/`parent` helpers to
    assemble the tree `query_spans` itself discards. Declared outcomes stay
    visible even when `cord.tier` and `gen_ai.*` are absent (Run semconv
    0.3.0); Subject grouping survives on `cord.subject.type`/`cord.subject.id`
    alone.

    Each Step record also carries `resumed_from` (the linked span ID, or
    `None`) and `repeated_execution` (#15): a normal resume links exactly one
    new Step to the Step it resumes, so `resumed_from` values are expected to
    be unique. When more than one Step declares the *same* `resumed_from`,
    every one after the earliest-starting is flagged `repeated_execution:
    True` -- evidence that the same paused point was resumed more than once
    (e.g. a duplicate submission), distinct from an ordinary resume chain. A
    new Step span alone is never proof of a duplicate business mutation; this
    is span evidence only; effect probes/receipts/idempotency stay graph-owned.
    """
    if spans:
        query_spans(spans, reference=0, top=1)  # validation only; count is unused

    runs: dict[str, dict] = {}
    steps: dict[str, dict] = {}

    def ensure_run(run_span):
        key = run_span["spanId"]
        if key not in runs:
            attrs = run_span["attributes"]
            runs[key] = {
                "graph_id": attrs["cord.graph.id"],
                "run_id": attrs["cord.run.id"],
                "subject_type": attrs["cord.subject.type"],
                "subject_id": attrs["cord.subject.id"],
                "start_ns": run_span["startTimeUnixNano"],
                "steps": [],
            }
        return runs[key]

    def ensure_step(step_span, run_span):
        key = step_span["spanId"]
        if key not in steps:
            attrs = step_span["attributes"]
            record = {
                "node": attrs["cord.node.name"],
                "outcome": attrs["cord.outcome"],
                "start_ns": step_span["startTimeUnixNano"],
                "resumed_from": attrs.get("cord.resumed_from"),
                "repeated_execution": False,
                "attempts": [],
            }
            steps[key] = record
            ensure_run(run_span)["steps"].append(record)
        return steps[key]

    for span, location in spans.values():
        try:
            kind = role(span)
            if kind == "run":
                ensure_run(span)
            elif kind == "step":
                ensure_step(span, parent(spans, span, "run"))
            elif kind == "attempt":
                step_span = parent(spans, span, "step")
                step = ensure_step(step_span, parent(spans, step_span, "run"))
                attrs = span["attributes"]
                step["attempts"].append({
                    "number": attrs["cord.step.attempt"],
                    "tier": attrs.get("cord.tier"),
                    "outcome": attrs["cord.outcome"],
                    "start_ns": span["startTimeUnixNano"],
                })
        except ArchiveError as exc:
            raise ArchiveError(f"{location}: {exc}") from None

    for record in runs.values():
        record["steps"].sort(key=lambda s: s["start_ns"])
        for step in record["steps"]:
            step["attempts"].sort(key=lambda a: a["start_ns"])

    by_resumed_from: dict[str, list[dict]] = {}
    for step in steps.values():
        if step["resumed_from"] is not None:
            by_resumed_from.setdefault(step["resumed_from"], []).append(step)
    for group in by_resumed_from.values():
        if len(group) > 1:
            group.sort(key=lambda s: s["start_ns"])
            for step in group[1:]:
                step["repeated_execution"] = True

    return sorted(runs.values(), key=lambda r: r["start_ns"])


def _document_warnings(document: dict) -> list[str]:
    """Fan-out interrupt (R3), per-graph gaps, and general producer
    limitations, all as plain warning strings the viewer never treats as
    blocking (ADR-0007/0015)."""
    warnings = []
    for warning in parallel_interrupt_warnings(document):
        confirmed = "confirmed" if warning.confirmed else "unconfirmed (branch semantics not verifiable)"
        targets = ", ".join(warning.interrupted_target_ids)
        warnings.append(
            f"fan-out interrupt [{confirmed}] in graph '{warning.graph_id}': "
            f"'{warning.source_node_id}' fans out into interrupted node(s) {targets}"
        )
    for gap in document.get("completeness", {}).get("gaps", []):
        element = gap["element"]
        warnings.append(f"gap [{gap['code']}] {element['kind']} '{element['id']}': {gap['message']}")
    for limitation in document.get("producerLimitations", []):
        warnings.append(f"producer limitation [{limitation['code']}]: {limitation['message']}")
    return warnings


def correlate_topology(freshness: FreshnessCheck | None) -> dict[str, Any]:
    """Map one Deployment endpoint's `FreshnessCheck` to viewer display facts.

    A drifted (`CHANGED`) document is shown as stale and is never used to
    correlate recorded execution (ADR-0011/0015): its Node ids are withheld
    entirely rather than risking a stale-but-labeled-current picture. Absent,
    unreachable, and invalid documents carry no Node structure either.
    `freshness is None` means a connection exists but this call did not probe
    its topology (`NOT_CHECKED`); `build_catalog` uses `NO_CONNECTION`
    instead when there is no connection at all for the Graph.
    """
    if freshness is None:
        return {"status": NOT_CHECKED, "node_ids": (), "warnings": (), "correlated": False}
    if freshness.status in (ABSENT, UNREACHABLE, INVALID):
        return {"status": freshness.status, "node_ids": (), "warnings": (), "correlated": False}
    if freshness.status == CHANGED:
        return {
            "status": STALE, "node_ids": (), "correlated": False,
            "warnings": ("published topology changed since the last explicit sync; "
                         "not used to correlate recorded execution until refreshed",),
        }
    # NO_SNAPSHOT or UNCHANGED: a confirmed-current, schema-valid document.
    document = freshness.reading.document
    document_graphs = graphs(document)
    warnings = _document_warnings(document)
    if len(document_graphs) != 1:
        warnings.append(
            "document publishes more than one graph; no explicit Deployment/Graph "
            "association exists yet to pick the right one, so Node structure is withheld"
        )
        return {"status": CURRENT, "node_ids": (), "warnings": tuple(warnings), "correlated": False}
    node_ids = tuple(sorted(node["id"] for node in document_graphs[0]["structure"]["nodes"]))
    return {"status": CURRENT, "node_ids": node_ids, "warnings": tuple(warnings), "correlated": True}


def build_catalog(connections: dict[str, dict], topology: dict[str, FreshnessCheck], spans: dict) -> dict[str, Any]:
    """The generic catalog: every connected or recorded Graph, its optional
    topology, and its recorded execution, joined only by explicit identity.

    `connections`: `{alias: {"endpoint": str, "reachable": bool, "graphs": [graph_id, ...]}}`,
        exactly `cord list`'s existing `/assistants` probe shape.
    `topology`: `{endpoint: FreshnessCheck}`, one check per connected endpoint.
    `spans`: the archive contract's `read_spans(...)` result (may be `{}`).

    A Graph with a registered connection but zero Runs still renders (its
    `runs` list is simply empty); a Graph with recorded execution but no
    registered connection still renders, with `topology_status` `"no_connection"`,
    preserving unmatched execution evidence as a diagnostic rather than
    dropping it (#13).
    """
    runs = build_execution_tree(spans)
    by_graph: dict[str, list[dict]] = {}
    for run in runs:
        by_graph.setdefault(run["graph_id"], []).append(run)

    connection_by_graph: dict[str, dict] = {}
    for alias, info in connections.items():
        for graph_id in info.get("graphs", ()):
            connection_by_graph[graph_id] = {
                "alias": alias, "endpoint": info["endpoint"], "reachable": info["reachable"],
            }

    graph_views = []
    for graph_id in sorted(set(connection_by_graph) | set(by_graph)):
        connection = connection_by_graph.get(graph_id)
        if connection is None:
            correlation = {"status": NO_CONNECTION, "node_ids": (), "warnings": (), "correlated": False}
        else:
            correlation = correlate_topology(topology.get(connection["endpoint"]))

        graph_runs = by_graph.get(graph_id, [])
        unmatched_node_names = ()
        if correlation["correlated"]:
            recorded_nodes = {step["node"] for run in graph_runs for step in run["steps"]}
            unmatched_node_names = tuple(sorted(recorded_nodes - set(correlation["node_ids"])))

        subjects: dict[tuple[str, str], list[dict]] = {}
        for run in graph_runs:
            key = (run["subject_type"], run["subject_id"])
            subjects.setdefault(key, []).append({
                "run_id": run["run_id"],
                "steps": [{"node": s["node"], "outcome": s["outcome"],
                           "resumed_from": s["resumed_from"], "repeated_execution": s["repeated_execution"],
                           "attempts": [
                    {"number": a["number"], "tier": a["tier"], "outcome": a["outcome"]} for a in s["attempts"]
                ]} for s in run["steps"]],
            })

        graph_views.append({
            "graph_id": graph_id,
            "deployment_alias": connection["alias"] if connection else None,
            "reachable": connection["reachable"] if connection else None,
            "topology_status": correlation["status"],
            "topology_nodes": correlation["node_ids"],
            "topology_warnings": correlation["warnings"],
            "unmatched_node_names": unmatched_node_names,
            "subjects": [
                {"subject_type": subject_type, "subject_id": subject_id, "runs": subject_runs}
                for (subject_type, subject_id), subject_runs in sorted(subjects.items())
            ],
        })
    return {"graphs": graph_views}


def format_catalog_text(catalog: dict[str, Any]) -> str:
    """A readable, deterministic text rendering (`cord view`'s default output)."""
    if not catalog["graphs"]:
        return "no Graphs: no connections registered and no recorded execution given"
    lines = []
    for graph in catalog["graphs"]:
        deployment = f"{graph['deployment_alias']}" if graph["deployment_alias"] else "no connection"
        reachability = {True: "reachable", False: "unreachable", None: ""}[graph["reachable"]]
        header = f"Graph {graph['graph_id']}  ({deployment}"
        header += f", {reachability}" if reachability else ""
        header += f", topology: {graph['topology_status']})"
        lines.append(header)
        if graph["topology_nodes"]:
            lines.append(f"  nodes: {', '.join(graph['topology_nodes'])}")
        for warning in graph["topology_warnings"]:
            lines.append(f"  warning: {warning}")
        for name in graph["unmatched_node_names"]:
            lines.append(f"  warning: recorded Node '{name}' is not in the correlated topology")
        if not graph["subjects"]:
            lines.append("  no recorded Runs")
        for subject in graph["subjects"]:
            lines.append(f"  Subject {subject['subject_type']}:{subject['subject_id']}")
            for run in subject["runs"]:
                lines.append(f"    Run {run['run_id']}")
                for step in run["steps"]:
                    lines.append(f"      Step {step['node']} -> {step['outcome']}")
                    if step["repeated_execution"]:
                        lines.append(
                            f"        warning: repeated execution -- another Step already resumed "
                            f"from {step['resumed_from']}"
                        )
                    for attempt in step["attempts"]:
                        tier = f", tier={attempt['tier']}" if attempt["tier"] else ""
                        lines.append(f"        Attempt {attempt['number']}{tier} -> {attempt['outcome']}")
    return "\n".join(lines)
