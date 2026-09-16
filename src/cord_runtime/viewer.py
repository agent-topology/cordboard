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
different, document-local address (ADR-0014) and is never compared to it by
name; a single-graph document is unambiguous on its own, and a multi-graph
document is only correlated through a connection's explicit `graph_map`
(#43, `cord_runtime.connections.set_graph_map`) -- never guessed. A document
with more than one graph and no matching `graph_map` entry is still shown as
present, just not correlated (see `correlate_topology`).

Catalog entries are scoped per registered connection (alias/endpoint, #43),
not merely per `graph_id`: two connections advertising the same `graph_id`
each render independently, with their own reachability and topology. Recorded
or live execution for a `graph_id` shared by more than one connection, with
no other identity to disambiguate it, is placed under a distinct `ambiguous`
entry instead of being attached to either connection -- never guessed.
"""

from typing import Any

from cord_runtime.archive_query import ArchiveError, parent, query_spans, role
from cord_runtime.live_reconciliation import INGESTION_FAILED, INGESTION_PENDING, LIVE
from cord_runtime.topology import ABSENT, CHANGED, INVALID, UNREACHABLE, FreshnessCheck, graphs, parallel_interrupt_warnings

# Topology status values this module reports for display, distinct from
# cord_runtime.topology's fetch/freshness vocabulary so the viewer can add
# "stale", "no_connection", and "not_checked" without overloading that
# module's contract.
STALE = "stale"
CURRENT = "current"
NO_CONNECTION = "no_connection"  # no registered Deployment connection at all for this Graph
NOT_CHECKED = "not_checked"  # a connection exists but its topology was not probed this call
AMBIGUOUS = "ambiguous"  # graph_id shared by >1 connection; recorded/live execution cannot be attributed (#43)


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

    Timeline fields (#46 AC5) separate approval wait from execution time
    rather than trusting the Run span's own `endTimeUnixNano` alone: per
    ADR-0008, `cord_runtime.execution.run`'s span closes the instant the
    graph first pauses, and `resume_run` never reopens it -- only new Step
    spans attach to the same trace on a resume. So each Run's `end_ns` here
    is the max of its own span end and every Step/Attempt end it contains,
    the truthful outer boundary of everything actually recorded. A Step with
    `resumed_from` set carries `approval_wait_ns` (the gap between the
    original awaiting_approval Step's end and this Step's start) when that
    original Step is present in `spans`, or `None` -- never synthesized --
    when it was pruned or never delivered; the Run then carries
    `timeline_incomplete: True` rather than a silently wrong total. A Step
    still `awaiting_approval` with no later Step resuming it is flagged
    `awaiting_resume: True` (evidence of an open wait, not a gap). Each
    Run's `execution_ns` subtracts only the *known* approval waits from its
    total span.
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
                "end_ns": run_span["endTimeUnixNano"],
                "steps": [],
            }
        return runs[key]

    def ensure_step(step_span, run_span):
        key = step_span["spanId"]
        if key not in steps:
            attrs = step_span["attributes"]
            record = {
                "span_id": key,
                "node": attrs["cord.node.name"],
                "outcome": attrs["cord.outcome"],
                "start_ns": step_span["startTimeUnixNano"],
                "end_ns": step_span["endTimeUnixNano"],
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
                    "end_ns": span["endTimeUnixNano"],
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

    # Timeline truthfulness (#46 AC5): approval wait vs. execution time, and
    # incomplete/unmatched evidence surfaced rather than guessed.
    for step in steps.values():
        if step["resumed_from"] is not None:
            original = steps.get(step["resumed_from"])
            step["approval_wait_ns"] = (max(0, step["start_ns"] - original["end_ns"])
                                        if original is not None else None)
        else:
            step["approval_wait_ns"] = None
        step["awaiting_resume"] = (step["outcome"] == "awaiting_approval"
                                   and step["span_id"] not in by_resumed_from)

    for record in runs.values():
        end_candidates = [record["end_ns"]] + [s["end_ns"] for s in record["steps"]] \
            + [a["end_ns"] for s in record["steps"] for a in s["attempts"]]
        record["end_ns"] = max(end_candidates)
        known_waits = [s["approval_wait_ns"] for s in record["steps"] if s["approval_wait_ns"] is not None]
        record["approval_wait_ns"] = sum(known_waits)
        record["execution_ns"] = max(0, record["end_ns"] - record["start_ns"] - record["approval_wait_ns"])
        record["timeline_incomplete"] = any(
            (s["resumed_from"] is not None and s["approval_wait_ns"] is None) or s["awaiting_resume"]
            for s in record["steps"]
        )

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


def _resolve_multi_graph(document_graphs: list[dict], graph_id: str | None,
                          graph_map: dict[str, str] | None) -> dict | None:
    """Find the one document-local graph whose explicit `graph_map` entry
    names `graph_id`, or `None` if there is no such unambiguous match (#43).

    `graph_map` is `{document_graph_id: cord_graph_id}` (`connections.py`'s
    `set_graph_map`), never inferred by matching names or positions.
    """
    if not graph_id or not graph_map:
        return None
    by_id = {g["id"]: g for g in document_graphs}
    matches = [doc_id for doc_id, mapped in graph_map.items() if mapped == graph_id and doc_id in by_id]
    return by_id[matches[0]] if len(matches) == 1 else None


def correlate_topology(freshness: FreshnessCheck | None, graph_id: str | None = None,
                        graph_map: dict[str, str] | None = None) -> dict[str, Any]:
    """Map one Deployment endpoint's `FreshnessCheck` to viewer display facts.

    A drifted (`CHANGED`) document is shown as stale and is never used to
    correlate recorded execution (ADR-0011/0015): its Node ids are withheld
    entirely rather than risking a stale-but-labeled-current picture. Absent,
    unreachable, and invalid documents carry no Node structure either.
    `freshness is None` means a connection exists but this call did not probe
    its topology (`NOT_CHECKED`); `build_catalog` uses `NO_CONNECTION`
    instead when there is no connection at all for the Graph.

    `graph_id`/`graph_map` (#43) resolve a multi-graph document: when the
    connection's explicit `graph_map` names exactly one document-local graph
    for `graph_id`, that graph's Nodes are used. Without a match -- no
    `graph_map`, no entry for `graph_id`, or more than one document-local
    graph mapped to it -- Node structure stays withheld exactly as for any
    other multi-graph document; the mapping is never guessed by name.
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
    if len(document_graphs) == 1:
        node_ids = tuple(sorted(node["id"] for node in document_graphs[0]["structure"]["nodes"]))
        return {"status": CURRENT, "node_ids": node_ids, "warnings": tuple(warnings), "correlated": True}
    resolved = _resolve_multi_graph(document_graphs, graph_id, graph_map)
    if resolved is None:
        warnings.append(
            "document publishes more than one graph; no explicit Deployment/Graph "
            "association names one for this graph, so Node structure is withheld"
        )
        return {"status": CURRENT, "node_ids": (), "warnings": tuple(warnings), "correlated": False}
    node_ids = tuple(sorted(node["id"] for node in resolved["structure"]["nodes"]))
    return {"status": CURRENT, "node_ids": node_ids, "warnings": tuple(warnings), "correlated": True}


def _subjects_view(graph_runs: list[dict]) -> list[dict]:
    subjects: dict[tuple[str, str], list[dict]] = {}
    for run in graph_runs:
        key = (run["subject_type"], run["subject_id"])
        subjects.setdefault(key, []).append({
            "run_id": run["run_id"],
            "execution_ns": run["execution_ns"],
            "approval_wait_ns": run["approval_wait_ns"],
            "timeline_incomplete": run["timeline_incomplete"],
            "steps": [{"node": s["node"], "outcome": s["outcome"],
                       "resumed_from": s["resumed_from"], "repeated_execution": s["repeated_execution"],
                       "approval_wait_ns": s["approval_wait_ns"], "awaiting_resume": s["awaiting_resume"],
                       "attempts": [
                {"number": a["number"], "tier": a["tier"], "outcome": a["outcome"]} for a in s["attempts"]
            ]} for s in run["steps"]],
        })
    return [{"subject_type": subject_type, "subject_id": subject_id, "runs": subject_runs}
            for (subject_type, subject_id), subject_runs in sorted(subjects.items())]


def _graph_view(graph_id: str, connection: dict | None, correlation: dict, graph_runs: list[dict],
                 live_runs: list[dict], *, ambiguous_aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    unmatched_node_names = ()
    if correlation["correlated"]:
        recorded_nodes = {step["node"] for run in graph_runs for step in run["steps"]}
        unmatched_node_names = tuple(sorted(recorded_nodes - set(correlation["node_ids"])))
    return {
        "graph_id": graph_id,
        "deployment_alias": connection["alias"] if connection else None,
        "reachable": connection["reachable"] if connection else None,
        "topology_status": correlation["status"],
        "topology_nodes": correlation["node_ids"],
        "topology_warnings": correlation["warnings"],
        "unmatched_node_names": unmatched_node_names,
        "ambiguous_aliases": ambiguous_aliases,
        "subjects": _subjects_view(graph_runs),
        "live_runs": live_runs,
    }


def build_catalog(connections: dict[str, dict], topology: dict[str, FreshnessCheck], spans: dict,
                   live_runs: dict[str, dict] | None = None) -> dict[str, Any]:
    """The generic catalog: every connected or recorded Graph, its optional
    topology, its recorded execution, and any still-live execution, joined
    only by explicit identity.

    `connections`: `{alias: {"endpoint": str, "reachable": bool, "graphs": [graph_id, ...],
        "graph_map": {document_graph_id: cord_graph_id}}}`, `cord list`'s existing
        `/assistants` probe shape plus each alias's optional explicit association
        (#43; absent/`None`/`{}` all mean "no associations").
    `topology`: `{endpoint: FreshnessCheck}`, one check per connected endpoint.
    `spans`: the archive contract's `read_spans(...)` result (may be `{}`).
    `live_runs`: `{run_id: view}` from `cord_runtime.live_reconciliation.reconcile`
        (may be `None`/`{}`); each `view` needs `graph_id` to be placed --
        an entry whose Assistant could not be resolved to a Graph is dropped
        rather than guessed (#20). A `run_id` also present in `spans`'
        recorded execution is always dropped here too, defensively, even
        though `reconcile` already excludes it -- the durable record wins.

    Entries are scoped per connection, not merely per `graph_id` (#43): a
    Graph with a registered connection but zero Runs still renders (its
    `runs` list is simply empty); a Graph with recorded execution but no
    registered connection still renders, with `topology_status` `"no_connection"`,
    preserving unmatched execution evidence as a diagnostic rather than
    dropping it (#13). When more than one connection advertises the same
    `graph_id`, each still renders its own isolated entry, and any recorded or
    live execution for that `graph_id` -- with no other identity to attribute
    it to one of them -- renders under one additional `"ambiguous"` entry
    instead of being attached to any of them.
    """
    runs = build_execution_tree(spans)
    by_graph: dict[str, list[dict]] = {}
    for run in runs:
        by_graph.setdefault(run["graph_id"], []).append(run)
    recorded_run_ids = {run["run_id"] for run in runs}

    live_by_graph: dict[str, list[dict]] = {}
    for run_id, view in (live_runs or {}).items():
        if run_id in recorded_run_ids or not view.get("graph_id"):
            continue
        live_by_graph.setdefault(view["graph_id"], []).append(view)
    for views in live_by_graph.values():
        views.sort(key=lambda v: v["run_id"])

    connections_by_graph: dict[str, list[dict]] = {}
    for alias, info in connections.items():
        for graph_id in info.get("graphs", ()):
            connections_by_graph.setdefault(graph_id, []).append({
                "alias": alias, "endpoint": info["endpoint"], "reachable": info["reachable"],
                "graph_map": info.get("graph_map") or {},
            })

    entries: list[tuple[tuple, dict]] = []  # (sort_key, view)
    for graph_id in sorted(set(connections_by_graph) | set(by_graph) | set(live_by_graph)):
        graph_connections = connections_by_graph.get(graph_id, [])
        graph_runs = by_graph.get(graph_id, [])
        graph_live_runs = live_by_graph.get(graph_id, [])

        if not graph_connections:
            correlation = {"status": NO_CONNECTION, "node_ids": (), "warnings": (), "correlated": False}
            entries.append(((graph_id, 0),
                             _graph_view(graph_id, None, correlation, graph_runs, graph_live_runs)))
            continue

        ambiguous = len(graph_connections) > 1
        for connection in sorted(graph_connections, key=lambda c: c["alias"]):
            correlation = correlate_topology(topology.get(connection["endpoint"]), graph_id,
                                              connection["graph_map"])
            own_runs = [] if ambiguous else graph_runs
            own_live = [] if ambiguous else graph_live_runs
            entries.append(((graph_id, 1, connection["alias"]),
                             _graph_view(graph_id, connection, correlation, own_runs, own_live)))

        if ambiguous and (graph_runs or graph_live_runs):
            aliases = tuple(sorted(c["alias"] for c in graph_connections))
            correlation = {
                "status": AMBIGUOUS, "node_ids": (), "correlated": False,
                "warnings": (f"graph_id '{graph_id}' is advertised by more than one connection "
                             f"({', '.join(aliases)}); recorded/live execution cannot be attributed "
                             "to one of them and is shown here instead of being guessed",),
            }
            entries.append(((graph_id, 2),
                             _graph_view(graph_id, None, correlation, graph_runs, graph_live_runs,
                                         ambiguous_aliases=aliases)))

    return {"graphs": [view for _, view in sorted(entries, key=lambda e: e[0])]}


def _format_live_source(live: dict[str, Any]) -> str:
    """Render one live-run view's status/source as the bracketed suffix
    `format_catalog_text` appends to its line (#20 AC1/AC2/AC5)."""
    status = live["aegra_status"]
    if live["source"] == LIVE:
        return f"live, {status}"
    seconds = int(live["seconds_since_completion"])
    if live["source"] == INGESTION_PENDING:
        return f"live, {status}, ingestion pending {seconds}s"
    if live["source"] == INGESTION_FAILED:
        return f"live, {status}, ingestion FAILED after {seconds}s"
    return f"live, {status}"  # pragma: no cover -- defensive; reconcile() emits only the above


def format_catalog_text(catalog: dict[str, Any]) -> str:
    """A readable, deterministic text rendering (`cord view`'s default output)."""
    if not catalog["graphs"]:
        return "no Graphs: no connections registered and no recorded execution given"
    lines = []
    for graph in catalog["graphs"]:
        if graph["topology_status"] == AMBIGUOUS:
            deployment = f"ambiguous among {', '.join(graph['ambiguous_aliases'])}"
        else:
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
        if not graph["subjects"] and not graph["live_runs"]:
            lines.append("  no recorded Runs")
        for live in graph["live_runs"]:
            lines.append(f"  Live Run {live['run_id']} (thread {live['thread_id']}) "
                         f"[{_format_live_source(live)}]")
        for subject in graph["subjects"]:
            lines.append(f"  Subject {subject['subject_type']}:{subject['subject_id']}")
            for run in subject["runs"]:
                duration = f"executed {run['execution_ns'] / 1e9:.3f}s"
                if run["approval_wait_ns"]:
                    duration += f", waited {run['approval_wait_ns'] / 1e9:.3f}s for approval"
                if run["timeline_incomplete"]:
                    duration += ", timeline incomplete"
                lines.append(f"    Run {run['run_id']} ({duration})")
                for step in run["steps"]:
                    lines.append(f"      Step {step['node']} -> {step['outcome']}")
                    if step["repeated_execution"]:
                        lines.append(
                            f"        warning: repeated execution -- another Step already resumed "
                            f"from {step['resumed_from']}"
                        )
                    if step["awaiting_resume"]:
                        lines.append("        waiting for approval to resume")
                    if step["resumed_from"] is not None and step["approval_wait_ns"] is None:
                        lines.append(
                            f"        warning: resumed from {step['resumed_from']}, but that Step is not "
                            "in this archive -- approval wait time unknown"
                        )
                    for attempt in step["attempts"]:
                        tier = f", tier={attempt['tier']}" if attempt["tier"] else ""
                        lines.append(f"        Attempt {attempt['number']}{tier} -> {attempt['outcome']}")
    return "\n".join(lines)
