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

A record also optionally carries ``auth_endpoint`` (#49): an entity-owned HTTP
port Cordboard asks for approval-response authorization decisions
(`cord_runtime.entity_auth.HttpEntityAuthBoundary`). This describes the
connection, not the graph (ADR-0014) -- it is never inferred from ``endpoint``
and never implies an implicit-allow boundary when absent.

A record also optionally carries ``graph_map`` (#43): an explicit
``{document_graph_id: cord_graph_id}`` association from a topology document's
local ``graphs[].id`` (never itself an execution identity, ADR-0014) to the
recorded ``cord.graph.id``, set only via `set_graph_map` -- never inferred by
name. A record with no ``graph_map`` key is read as an empty mapping: no
associations, not an error, so every connection record predating this field
still loads unchanged.
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
                    auth_endpoint: str | None = None, replace: bool = False) -> None:
    """Register or replace a Deployment alias. Raises InvalidConnection on invalid input.

    ``launch`` is an optional operator-supplied command that starts this
    Deployment's own existing entrypoint; its presence opts the Deployment
    into managed startup/idle shutdown (#17). ``idle_after`` only applies to
    a managed Deployment and is ignored (not stored) otherwise. ``auth_endpoint``
    is an optional entity-owned HTTP port for approval-response authorization
    (#49); see the module docstring.
    """
    alias = _validate_alias(alias)
    endpoint = validate_endpoint(endpoint)
    launch = _validate_launch(launch)
    auth_endpoint = validate_endpoint(auth_endpoint) if auth_endpoint is not None else None
    data = load_connections(board_dir)
    if alias in data and not replace:
        raise InvalidConnection(f"alias '{alias}' already exists; pass --replace to overwrite it")
    record = {"endpoint": endpoint}
    if launch is not None:
        record["launch"] = launch
        record["idle_after"] = _validate_idle_after(idle_after)
    if auth_endpoint is not None:
        record["auth_endpoint"] = auth_endpoint
    data[alias] = record
    _write_atomic(connections_path(board_dir), data)


def set_auth_endpoint(board_dir: Path, alias: str, auth_endpoint: str) -> None:
    """Set or replace an already-registered alias's ``auth_endpoint`` (#49),
    the same way `set_graph_map` adds its own optional field after the fact.
    """
    auth_endpoint = validate_endpoint(auth_endpoint)
    data = load_connections(board_dir)
    if alias not in data:
        raise InvalidConnection(f"unknown alias '{alias}'")
    record = dict(data[alias])
    record["auth_endpoint"] = auth_endpoint
    data[alias] = record
    _write_atomic(connections_path(board_dir), data)


def _validate_graph_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidConnection(f"{label} must be a non-empty string")
    return value


def set_graph_map(board_dir: Path, alias: str, document_graph_id: str, graph_id: str) -> None:
    """Store one explicit association from a topology document's local
    ``graphs[].id`` (``document_graph_id``) to the recorded ``cord.graph.id``
    (``graph_id``) for an already-registered alias (#43).

    Never inferred by name: this is the only way a connection record gains a
    ``graph_map`` entry. Setting the same ``document_graph_id`` again replaces
    its prior association; other entries are left unchanged.
    """
    document_graph_id = _validate_graph_id(document_graph_id, "document graph id")
    graph_id = _validate_graph_id(graph_id, "graph id")
    data = load_connections(board_dir)
    if alias not in data:
        raise InvalidConnection(f"unknown alias '{alias}'")
    record = dict(data[alias])
    graph_map = dict(record.get("graph_map") or {})
    graph_map[document_graph_id] = graph_id
    record["graph_map"] = graph_map
    data[alias] = record
    _write_atomic(connections_path(board_dir), data)
