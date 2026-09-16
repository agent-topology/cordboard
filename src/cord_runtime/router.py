"""One generic Signal -> Rule -> Assistant routing path for all sources (#16).

``route_signal`` is the single entry point manual, file, and schedule Signals
all pass through (ADR-0013 switchboard boundary). It evaluates only a Rule's
declared match/mapping expressions -- bounded dot-paths into the Signal
payload -- and never inspects payload meaning to pick or substitute a target.
Unmatched Signals and invalid mappings are recorded with bounded, sanitized
diagnostics (Signal type/id and a reason string) so they stay visible without
ever persisting the payload itself.

Three durable, restart-safe checks (#17) sit between a matched Rule and the
actual submission, in this order:

1. The declared (Assistant, Subject) concurrency claim
   (`cord_runtime.concurrency`, "skip" is the only implemented policy) --
   applied first because a conflict means no Deployment needs to start and no
   Signal-ID claim should be spent.
2. Managed Deployment startup (`cord_runtime.deployment_lifecycle`) -- a
   startup/health failure also stops short of claiming the Signal-ID, since
   nothing was submitted and a corrected redelivery should be free to retry.
3. The Signal-ID dedupe claim (`cord_runtime.signal_dedupe`) -- claimed only
   immediately before `execute()`, and never rolled back afterwards, because
   a failed or ambiguous submission result still means the Run may have been
   accepted.
"""

from datetime import datetime, timezone
import json
from pathlib import Path

from cord_runtime.aegra_client import execute
from cord_runtime.concurrency import release as release_concurrency, try_claim as try_claim_concurrency
from cord_runtime.connections import InvalidConnection, get_connection
from cord_runtime.deployment_lifecycle import ensure_started, release_active
from cord_runtime.rules import load_rules
from cord_runtime.signal_dedupe import claim as claim_signal


class MappingError(ValueError):
    """A matched Rule's declared mapping or Subject path is missing from the Signal payload."""


def _lookup(payload: dict, path: str):
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise MappingError(f"path '{path}' not found in signal payload")
        node = node[part]
    return node


def _matches(rule: dict, signal: dict) -> bool:
    if rule["signal_type"] != signal["type"]:
        return False
    payload = signal["payload"]
    for path, expected in rule.get("match", {}).items():
        try:
            if _lookup(payload, path) != expected:
                return False
        except MappingError:
            return False
    return True


def _apply(rule: dict, signal: dict) -> tuple[dict, str]:
    payload = signal["payload"]
    graph_input = {key: _lookup(payload, path) for key, path in rule.get("input", {}).items()}
    subject = _lookup(payload, rule["subject"])
    if not isinstance(subject, str) or not subject.strip():
        raise MappingError(f"Subject path '{rule['subject']}' did not resolve to a non-empty string")
    return graph_input, subject


def _unmatched_path(board_dir: Path) -> Path:
    return Path(board_dir) / ".cordboard" / "unmatched.json"


def _record_unmatched(board_dir: Path, signal: dict, reason: str) -> None:
    path = _unmatched_path(board_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = []
    existing.append({"signal_type": signal["type"], "signal_id": signal["id"], "reason": reason})
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


_STALE_CLAIM_BUFFER = 30.0  # seconds of slack over timeout + health_timeout before a claim is reclaimable
_HEALTH_TIMEOUT = 30.0


def route_signal(board_dir: Path, signal: dict, *, timeout: float = 120.0,
                  now: datetime | None = None) -> dict:
    """Route one Signal through its first matching Rule and execute it.

    Returns ``{"status": "routed", ..., "result": <execute() outcome>}`` on a
    match, ``{"status": "unmatched" | "invalid_mapping", "signal_id": ...}``
    when no Rule applies or a matched Rule's mapping/Subject path could not be
    resolved from the payload (both recorded via ``_record_unmatched`` with no
    payload content), ``{"status": "concurrency_skipped", "signal_id": ...}``
    when another Run is already in flight for the same declared
    (Assistant, Subject) pair, ``{"status": "deployment_unavailable", ...,
    "error": ...}`` when a matched Rule's managed Deployment failed to start
    or become healthy, ``{"status": "duplicate", "signal_id": ...}`` when this
    Signal ID already has a live dedupe claim, or
    ``{"status": "execution_failed", ..., "error": ...}`` when a matched,
    well-mapped Rule's target could not be reached or did not accept the Run
    -- ``error`` is ``execute()``'s own payload-free message.

    ``now`` is an injected clock (default: real UTC time) so the
    concurrency/dedupe/lifecycle checks above are deterministically testable.
    """
    board_dir = Path(board_dir)
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    rules = load_rules(board_dir)
    matching = [r for r in rules if _matches(r, signal)]
    if not matching:
        _record_unmatched(board_dir, signal, "no rule declared for this signal")
        return {"status": "unmatched", "signal_id": signal["id"]}

    rule = matching[0]
    try:
        graph_input, subject = _apply(rule, signal)
        connection = get_connection(board_dir, rule["connection"])
    except (MappingError, InvalidConnection) as exc:
        _record_unmatched(board_dir, signal, str(exc))
        return {"status": "invalid_mapping", "signal_id": signal["id"], "rule": rule["name"]}

    assistant = rule["assistant"]
    stale_after = timeout + _HEALTH_TIMEOUT + _STALE_CLAIM_BUFFER
    if not try_claim_concurrency(board_dir, assistant, subject, now=now_ts, stale_after=stale_after):
        return {"status": "concurrency_skipped", "signal_id": signal["id"], "rule": rule["name"]}

    try:
        started = ensure_started(board_dir, rule["connection"], connection,
                                  now=now_ts, health_timeout=_HEALTH_TIMEOUT)
        if started["status"] == "start_failed":
            return {"status": "deployment_unavailable", "signal_id": signal["id"],
                    "rule": rule["name"], "error": started["error"]}

        if not claim_signal(board_dir, signal["id"], now=now_ts):
            return {"status": "duplicate", "signal_id": signal["id"], "rule": rule["name"]}

        try:
            result = execute(connection["endpoint"], assistant, subject, graph_input, timeout=timeout)
        except (ValueError, RuntimeError) as exc:
            # aegra_client.execute's exceptions are already payload/credential-free.
            return {"status": "execution_failed", "signal_id": signal["id"],
                    "rule": rule["name"], "error": str(exc)}
        return {"status": "routed", "signal_id": signal["id"], "rule": rule["name"], "result": result}
    finally:
        release_active(board_dir, rule["connection"], connection, now=now_ts)
        release_concurrency(board_dir, assistant, subject)
