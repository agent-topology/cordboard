"""Board-level connection records for existing deployments (ADR-0014 §4).

Cordboard does not describe a graph; a connection record names only where a
Deployment lives. Storage is a single JSON object keyed by alias, written
atomically, with no credentials or graph descriptors.

A record optionally carries ``launch`` (#17): an operator-supplied command
that starts this Deployment's own existing entrypoint (e.g. its Aegra
process) and an ``idle_after`` seconds threshold. Presence of ``launch`` is
what makes a Deployment "managed" -- `cord_runtime.deployment_lifecycle` may
start and stop it. Its absence makes the Deployment "external": Cordboard
only ever contacts it, per the connection-level opt-in ADR-0013/0014
describe. No launch command implies no recreation of the entity's process
factories, auth, or database management -- only reuse of what the operator
already runs.
"""

import contextlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

CONNECTIONS_DIRNAME = ".cordboard"
CONNECTIONS_FILENAME = "connections.json"

DEFAULT_IDLE_AFTER = 600.0  # 10 minutes (#17, preserved per issue #17's proposed default)


def is_managed(connection: dict) -> bool:
    """A Deployment is managed only when its record declares a launch command."""
    return bool(connection.get("launch"))


class InvalidConnection(ValueError):
    """Invalid connection input or storage; distinct from a reachability failure."""


def connections_path(board_dir: Path) -> Path:
    return Path(board_dir) / CONNECTIONS_DIRNAME / CONNECTIONS_FILENAME


def load_connections(board_dir: Path) -> dict:
    path = connections_path(board_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidConnection(f"connections file is not valid JSON: {path}") from exc
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise InvalidConnection(f"connections file must be a JSON object of alias -> record: {path}")
    return data


def get_connection(board_dir: Path, alias: str) -> dict:
    data = load_connections(board_dir)
    if alias not in data:
        raise InvalidConnection(f"unknown alias '{alias}'")
    return data[alias]


def validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise InvalidConnection("endpoint must be a non-empty string")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise InvalidConnection("endpoint must be an http or https URL")
    if not parsed.hostname:
        raise InvalidConnection("endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidConnection("endpoint must not carry userinfo; Cordboard stores no credentials")
    return endpoint.rstrip("/")


def _validate_alias(alias: str) -> str:
    if not isinstance(alias, str) or not alias.strip():
        raise InvalidConnection("alias must be a non-empty string")
    return alias


def _validate_launch(launch: list[str] | None) -> list[str] | None:
    if launch is None:
        return None
    if not isinstance(launch, list) or not launch or not all(
        isinstance(part, str) and part.strip() for part in launch
    ):
        raise InvalidConnection("launch must be a non-empty list of non-empty strings")
    return list(launch)


def _validate_idle_after(idle_after: float | None) -> float:
    if idle_after is None:
        return DEFAULT_IDLE_AFTER
    if not isinstance(idle_after, (int, float)) or isinstance(idle_after, bool) or idle_after <= 0:
        raise InvalidConnection("idle_after must be a positive number of seconds")
    return float(idle_after)


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".connections-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def add_connection(board_dir: Path, alias: str, endpoint: str, *,
                    launch: list[str] | None = None, idle_after: float | None = None,
                    replace: bool = False) -> None:
    """Register or replace a Deployment alias. Raises InvalidConnection on invalid input.

    ``launch`` is an optional operator-supplied command that starts this
    Deployment's own existing entrypoint; its presence opts the Deployment
    into managed startup/idle shutdown (#17). ``idle_after`` only applies to
    a managed Deployment and is ignored (not stored) otherwise.
    """
    alias = _validate_alias(alias)
    endpoint = validate_endpoint(endpoint)
    launch = _validate_launch(launch)
    data = load_connections(board_dir)
    if alias in data and not replace:
        raise InvalidConnection(f"alias '{alias}' already exists; pass --replace to overwrite it")
    record = {"endpoint": endpoint}
    if launch is not None:
        record["launch"] = launch
        record["idle_after"] = _validate_idle_after(idle_after)
    data[alias] = record
    _write_atomic(connections_path(board_dir), data)
