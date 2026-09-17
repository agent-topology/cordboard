"""Pure layout and URL helpers. No domain status or correlation decisions."""
from collections import deque
from urllib.parse import quote

from agent_topology.spec import derived_join_edges


TOPOLOGY_LABELS = {
    "absent": ("○", "Absent"), "unreachable": ("×", "Unreachable"),
    "invalid": ("!", "Invalid"), "stale": ("◷", "Stale"),
    "current": ("✓", "Current"), "not_checked": ("?", "Not checked"),
    "no_connection": ("◇", "No connection"), "ambiguous": ("≠", "Ambiguous"),
}

NODE_STATUS_LABELS = {
    "not_observed": ("—", "Not observed"), "passed": ("✓", "Passed"),
    "failed": ("×", "Failed"), "paused": ("⏸", "Paused"), "repeated": ("↻", "Repeated"),
}


def graph_url(graph):
    gid = quote(graph["graph_id"], safe="")
    if graph["deployment_alias"] is not None:
        return "/connections/" + quote(graph["deployment_alias"], safe="") + "/graphs/" + gid
    return "/graphs/" + gid + ("/ambiguous" if graph["topology_status"] == "ambiguous" else "")


def run_url(graph, run):
    return graph_url(graph) + "/runs/" + quote(run["run_id"], safe="")


def execute_url(graph):
    return graph_url(graph) + "/execute"


def approval_url(alias, thread_id, interrupt_id):
    return "/approvals/" + "/".join(quote(part, safe="") for part in (alias, thread_id, interrupt_id))


def layout(structure):
    if not structure:
        return None
    nodes = [node["id"] for node in structure["nodes"]]
    edges = list(structure["edges"]) + [
        {**edge, "kind": "AND join"} for edge in derived_join_edges(structure)]
    adjacency = {node: [] for node in nodes}
    for edge in edges:
        adjacency[edge["source"]].append(edge["target"])
    depths = {}
    for root in list(structure["entryNodeIds"]) + nodes:
        if root in depths:
            continue
        depths[root] = 0 if not depths else max(depths.values()) + 1
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for target in adjacency[node]:
                if target not in depths:
                    depths[target] = depths[node] + 1
                    queue.append(target)
    rows = {}
    for node in nodes:
        rows.setdefault(depths[node], []).append(node)
    positions = {}
    # A bounded viewBox scales down without causing viewport overflow.
    width = max((len(row) for row in rows.values()), default=1) * 240
    for depth, row in rows.items():
        for col, node in enumerate(row):
            positions[node] = (width * (col + .5) / len(row), depth * 100 + 40)
    return {"width": width, "height": (max(rows, default=0) + 1) * 100,
            "nodes": [{"id": node, "x": positions[node][0], "y": positions[node][1]} for node in nodes],
            "edges": [{**edge, "start": positions[edge["source"]], "end": positions[edge["target"]]}
                      for edge in edges]}


def _node_status(node_steps):
    if not node_steps:
        return "not_observed"
    if any(step["awaiting_resume"] for step in node_steps):
        return "paused"
    if len(node_steps) > 1:
        return "repeated"
    return "failed" if node_steps[0]["outcome"] in ("failed", "halted") else "passed"


def run_topology(structure, run):
    """Overlay one Run's observed Step evidence onto its topology layout (#70).

    A Node with more than one Step -- a same-Node retry, or a pause whose
    resuming Step re-executes the same Node -- keeps every Step listed under
    that Node rather than collapsing them into one fabricated outcome; a
    Step whose `node` has no match in `structure` is never guessed onto a
    Node by name and is surfaced separately as `unmatched_steps` instead.
    Returns `None`, exactly like `layout`, when there is no current
    correlated topology to overlay onto -- absent/stale/invalid/ambiguous
    topology never receives a guessed path (ADR-0015).
    """
    diagram = layout(structure)
    if diagram is None:
        return None
    node_ids = {node["id"] for node in structure["nodes"]}
    by_node: dict[str, list[dict]] = {}
    for step in run.get("steps", []):
        by_node.setdefault(step["node"], []).append(step)
    for node in diagram["nodes"]:
        node_steps = by_node.get(node["id"], [])
        node["steps"] = node_steps
        node["status"] = _node_status(node_steps)
    diagram["unmatched_steps"] = [step for name, group in by_node.items()
                                  if name not in node_ids for step in group]
    return diagram


def timeline(run):
    if "start_ns" not in run:
        return []
    start, end = run["start_ns"], run["end_ns"]
    extent = end - start
    rows = []

    def add(label, record, kind):
        left = 100 * (record["start_ns"] - start) / extent if extent else 0
        width = 100 * (record["end_ns"] - record["start_ns"]) / extent if extent else 0
        rows.append({"label": label, "kind": kind, "start": record["start_ns"],
                     "end": record["end_ns"], "duration": record["end_ns"] - record["start_ns"],
                     "left": left, "width": width})
    add("Run", run, "run")
    for step in run["steps"]:
        wait = step["approval_wait_ns"]
        if wait is not None:
            add("Approval wait", {"start_ns": step["start_ns"] - wait, "end_ns": step["start_ns"]}, "wait")
        add("Step " + step["node"], step, "step")
        for attempt in step["attempts"]:
            add("Attempt " + str(attempt["number"]), attempt, "attempt")
    return rows


LIVE_STATUS_PURPOSE = {
    "pending": "queued", "running": "in progress", "interrupted": "waiting on approval",
    "success": "finished successfully", "error": "failed", "timeout": "timed out",
}


def scenario_summary(run):
    """A plain-English purpose for one Run, derived only from vocabulary the
    shared model already exposes (declared Step/Attempt outcomes or Aegra
    status) -- never a graph-specific interpretation of its state (AGENTS.md
    no-descriptors rule)."""
    if "start_ns" in run:
        steps = run.get("steps", [])
        if any(step.get("outcome") == "awaiting_approval" or step.get("awaiting_resume") for step in steps):
            return "Recorded Run: paused, awaiting approval"
        attempts = [a["outcome"] for step in steps for a in step.get("attempts", [])]
        if len(attempts) > 1:
            return f"Recorded Run: {len(attempts)} Attempts ({', '.join(attempts)})"
        if attempts:
            return f"Recorded Run: {attempts[0]}"
        return "Recorded Run: no declared Step outcomes"
    purpose = LIVE_STATUS_PURPOSE.get(run.get("aegra_status"), run.get("aegra_status") or "unknown status")
    return f"Run: {purpose}"


def matches(run, subject, status):
    subject_value = run.get("subject_id", run.get("subject", "")) or ""
    outcomes = {s["outcome"] for s in run.get("steps", [])}
    outcomes.update(a["outcome"] for s in run.get("steps", []) for a in s["attempts"])
    return ((not subject or subject in subject_value)
            and (not status or status == run.get("aegra_status") or status in outcomes))


def duration(ns):
    if ns is None:
        return "Unknown"
    return f"{ns:,} ns" if abs(ns) < 1_000_000 else f"{ns / 1_000_000:,.3f} ms"
