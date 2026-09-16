"""Durable mapping from Aegra's per-submission API Run IDs to one logical Run.

Aegra 0.10.4 issues a new API Run ID for every submission on a Thread,
including a ``command.resume`` continuing an interrupted one (ADR-0003
correction). This module records, per (Deployment, Thread), which API Run ID
was first and groups every later submission on that Thread under it, so a
resume is never mistaken for an unrelated execution. It is not an approval
inbox, an authorization boundary, or a dedupe policy for concurrent
submissions — those remain out of scope until #14's blockers are resolved.
Storage follows `cord_runtime.connections`'s atomic single-JSON-file pattern.
"""

import contextlib
import json
import os
from pathlib import Path
import tempfile

CONTINUITY_DIRNAME = ".cordboard"
CONTINUITY_FILENAME = "run_continuity.json"


class RunContinuityError(ValueError):
    """Invalid input or storage for the API Run ID -> logical Run mapping."""


class UnknownThread(RunContinuityError):
    """No API Run ID has been recorded yet for this Deployment/Thread pair."""


def continuity_path(board_dir: Path) -> Path:
    return Path(board_dir) / CONTINUITY_DIRNAME / CONTINUITY_FILENAME


def load_run_continuity(board_dir: Path) -> dict:
    path = continuity_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunContinuityError(f"run continuity file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RunContinuityError(f"run continuity file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".run-continuity-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _validate(deployment: str, thread_id: str, api_run_id: str) -> None:
    for name, value in (("deployment alias", deployment), ("thread ID", thread_id),
                        ("API Run ID", api_run_id)):
        if not isinstance(value, str) or not value.strip():
            raise RunContinuityError(f"{name} must be a non-empty string")


def record_submission(board_dir: Path, deployment: str, thread_id: str, api_run_id: str) -> str:
    """Record one Aegra API Run ID against its Thread; return the logical Run ID it belongs to.

    The first API Run ID recorded for a (Deployment, Thread) pair becomes that
    pair's logical Run ID (matching the existing independent-execution mapping
    in ARCHITECTURE.md); every later submission on the same Thread — a resume —
    is appended under it instead of starting a new logical Run. Recording the
    same API Run ID twice is idempotent.
    """
    _validate(deployment, thread_id, api_run_id)
    data = load_run_continuity(board_dir)
    threads = data.setdefault(deployment, {})
    record = threads.get(thread_id)
    if record is None:
        record = {"run_id": api_run_id, "api_run_ids": [api_run_id]}
        threads[thread_id] = record
    elif api_run_id not in record["api_run_ids"]:
        record["api_run_ids"].append(api_run_id)
    else:
        return record["run_id"]
    _write_atomic(continuity_path(board_dir), data)
    return record["run_id"]


def logical_run(board_dir: Path, deployment: str, thread_id: str) -> dict:
    """Return ``{"run_id": ..., "api_run_ids": [...]}`` for a known Thread.

    Raises ``UnknownThread`` when no submission has been recorded for this
    (Deployment, Thread) pair.
    """
    data = load_run_continuity(board_dir)
    try:
        return data[deployment][thread_id]
    except KeyError:
        raise UnknownThread(f"no recorded submission for {deployment}/{thread_id}") from None
