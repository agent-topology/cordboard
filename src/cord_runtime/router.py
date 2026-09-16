"""One generic Signal -> Rule -> Assistant routing path for all sources
(#16), including the ``run.finished`` cascade (#18).

``route_signal`` is the single entry point manual, file, schedule, and
``run.finished`` Signals all pass through (ADR-0013 switchboard boundary).
It evaluates only a Rule's declared match/when/mapping expressions --
bounded dot-paths into the Signal payload -- and never inspects payload
meaning to pick or substitute a target. Unmatched Signals and invalid
mappings are recorded with bounded, sanitized diagnostics (Signal type/id
and a reason string) so they stay visible without ever persisting the
payload itself.

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

Chaining (#18): a matched Rule's execution reaching logical Run terminal
state (`execute()`'s own ``"success"``, never each Aegra API Run completion
-- a pause/resume must not create a false cascade) makes `route_signal`
construct that Run's own `run.finished` Signal and route it again, after this
call's own claims are released so a cascade Run never contends with its own
parent's (Assistant, Subject) claim. Recursion is bounded by
`MAX_CASCADE_DEPTH`; the same #17 Signal-ID dedupe claim (keyed on the
completed Run's id) makes a redelivered or re-derived completion for one Run
cascade at most once.
"""

from datetime import datetime, timezone
import json
from pathlib import Path

from cord_runtime.aegra_client import execute
from cord_runtime.concurrency import release as release_concurrency, try_claim as try_claim_concurrency
from cord_runtime.connections import InvalidConnection, get_connection
from cord_runtime.deployment_lifecycle import ensure_started, release_active
from cord_runtime.rules import load_rules
from cord_runtime.run_continuity import record_submission
from cord_runtime.signal_dedupe import claim as claim_signal
from cord_runtime.signals import run_finished_signal

MAX_CASCADE_DEPTH = 5


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
    condition = rule["when"] if rule["signal_type"] == "run.finished" else rule.get("match", {})
    for path, expected in condition.items():
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
    Signal ID already has a live dedupe claim, ``{"status":
    "cascade_depth_exceeded", "signal_id": ..., "depth": ...}`` when a
    ``run.finished`` Signal already carries the planned maximum cascade depth
    (#18), or ``{"status": "execution_failed", ..., "error": ...}`` when a
    matched, well-mapped Rule's target could not be reached or did not accept
    the Run -- ``error`` is ``execute()``'s own payload-free message.

    A ``"routed"`` result whose execution reached logical Run terminal state
    also carries ``"cascade"``: this call's own recursive ``route_signal``
    outcome for that Run's ``run.finished`` Signal (#18), typically
    ``"unmatched"`` when no cascade Rule applies.

    ``now`` is an injected clock (default: real UTC time) so the
    concurrency/dedupe/lifecycle checks above are deterministically testable.
    """
    board_dir = Path(board_dir)
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()

    if signal["type"] == "run.finished":
        depth = signal["payload"].get("cascade_depth", 0)
        if depth >= MAX_CASCADE_DEPTH:
            _record_unmatched(board_dir, signal,
                               f"cascade depth {depth} is at the maximum of {MAX_CASCADE_DEPTH}")
            return {"status": "cascade_depth_exceeded", "signal_id": signal["id"], "depth": depth}

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
    caused_by_run_id = signal["payload"]["run_id"] if signal["type"] == "run.finished" else None
    child_cascade_depth = signal["payload"].get("cascade_depth", 0) + 1 if signal["type"] == "run.finished" else 0
    stale_after = timeout + _HEALTH_TIMEOUT + _STALE_CLAIM_BUFFER
    if not try_claim_concurrency(board_dir, assistant, subject, now=now_ts, stale_after=stale_after):
        return {"status": "concurrency_skipped", "signal_id": signal["id"], "rule": rule["name"]}

    response = None
    try:
        started = ensure_started(board_dir, rule["connection"], connection,
                                  now=now_ts, health_timeout=_HEALTH_TIMEOUT)
        if started["status"] == "start_failed":
            response = {"status": "deployment_unavailable", "signal_id": signal["id"],
                        "rule": rule["name"], "error": started["error"]}
        elif not claim_signal(board_dir, signal["id"], now=now_ts):
            response = {"status": "duplicate", "signal_id": signal["id"], "rule": rule["name"]}
        else:
            try:
                result = execute(connection["endpoint"], assistant, subject, graph_input, timeout=timeout,
                                  caused_by_run_id=caused_by_run_id, cascade_depth=child_cascade_depth)
            except (ValueError, RuntimeError) as exc:
                # aegra_client.execute's exceptions are already payload/credential-free.
                response = {"status": "execution_failed", "signal_id": signal["id"],
                            "rule": rule["name"], "error": str(exc)}
            else:
                # Anchor this submission as its Thread's first (#46 AC2), the
                # same as cmd_run -- see cord_runtime.cli.cmd_run's comment.
                if result.get("thread_id") and result.get("run_id"):
                    record_submission(board_dir, rule["connection"], result["thread_id"], result["run_id"])
                response = {"status": "routed", "signal_id": signal["id"], "rule": rule["name"], "result": result}
    finally:
        release_active(board_dir, rule["connection"], connection, now=now_ts)
        release_concurrency(board_dir, assistant, subject)

    # Chain (#18) only after this Run's own claims are released, so a cascade
    # Run never contends with its own parent's (Assistant, Subject) claim.
    if response["status"] == "routed" and response["result"].get("status") == "success" \
            and response["result"].get("run_id"):
        finished = run_finished_signal(run_id=response["result"]["run_id"], subject=subject,
                                        connection=rule["connection"], assistant=assistant,
                                        status=response["result"]["status"], cascade_depth=child_cascade_depth)
        response["cascade"] = route_signal(board_dir, finished, timeout=timeout, now=now)
    return response
