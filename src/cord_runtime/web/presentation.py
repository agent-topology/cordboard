"""Pure layout and URL helpers. No domain status or correlation decisions."""
from collections import deque
from urllib.parse import quote

from agent_topology.spec import derived_join_edges


# The single evidence-backed binding from a finite status value the shared
# read models already expose to a stable label, plain-language explanation,
# an evidence/ownership cue (which module/contract produced this fact), and
# a safe next action -- or an explicit statement that no action is needed or
# known (#71). Never a graph-specific interpretation (AGENTS.md no-descriptors
# rule): every "explain"/"action" here only restates what the cited module's
# own docstring already guarantees.
STATE_CATALOG = {
    "topology_status": {
        "absent": {
            "icon": "○", "label": "Absent",
            "explain": "No topology document is published at this Deployment's well-known path.",
            "evidence": "cord_runtime.topology probe of the connected endpoint",
            "action": "No action needed — topology is optional (ADR-0015); execution is unaffected. "
                      "Publish a document at /.well-known/agent-topology.manifest.json if a diagram is wanted.",
        },
        "unreachable": {
            "icon": "×", "label": "Unreachable",
            "explain": "The topology probe could not reach the endpoint (timeout or connection error).",
            "evidence": "cord_runtime.topology probe of the connected endpoint",
            "action": "Check the Deployment is running and reachable, then reload this page.",
        },
        "invalid": {
            "icon": "!", "label": "Invalid",
            "explain": "A document was returned but failed agent-topology schema validation.",
            "evidence": "cord_runtime.topology probe of the connected endpoint",
            "action": "Fix the published document against the agent-topology schema, then reload.",
        },
        "stale": {
            "icon": "◷", "label": "Stale",
            "explain": "The published topology changed since the last confirmed snapshot; it is not "
                      "used to correlate execution until re-synced (ADR-0011/0015).",
            "evidence": "cord_runtime.topology snapshot comparison",
            "action": "Re-sync this Deployment's topology snapshot, then reload.",
        },
        "current": {
            "icon": "✓", "label": "Current",
            "explain": "The published topology matches the last confirmed snapshot and is used to "
                      "correlate recorded execution below.",
            "evidence": "cord_runtime.topology probe + snapshot comparison",
            "action": "No action needed — this is the expected state for correlated execution.",
        },
        "not_checked": {
            "icon": "?", "label": "Not checked",
            "explain": "A connection exists, but its topology was not probed for this view.",
            "evidence": "cord_runtime.viewer.correlate_topology (no freshness check supplied)",
            "action": "Reload this page, or open the Graph page directly, to probe topology.",
        },
        "no_connection": {
            "icon": "◇", "label": "No connection",
            "explain": "No registered Deployment connection advertises this Graph; only recorded "
                      "execution is shown.",
            "evidence": "cord_runtime.connections registered aliases",
            "action": "Register a connection with `cord add <alias> <endpoint>` to live-observe this Graph.",
        },
        "ambiguous": {
            "icon": "≠", "label": "Ambiguous",
            "explain": "More than one connection advertises this graph_id, so its recorded/live "
                      "execution cannot be attributed to one of them.",
            "evidence": "cord_runtime.viewer.build_catalog connection scoping (#43)",
            "action": "Give each Deployment a distinct graph_id to disambiguate, or inspect the shared "
                      "execution here as-is.",
        },
    },
    "node_status": {
        "not_observed": {
            "icon": "—", "label": "Not observed",
            "explain": "No recorded Step touched this Node in this Run.",
            "evidence": "cord.* span attributes for this Run",
            "action": "No action needed — an unreached Node in a normal execution path.",
        },
        "passed": {
            "icon": "✓", "label": "Passed",
            "explain": "The recorded Step(s) at this Node completed without a failed/halted outcome.",
            "evidence": "declared Step outcome",
            "action": "No action needed.",
        },
        "failed": {
            "icon": "×", "label": "Failed",
            "explain": "The recorded Step at this Node declared a failed or halted outcome.",
            "evidence": "declared Step outcome",
            "action": "Open the Step/Attempt detail below for the declared failure reason.",
        },
        "paused": {
            "icon": "⏸", "label": "Paused",
            "explain": "A Step at this Node is awaiting resume — an open approval wait.",
            "evidence": "Step awaiting_resume flag",
            "action": "Check the approval action on this page, if one is available.",
        },
        "repeated": {
            "icon": "↻", "label": "Repeated",
            "explain": "More than one Step executed at this Node in this Run (a retry or a resumed "
                      "re-execution).",
            "evidence": "Step count per Node for this Run",
            "action": "No action needed — every Step is listed individually below.",
        },
    },
    "reachability": {
        True: {
            "icon": "✓", "label": "Connected",
            "explain": "The last probe of this Deployment's endpoint succeeded.",
            "evidence": "cord list probe (GET /assistants)",
            "action": "No action needed.",
        },
        False: {
            "icon": "×", "label": "Disconnected",
            "explain": "The last probe of this Deployment's endpoint failed or timed out.",
            "evidence": "cord list probe (GET /assistants)",
            "action": "Check the Deployment process and network path, then reload. Existing recorded "
                      "execution stays visible and is not affected.",
        },
    },
    "run_source": {
        "recorded": {
            "icon": "✓", "label": "Recorded",
            "explain": "This Run's full execution record has arrived in the archive.",
            "evidence": "archive spans (cord_runtime.archive_query)",
            "action": "No action needed.",
        },
        "live": {
            "icon": "…", "label": "Live",
            "explain": "This Run is still executing, or finished too recently for its archive "
                      "record to arrive; only identity and current status are known so far.",
            "evidence": "Aegra SSE lifecycle stream (cord_runtime.aegra_client)",
            "action": "No action needed — this page updates automatically as evidence arrives.",
        },
        "ingestion_pending": {
            "icon": "⏳", "label": "Ingestion pending",
            "explain": "This Run finished, but its durable archive record has not arrived yet.",
            "evidence": "cord_runtime.live_reconciliation (bounded wait since completion)",
            "action": "No action needed yet — this resolves automatically once the archive record "
                      "arrives, or becomes ingestion-failed after the bounded deadline.",
        },
        "ingestion_failed": {
            "icon": "!", "label": "Ingestion failed",
            "explain": "This Run finished, but no durable archive record arrived within the "
                      "reconciliation deadline. Runtime success does not imply telemetry delivery.",
            "evidence": "cord_runtime.live_reconciliation (deadline elapsed)",
            "action": "Check the Collector and archive path for this Deployment; this Run's own "
                      "outcome is not lost, but it is not durably recorded here.",
        },
    },
    "live_status": {
        "pending": {
            "label": "Queued", "explain": "The Run is queued by Aegra but has not started.",
            "evidence": "Aegra Run status", "action": "No action needed.",
        },
        "running": {
            "label": "In progress", "explain": "The Run is currently executing.",
            "evidence": "Aegra Run status", "action": "No action needed.",
        },
        "interrupted": {
            "label": "Waiting on approval", "explain": "The Run is paused awaiting an approval response.",
            "evidence": "Aegra Run status",
            "action": "Check the approval action on this page, if one is available.",
        },
        "success": {
            "label": "Finished successfully", "explain": "The Run finished successfully.",
            "evidence": "Aegra Run status", "action": "No action needed.",
        },
        "error": {
            "label": "Failed", "explain": "The Run finished with a failure.",
            "evidence": "Aegra Run status", "action": "No action needed here — inspect the Run's own "
                      "declared outcome once its archive record arrives.",
        },
        "timeout": {
            "label": "Timed out", "explain": "The Run stopped after timing out.",
            "evidence": "Aegra Run status", "action": "No action needed here — inspect the Run's own "
                      "declared outcome once its archive record arrives.",
        },
    },
    "action_slot": {
        "ready": {
            "label": "Approval ready",
            "explain": "Exactly one waiting interrupt matches this Run's Thread, and this Deployment "
                      "has approval authority configured.",
            "evidence": "cord_runtime.approval_inbox.discover_waiting_for_thread",
            "action": "Respond to this approval.",
        },
        "ambiguous": {
            "label": "Multiple pending interrupts",
            "explain": "More than one interrupt is waiting on this Thread, so this Run's own interrupt "
                      "cannot be identified.",
            "evidence": "cord_runtime.approval_inbox.discover_waiting_for_thread",
            "action": "Resolve from the approval inbox, where each interrupt is listed individually.",
        },
        "no_authority": {
            "label": "No approval authority",
            "explain": "This Deployment has no auth_endpoint configured, so approval is refused by "
                      "default (ADR-0019).",
            "evidence": "connection record (auth_endpoint)",
            "action": "Configure auth_endpoint for this connection to enable approval.",
        },
        "none": {
            "label": "No waiting interrupt",
            "explain": "This Run is paused, but no currently-waiting interrupt matches its Thread.",
            "evidence": "cord_runtime.approval_inbox.discover_waiting_for_thread",
            "action": "No action available — it may already have been answered, resumed elsewhere, "
                      "or expired.",
        },
    },
    "submission_result": {
        "resumed": {
            "label": "Resumed",
            "explain": "The response was authorized and successfully resumed the Run.",
            "evidence": "cord_runtime.approval_inbox.submit_response",
            "action": "No action needed.",
        },
        "duplicate": {
            "label": "Already submitted",
            "explain": "A response to this exact interrupt was already claimed.",
            "evidence": "cord_runtime.response_dedupe",
            "action": "Check the Run's current state before submitting again; this submission was "
                      "not applied twice.",
        },
        "rejected": {
            "label": "Rejected",
            "explain": "The connection's configured authorization boundary declined this response.",
            "evidence": "connection auth_endpoint decision (cord_runtime.entity_auth)",
            "action": "Confirm the approver identity and revision, then retry if appropriate.",
        },
        "unknown": {
            "label": "Outcome unconfirmed",
            "explain": "The transport outcome of this submission could not be confirmed.",
            "evidence": "cord_runtime.approval_inbox.submit_response (ambiguous transport)",
            "action": "Check the Deployment or the Run list before submitting again; do not "
                      "resubmit blindly.",
        },
        "deployment_unavailable": {
            "label": "Deployment unavailable",
            "explain": "The managed Deployment could not be started to receive this response.",
            "evidence": "cord_runtime.deployment_lifecycle.ensure_started",
            "action": "Check the Deployment process, then retry.",
        },
    },
    "archive_health": {
        "pending": {
            "label": "Archive pending",
            "explain": "No archive files exist yet at the configured path.",
            "evidence": "cord_runtime.archive_health",
            "action": "No action needed before any execution has been recorded.",
        },
        "empty": {
            "label": "Archive empty",
            "explain": "Archive files exist but contain no spans yet.",
            "evidence": "cord_runtime.archive_health",
            "action": "No action needed.",
        },
        "incomplete": {
            "label": "Archive write incomplete",
            "explain": "The archive's most recent write is still in progress; complete records "
                      "remain visible.",
            "evidence": "cord_runtime.archive_health",
            "action": "No action needed.",
        },
        "failed": {
            "label": "Archive read failed",
            "explain": "A complete archive record failed validation and could not be read.",
            "evidence": "cord_runtime.archive_health",
            "action": "Check the archive files at the configured path for corruption.",
        },
        "healthy": {
            "label": "Archive healthy",
            "explain": "The archive is readable and contains valid spans.",
            "evidence": "cord_runtime.archive_health",
            "action": "No action needed.",
        },
    },
    # Wired into the catalog for forward compatibility, but not yet probed
    # live: connections.py has no per-connection Collector health endpoint
    # field today, and adding one is a connection-scoped product decision
    # (ADR-0014) outside this presentation-only change (#71 contract gap).
    "collector_health": {
        "reachable": {
            "label": "Collector reachable",
            "explain": "The Collector's health endpoint responded.",
            "evidence": "cord_runtime.collector_health",
            "action": "No action needed.",
        },
        "unavailable": {
            "label": "Collector unavailable",
            "explain": "The Collector's health endpoint did not respond.",
            "evidence": "cord_runtime.collector_health",
            "action": "Check the Collector process; exported telemetry may not be recorded until "
                      "it recovers.",
        },
    },
}

TOPOLOGY_LABELS = {key: (v["icon"], v["label"]) for key, v in STATE_CATALOG["topology_status"].items()}

NODE_STATUS_LABELS = {key: (v["icon"], v["label"]) for key, v in STATE_CATALOG["node_status"].items()}


def describe(family, key):
    """The stable label/explanation/evidence/action for one cataloged state.

    A value absent from ``STATE_CATALOG`` is an explicit, visible contract
    gap rather than a guessed explanation (#71 Blockers and handoff): no
    trustworthy owner or action is invented for a state this catalog does
    not yet know.
    """
    entry = STATE_CATALOG.get(family, {}).get(key)
    if entry is not None:
        return entry
    return {
        "icon": "?", "label": str(key) if key is not None else "Unknown",
        "explain": "This state is not yet in the catalog.",
        "evidence": "not classified",
        "action": "No safe action is known for this uncataloged state — treat it as a contract gap "
                  "and report it rather than guessing.",
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
