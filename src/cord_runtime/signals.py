"""Signal construction for the three declared sources (#16): manual, file, schedule.

A Signal is an external event (ADR-0002); the platform never interprets its
meaning, only the fields a Rule declares (see ``router.py``). Signal identity
is deterministic per source so a later retention/dedup layer (#17) can be
added without changing this contract: the same file content or the same
schedule tick yields the same id.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

SIGNAL_TYPES = ("manual", "file", "schedule")


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
