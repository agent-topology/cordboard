"""Explicit opt-in managed Deployment startup and idle shutdown (#17).

A connection record with no ``launch`` command (`cord_runtime.connections`)
is external: this module never starts or stops it, only ever contacts it,
same as every other slice. A record that declares ``launch`` opts into
managed lifecycle -- Cordboard may start that Deployment's own existing
entrypoint (an operator-supplied command reusing the entity's Aegra process,
dependency environment, and health check) and later stop it once idle. This
module does not recreate that entrypoint's graph factories, auth, Postgres
management, or redaction provider; it only runs the command it was given and
polls the same public ``/health`` every other slice already probes.

``ensure_started`` is the shared entry point both an execution submission and
a future approval-resume submission are meant to call before reaching the
Deployment (ARCHITECTURE.md "Isolation, routing, and approval": "Lazy startup
is planned with routing"). Only the execution path in `cord_runtime.router`
wires it in this slice -- the approval-resume orchestration that would also
call it does not exist yet in this codebase.

Storage tracks state per Deployment alias, not per Graph: a Deployment may
host multiple Graphs (ADR-0001's shared-group correction), so ``active_runs``
is a single counter per alias and a Graph going idle only drops that counter
-- it never stops a Deployment while any other Graph it hosts still holds a
claim. A startup or health failure is recorded durably (``status`` and
``last_error``) rather than raised, so the caller can report it and leave the
triggering Signal pending for a retry instead of a false success.

Storage follows `cord_runtime.connections`'s atomic single-JSON-file pattern.
``launch``/``health_check``/``stop`` are injected so tests exercise this
module against a tiny test-owned process rather than a real deployment.
"""

import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import requests

from cord_runtime.connections import DEFAULT_IDLE_AFTER

LIFECYCLE_DIRNAME = ".cordboard"
LIFECYCLE_FILENAME = "deployment_lifecycle.json"

EXTERNAL = "external"
RUNNING = "running"
STOPPED = "stopped"
START_FAILED = "start_failed"

_HEALTH_POLL_INTERVAL = 0.2


class DeploymentLifecycleError(ValueError):
    """Invalid input or storage for the managed Deployment lifecycle store."""


def lifecycle_path(board_dir: Path) -> Path:
    return Path(board_dir) / LIFECYCLE_DIRNAME / LIFECYCLE_FILENAME


def load_lifecycle(board_dir: Path) -> dict:
    path = lifecycle_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeploymentLifecycleError(f"deployment lifecycle file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise DeploymentLifecycleError(f"deployment lifecycle file must be a JSON object: {path}")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".deployment-lifecycle-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _default_launch(alias: str, launch: list[str]) -> int:
    # `launch` is the operator's own command from their connection record, not
    # derived from Signal payload content.
    process = subprocess.Popen(launch, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process.pid


def _default_health_check(endpoint: str, *, health_timeout: float) -> bool:
    deadline = time.monotonic() + health_timeout
    while True:
        try:
            if requests.get(endpoint + "/health", timeout=_HEALTH_POLL_INTERVAL).status_code == 200:
                return True
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(_HEALTH_POLL_INTERVAL)


_LIVENESS_PROBE_TIMEOUT = 1.0


def _default_is_alive(endpoint: str) -> bool:
    # One bounded, non-looping probe -- distinct from `_default_health_check`,
    # which polls up to `health_timeout` for a *freshly launched* process to
    # become healthy. Here the record already claims RUNNING; this only
    # confirms that claim is still true right now (#75).
    try:
        return requests.get(endpoint + "/health", timeout=_LIVENESS_PROBE_TIMEOUT).status_code == 200
    except requests.RequestException:
        return False


def _default_stop(alias: str, record: dict) -> None:
    pid = record.get("pid")
    if pid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 15)  # SIGTERM: ask the operator's own entrypoint to shut down cleanly


def ensure_started(board_dir: Path, alias: str, connection: dict, *, now: float,
                    launch=None, health_check=None, health_timeout: float = 30.0, is_alive=None) -> dict:
    """Start ``alias`` if it declares a ``launch`` command and is not already
    tracked as running and reachable; mark it active either way. Returns
    ``{"status": "external"}`` for an unmanaged connection (never started or
    stopped here), ``{"status": "running", ...}`` once started (or already
    running) with the active-claim count bumped, or
    ``{"status": "start_failed", "error": ...}`` when the launch or health
    check did not succeed -- the caller must not submit a Run in that case.

    A stored ``RUNNING`` record is re-verified with ``is_alive`` before it is
    trusted: the tracked process can have crashed outside Cordboard's control
    (killed, OOM, host restart) between one call and the next, and a stale
    record would otherwise wedge the Deployment as unusable until an operator
    manually edits `deployment_lifecycle.json` or waits out `idle_after` and
    runs `cord deployment sweep` -- exactly the hidden state repair this
    recovery loop must not require (#75). A record that fails this recheck
    falls through to a fresh launch attempt below, the same as one that was
    never started.
    """
    launch_command = connection.get("launch")
    if not launch_command:
        return {"status": EXTERNAL}

    launch = launch or _default_launch
    health_check = health_check or (
        lambda endpoint: _default_health_check(endpoint, health_timeout=health_timeout)
    )
    is_alive = is_alive or _default_is_alive

    data = load_lifecycle(board_dir)
    record = data.get(alias)
    if record is not None and record["status"] == RUNNING and is_alive(connection["endpoint"]):
        record["active_runs"] += 1
        record["last_active"] = now
        _write_atomic(lifecycle_path(board_dir), data)
        return {"status": RUNNING, "pid": record.get("pid")}

    try:
        pid = launch(alias, launch_command)
    except OSError as exc:
        record = {"status": START_FAILED, "pid": None, "active_runs": 0,
                   "last_active": now, "last_error": str(exc)}
        data[alias] = record
        _write_atomic(lifecycle_path(board_dir), data)
        return {"status": START_FAILED, "error": str(exc)}

    if not health_check(connection["endpoint"]):
        error = f"Deployment '{alias}' did not become healthy within {health_timeout}s of launch"
        record = {"status": START_FAILED, "pid": pid, "active_runs": 0,
                   "last_active": now, "last_error": error}
        data[alias] = record
        _write_atomic(lifecycle_path(board_dir), data)
        return {"status": START_FAILED, "error": error}

    record = {"status": RUNNING, "pid": pid, "active_runs": 1,
              "started_at": now, "last_active": now, "last_error": None}
    data[alias] = record
    _write_atomic(lifecycle_path(board_dir), data)
    return {"status": RUNNING, "pid": pid}


def release_active(board_dir: Path, alias: str, connection: dict, *, now: float) -> None:
    """Drop one active claim on ``alias``. A no-op for an external connection
    or an alias this module never started.
    """
    if not connection.get("launch"):
        return
    data = load_lifecycle(board_dir)
    record = data.get(alias)
    if record is None or record["status"] != RUNNING:
        return
    record["active_runs"] = max(0, record["active_runs"] - 1)
    record["last_active"] = now
    _write_atomic(lifecycle_path(board_dir), data)


def stop_idle(board_dir: Path, connections: dict, *, now: float, stop=None) -> list[str]:
    """Stop every managed, running Deployment that is idle beyond its
    declared ``idle_after`` and currently has no active claims. Returns the
    stopped aliases. External connections, connections with active claims,
    and connections not present in ``connections`` are left untouched --
    including any sibling Graph on the same Deployment that is still active,
    since ``active_runs`` is tracked per Deployment alias.
    """
    stop = stop or _default_stop
    data = load_lifecycle(board_dir)
    stopped = []
    for alias, record in data.items():
        connection = connections.get(alias)
        if connection is None or not connection.get("launch"):
            continue
        if record["status"] != RUNNING or record["active_runs"] > 0:
            continue
        idle_after = connection.get("idle_after", DEFAULT_IDLE_AFTER)
        if now - record["last_active"] < idle_after:
            continue
        stop(alias, record)
        record["status"] = STOPPED
        record["pid"] = None
        stopped.append(alias)
    if stopped:
        _write_atomic(lifecycle_path(board_dir), data)
    return stopped
