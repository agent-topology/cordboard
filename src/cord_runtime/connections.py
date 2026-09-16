"""Board-level connection records for existing deployments (ADR-0014 §4).

Cordboard does not describe a graph; a connection record names only where a
Deployment lives. Storage is a single JSON object keyed by alias, written
atomically, with no credentials or graph descriptors.
"""

import contextlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

CONNECTIONS_DIRNAME = ".cordboard"
CONNECTIONS_FILENAME = "connections.json"


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


def add_connection(board_dir: Path, alias: str, endpoint: str, *, replace: bool = False) -> None:
    """Register or replace a Deployment alias. Raises InvalidConnection on invalid input."""
    alias = _validate_alias(alias)
    endpoint = validate_endpoint(endpoint)
    data = load_connections(board_dir)
    if alias in data and not replace:
        raise InvalidConnection(f"alias '{alias}' already exists; pass --replace to overwrite it")
    data[alias] = {"endpoint": endpoint}
    _write_atomic(connections_path(board_dir), data)
