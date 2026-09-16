"""Signal construction for the three caller-declared sources (#16): manual,
file, schedule -- plus the platform-emitted ``run.finished`` source (#18).

A Signal is an external event (ADR-0002); the platform never interprets its
meaning, only the fields a Rule declares (see ``router.py``). Signal identity
is deterministic per source so the retention/dedup layer (#17) keys its
durable claim on it without changing this contract: the same file content,
the same schedule tick, or the same completed Run yields the same id.

``run.finished`` is not caller-supplied like the other three: ``router.py``
constructs it itself from a Run's own logical terminal state (`execute()`/
`resume()` settling as ``"success"``), never from interpreted payload content.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

SIGNAL_TYPES = ("manual", "file", "schedule", "run.finished")


class InvalidSignal(ValueError):
    pass


def _digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def manual_signal(payload: dict, *, signal_id: str | None = None, now: datetime | None = None) -> dict:
    """A caller-initiated Signal. See ADR-0003's ``manual:<timestamp>`` Subject convention."""
    if not isinstance(payload, dict):
        raise InvalidSignal("manual signal payload must be a JSON object")
    if signal_id is None:
        signal_id = f"manual:{(now or datetime.now(timezone.utc)).isoformat()}"
    elif not signal_id.strip():
        raise InvalidSignal("manual signal id must be a non-empty string")
    return {"type": "manual", "id": signal_id, "payload": payload}


def file_signal(path: Path) -> dict:
    """A Signal for one file's current content. Id is a content hash, not a timestamp."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidSignal(f"cannot read file signal '{path}': {exc.strerror}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise InvalidSignal(f"file signal '{path}' is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidSignal(f"file signal '{path}' must contain a JSON object")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {"type": "file", "id": f"file:{path}:{content_hash}", "payload": payload}


def schedule_signal(name: str, at: datetime, payload: dict | None = None) -> dict:
    """A Signal for one schedule tick at an explicit, caller-supplied time."""
    if not isinstance(name, str) or not name.strip():
        raise InvalidSignal("schedule signal name must be a non-empty string")
    if payload is not None and not isinstance(payload, dict):
        raise InvalidSignal("schedule signal payload must be a JSON object")
    at_iso = at.isoformat()
    merged = {"schedule": name, "at": at_iso, **(payload or {})}
    return {"type": "schedule", "id": f"schedule:{name}:{at_iso}", "payload": merged}


def run_finished_signal(*, run_id: str, subject: str, connection: str, assistant: str,
                         status: str, cascade_depth: int = 0) -> dict:
    """A Signal for one Run's logical terminal state (#18).

    Id is deterministic on ``run_id`` alone, so a redelivered or re-derived
    completion for the same Run shares #17's durable dedupe claim: a cascade
    never fires twice for one completed Run. ``connection``/``assistant``
    identify the Run that just finished (the source, not the target), which
    is what a Rule's self-loop guard compares its own target against.
    """
    for label, value in (("run_id", run_id), ("subject", subject), ("connection", connection),
                          ("assistant", assistant), ("status", status)):
        if not isinstance(value, str) or not value.strip():
            raise InvalidSignal(f"run.finished signal '{label}' must be a non-empty string")
    if not isinstance(cascade_depth, int) or isinstance(cascade_depth, bool) or cascade_depth < 0:
        raise InvalidSignal("run.finished signal 'cascade_depth' must be a non-negative integer")
    payload = {"run_id": run_id, "subject": subject, "connection": connection,
               "assistant": assistant, "status": status, "cascade_depth": cascade_depth}
    return {"type": "run.finished", "id": f"run.finished:{run_id}", "payload": payload}
