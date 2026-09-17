"""One connection-scoped readiness diagnostic composed from existing
per-capability contracts (#72).

Separates the one required capability (execution reachability) from every
optional capability (topology, telemetry archive/collector, managed
lifecycle, approval authorization) so a caller never treats a missing
optional capability as connection failure (ADR-0015). Every check here is a
read-only probe or a local file/JSON read: it never submits a Run, resumes an
interrupt, refreshes a topology snapshot, or starts/stops a Deployment.

`telemetry.collector` is always ``UNSUPPORTED``: connections have no
per-connection Collector health endpoint field today, and inventing one to
probe here would guess at an undocumented contract (ADR-0014 decision
required; see `web/presentation.py`'s ``collector_health`` vocabulary).
`approval_authorization` reports configuration presence only -- there is no
passive health probe on `entity_auth`'s boundary, only `authorize()`, which
requires a real Thread/interrupt submission.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cord_runtime.archive_health import HealthStatus, archive_health
from cord_runtime.connections import is_managed
from cord_runtime.deployment_lifecycle import START_FAILED, load_lifecycle
from cord_runtime.topology import ABSENT, INVALID, UNREACHABLE, check_freshness
from cord_runtime.viewer import CURRENT, STALE, correlate_topology

READY = "ready"
UNAVAILABLE = "unavailable"
UNSUPPORTED = "unsupported"
NOT_CONFIGURED = "not_configured"

CAPABILITY_STATES = (READY, UNAVAILABLE, UNSUPPORTED, NOT_CONFIGURED)

_TOPOLOGY_STATE = {
    ABSENT: NOT_CONFIGURED,
    UNREACHABLE: UNAVAILABLE,
    INVALID: UNAVAILABLE,
    STALE: READY,
    CURRENT: READY,
}


@dataclass(frozen=True)
class CapabilityFact:
    """One capability's bounded readiness fact.

    ``status`` is the capability's own finer-grained vocabulary value (e.g.
    ``"current"``, ``"start_failed"``); ``state`` is always one of
    `CAPABILITY_STATES`, the finite cross-capability summary. ``detail`` is
    bounded and non-sensitive (a graph id list, a span count) -- never a raw
    payload, log line, or exception message.
    """

    required: bool
    state: str
    status: str
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class Telemetry:
    archive: CapabilityFact
    collector: CapabilityFact


@dataclass(frozen=True)
class Capabilities:
    execution: CapabilityFact
    topology: CapabilityFact
    telemetry: Telemetry
    managed_lifecycle: CapabilityFact
    approval_authorization: CapabilityFact


@dataclass(frozen=True)
class ConnectionDiagnostics:
    alias: str
    endpoint: str
    checked_at: str
    capabilities: Capabilities

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _execution(endpoint: str) -> CapabilityFact:
    # Local import: cli.py composes commands from this module, so a top-level
    # import here would cycle. web/observation.py already imports `_probe`
    # from cli.py the same way, for the same reason.
    from cord_runtime.cli import _probe

    probed = _probe(endpoint)
    reachable = probed["reachable"]
    return CapabilityFact(required=True, state=READY if reachable else UNAVAILABLE,
                          status="reachable" if reachable else "unreachable",
                          detail={"graphs": probed["graphs"]} if reachable else None)


def _topology(board_dir: Path, endpoint: str, *, timeout: float) -> CapabilityFact:
    freshness = check_freshness(board_dir, endpoint, timeout=timeout)
    correlated = correlate_topology(freshness)
    status = correlated["status"]
    detail = {"warnings": list(correlated["warnings"])} if correlated["warnings"] else None
    return CapabilityFact(required=False, state=_TOPOLOGY_STATE[status], status=status, detail=detail)


def _telemetry_archive(paths: tuple[Path, ...]) -> CapabilityFact:
    if not paths:
        return CapabilityFact(required=False, state=NOT_CONFIGURED, status="not_configured")
    health = archive_health(list(paths))
    state = UNAVAILABLE if health.status == HealthStatus.FAILED else READY
    detail = {"span_count": health.span_count} if health.status == HealthStatus.HEALTHY else None
    return CapabilityFact(required=False, state=state, status=health.status.value, detail=detail)


def _telemetry_collector() -> CapabilityFact:
    return CapabilityFact(required=False, state=UNSUPPORTED, status="unsupported")


def _managed_lifecycle(board_dir: Path, alias: str, connection: dict) -> CapabilityFact:
    if not is_managed(connection):
        return CapabilityFact(required=False, state=NOT_CONFIGURED, status="external")
    record = load_lifecycle(board_dir).get(alias)
    if record is None:
        return CapabilityFact(required=False, state=READY, status="never_started")
    status = record["status"]
    return CapabilityFact(required=False, state=UNAVAILABLE if status == START_FAILED else READY, status=status)


def _approval_authorization(connection: dict) -> CapabilityFact:
    configured = bool(connection.get("auth_endpoint"))
    return CapabilityFact(required=False, state=READY if configured else NOT_CONFIGURED,
                          status="configured" if configured else "not_configured")


def diagnose_connection(board_dir: Path, alias: str, connection: dict, *,
                        archive_paths: tuple[Path, ...] = (), timeout: float = 5.0,
                        now: datetime | None = None) -> ConnectionDiagnostics:
    """Compose one connection's readiness across every known capability.

    ``archive_paths`` mirrors `cord view`/`cord serve`'s own ``--archive``
    input (ADR-0014: an archive location is a board/session-level input, not
    a per-connection or per-graph stored setting).
    """
    endpoint = connection["endpoint"]
    capabilities = Capabilities(
        execution=_execution(endpoint),
        topology=_topology(board_dir, endpoint, timeout=timeout),
        telemetry=Telemetry(archive=_telemetry_archive(archive_paths), collector=_telemetry_collector()),
        managed_lifecycle=_managed_lifecycle(board_dir, alias, connection),
        approval_authorization=_approval_authorization(connection),
    )
    checked_at = (now or datetime.now(UTC)).isoformat()
    return ConnectionDiagnostics(alias=alias, endpoint=endpoint, checked_at=checked_at, capabilities=capabilities)
