"""Read a Deployment's optional published topology and warn on drift, gaps,
and fan-out interrupts (#11).

Discovery is never a precondition for connecting or recording a Graph
(ADR-0015): every function here reports an explicit status instead of raising
for a missing, unreachable, or schema-invalid document. Documents are read
through the public `agent_topology.spec` API and passed through unchanged
(ADR-0014) -- no section is added, no id is rewritten, and `graphs[].id`
(a document-local address) is never equated with the execution identity
`cord.graph.id`.
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from agent_topology.spec import validate_document
import requests

from cord_runtime.connections import validate_endpoint

WELL_KNOWN_PATH = "/.well-known/agent-topology.manifest.json"

SNAPSHOTS_DIRNAME = ".cordboard"
SNAPSHOTS_FILENAME = "topology-snapshots.json"

# The agent-topology x-topology-interpretation revisions this reader
# understands (AT-2/AT-3, experimental). Revision 2 preserves revision 1's
# branch contract and adds materialized-child facts. A missing or different
# revision is opaque, not an error: R3 falls back to "cannot confirm".
RECOGNIZED_INTERPRETATION_VERSIONS = ("1", "2")

ABSENT = "absent"
UNREACHABLE = "unreachable"
INVALID = "invalid"
VALID = "valid"

NO_SNAPSHOT = "no_snapshot"
UNCHANGED = "unchanged"
CHANGED = "changed"


class TopologySnapshotError(ValueError):
    """Invalid local snapshot storage; distinct from a fetch/validation failure."""


@dataclass(frozen=True)
class TopologyReading:
    """One fetch attempt against a Deployment's well-known topology document."""

    status: str  # ABSENT | UNREACHABLE | INVALID | VALID
    document: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()
    reason: str | None = None  # fixed status only; never a response body or URL


def fetch_topology(endpoint: str, *, timeout: float = 5.0, session: requests.Session | None = None) -> TopologyReading:
    """Fetch and schema-validate the well-known topology document.

    Absent (404) is distinct from unreachable (no response, or a status other
    than 200/404) and from invalid (a 200 response that is not valid JSON or
    fails the pinned schema). Never raises for any of these.
    """
    url = validate_endpoint(endpoint) + WELL_KNOWN_PATH
    client = session or requests
    try:
        response = client.get(url, timeout=timeout, allow_redirects=False)
    except requests.Timeout:
        return TopologyReading(status=UNREACHABLE, reason="timeout")
    except requests.RequestException:
        return TopologyReading(status=UNREACHABLE, reason="connection_error")
    if response.status_code == 404:
        return TopologyReading(status=ABSENT)
    if response.status_code != 200:
        return TopologyReading(status=UNREACHABLE, reason=f"http_{response.status_code}")
    try:
        raw = response.json()
    except ValueError:
        return TopologyReading(status=INVALID, errors=("response body is not valid JSON",))
    errors = validate_document(raw)
    if errors:
        return TopologyReading(status=INVALID, errors=tuple(errors))
    return TopologyReading(status=VALID, document=raw)


def graphs(document: dict[str, Any]) -> list[dict[str, Any]]:
    """The document's graphs, exactly as published and in document order."""
    return document["graphs"]


def graph_by_id(document: dict[str, Any], graph_id: str) -> dict[str, Any] | None:
    """Look up a graph by its document-local address (`graphs[].id`).

    That address is not an execution identity; do not compare it to
    `cord.graph.id` (ADR-0014).
    """
    for graph in graphs(document):
        if graph["id"] == graph_id:
            return graph
    return None


@dataclass(frozen=True)
class ParallelInterruptWarning:
    """R3 (ADR-0007, downgraded to a warning by ADR-0015): a static interrupt
    inside a direct fan-out branch, judged from core `edges[].kind` and
    `nodes[].interrupts` facts alone.

    `confirmed` reflects agent-topology's experimental branch interpretation
    extension: True only when a recognized revision marks the fan-out source
    `all-declared` with `status: known`. Missing, unknown, or an unrecognized
    revision leaves it False -- the fan-out's parallel semantics cannot be
    confirmed, not that it is safe. Dynamic `interrupt()` calls inside a node
    body are never visible here; that blind spot ships as a `dynamic-interrupts`
    entry in the document's `producerLimitations`, not as a warning field.
    """

    graph_id: str
    source_node_id: str
    fanout_target_ids: tuple[str, ...]
    interrupted_target_ids: tuple[str, ...]
    confirmed: bool


def _branch_interpretation(graph: dict[str, Any]) -> dict[str, str]:
    """nodeId -> known branch value for recognized interpretation revisions.
    Any other revision, or a malformed extension, is opaque."""
    extension = graph.get("x-topology-interpretation")
    if not isinstance(extension, dict) or extension.get("version") not in RECOGNIZED_INTERPRETATION_VERSIONS:
        return {}
    result: dict[str, str] = {}
    for node in extension.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        branch = node.get("branch")
        node_id = node.get("nodeId")
        if isinstance(branch, dict) and isinstance(node_id, str) and branch.get("status") == "known":
            result[node_id] = branch.get("value")
    return result


def parallel_interrupt_warnings(document: dict[str, Any]) -> list[ParallelInterruptWarning]:
    """R3 across every graph in the document."""
    warnings: list[ParallelInterruptWarning] = []
    for graph in graphs(document):
        structure = graph["structure"]
        interrupted_nodes = {node["id"] for node in structure["nodes"] if node.get("interrupts")}
        if not interrupted_nodes:
            continue
        fanout: dict[str, list[str]] = {}
        for edge in structure["edges"]:
            if edge["kind"] == "direct":
                fanout.setdefault(edge["source"], []).append(edge["target"])
        interpretation = _branch_interpretation(graph)
        for source, targets in fanout.items():
            if len(targets) < 2:
                continue
            hit = tuple(sorted(t for t in targets if t in interrupted_nodes))
            if not hit:
                continue
            warnings.append(ParallelInterruptWarning(
                graph_id=graph["id"],
                source_node_id=source,
                fanout_target_ids=tuple(sorted(targets)),
                interrupted_target_ids=hit,
                confirmed=interpretation.get(source) == "all-declared",
            ))
    return warnings


def snapshots_path(board_dir: Path) -> Path:
    return Path(board_dir) / SNAPSHOTS_DIRNAME / SNAPSHOTS_FILENAME


def _load_snapshots(board_dir: Path) -> dict[str, Any]:
    path = snapshots_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TopologySnapshotError(f"snapshots file is not valid JSON: {path}") from exc
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise TopologySnapshotError(f"snapshots file must be a JSON object of endpoint -> snapshot: {path}")
    return data


def get_snapshot(board_dir: Path, endpoint: str) -> dict[str, Any] | None:
    """The last explicitly refreshed snapshot for this endpoint, or None."""
    return _load_snapshots(board_dir).get(validate_endpoint(endpoint))


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".topology-snapshots-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def refresh_snapshot(board_dir: Path, endpoint: str, *, timeout: float = 5.0,
                     session: requests.Session | None = None) -> TopologyReading:
    """Fetch now and, only on a valid document, atomically replace the stored
    snapshot for this endpoint. This is the explicit refresh path a future
    `cord sync` reuses (ADR-0011/0015). An absent, unreachable, or invalid
    reading never touches a stored snapshot.
    """
    endpoint = validate_endpoint(endpoint)
    reading = fetch_topology(endpoint, timeout=timeout, session=session)
    if reading.status == VALID:
        data = _load_snapshots(board_dir)
        data[endpoint] = {
            "fetchedAt": datetime.now(UTC).isoformat(),
            "structureHash": reading.document["structureHash"],
            "document": reading.document,
        }
        _write_atomic(snapshots_path(board_dir), data)
    return reading


@dataclass(frozen=True)
class FreshnessCheck:
    """Whether a fresh fetch's structure hash matches the stored snapshot.

    `unchanged` means the published document has not changed since the last
    explicit refresh -- it is not proof the running code still matches the
    document; Cordboard never re-derives from source, so that evidence is
    unavailable (ADR-0011/0015). When the fetch itself is not valid, drift is
    unknown rather than confirmed either way, and the stored snapshot (if any)
    is left untouched -- a caller should show it as unconfirmed, not current.
    """

    status: str  # NO_SNAPSHOT | UNCHANGED | CHANGED | ABSENT | UNREACHABLE | INVALID
    reading: TopologyReading


def check_freshness(board_dir: Path, endpoint: str, *, timeout: float = 5.0,
                    session: requests.Session | None = None) -> FreshnessCheck:
    """Compare a fresh fetch against the stored snapshot without refreshing it."""
    endpoint = validate_endpoint(endpoint)
    reading = fetch_topology(endpoint, timeout=timeout, session=session)
    if reading.status != VALID:
        return FreshnessCheck(status=reading.status, reading=reading)
    stored = get_snapshot(board_dir, endpoint)
    if stored is None:
        return FreshnessCheck(status=NO_SNAPSHOT, reading=reading)
    changed = stored["structureHash"] != reading.document["structureHash"]
    return FreshnessCheck(status=CHANGED if changed else UNCHANGED, reading=reading)
