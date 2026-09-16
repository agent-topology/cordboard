"""`cord`: connect and run existing deployments (#12).

Exit statuses: 0 success, 2 invalid usage/input, 1 connection/execution
failure (including a "waiting" Run: a non-success status that still carries
its identities). See docs/cord-cli.md for the full command reference.
"""

import argparse
import json
from pathlib import Path
import sys

import requests

from cord_runtime.aegra_client import execute
from cord_runtime.connections import InvalidConnection, add_connection, load_connections

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

_PROBE_TIMEOUT = 5.0


def _board_dir(args: argparse.Namespace) -> Path:
    return Path(args.board) if args.board else Path.cwd()


def _fail(message: str, code: int) -> int:
    print(f"cord: {message}", file=sys.stderr)
    return code


def _read_json_file(path: Path, label: str):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidConnection(f"cannot read {label} file '{path}': {exc.strerror}") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise InvalidConnection(f"{label} file '{path}' is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidConnection(f"{label} file '{path}' must contain a JSON object")
    return value


def cmd_add(args: argparse.Namespace) -> int:
    try:
        add_connection(_board_dir(args), args.alias, args.endpoint, replace=args.replace)
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    print(f"added '{args.alias}' -> {args.endpoint}")
    return EXIT_OK


def _probe(endpoint: str) -> dict:
    """Reachability and exposed graphs, without raising. No credentials sent."""
    try:
        reachable = requests.get(endpoint + "/health", timeout=_PROBE_TIMEOUT).status_code == 200
    except requests.RequestException:
        reachable = False
    graphs: list[str] = []
    if reachable:
        try:
            response = requests.get(endpoint + "/assistants", timeout=_PROBE_TIMEOUT)
            if response.status_code == 200:
                graphs = sorted({a["graph_id"] for a in response.json().get("assistants", [])})
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass
    return {"reachable": reachable, "graphs": graphs}


def cmd_list(args: argparse.Namespace) -> int:
    try:
        data = load_connections(_board_dir(args))
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    if args.alias is not None:
        if args.alias not in data:
            return _fail(f"unknown alias '{args.alias}'", EXIT_USAGE)
        data = {args.alias: data[args.alias]}
    if not data:
        print("no connections registered")
        return EXIT_OK
    all_reachable = True
    for alias in sorted(data):
        endpoint = data[alias]["endpoint"]
        status = _probe(endpoint)
        all_reachable = all_reachable and status["reachable"]
        reachability = "reachable" if status["reachable"] else "unreachable"
        graphs = ", ".join(status["graphs"]) if status["graphs"] else "-"
        print(f"{alias}\t{endpoint}\t{reachability}\tgraphs={graphs}")
    return EXIT_OK if all_reachable else EXIT_FAILURE


def cmd_run(args: argparse.Namespace) -> int:
    try:
        connection = load_connections(_board_dir(args))
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    if args.alias not in connection:
        return _fail(f"unknown alias '{args.alias}'", EXIT_USAGE)
    endpoint = connection[args.alias]["endpoint"]

    if not isinstance(args.subject, str) or not args.subject.strip():
        return _fail("Subject must be a non-empty string", EXIT_USAGE)

    try:
        graph_input = _read_json_file(args.input, "input")
        request_context = _read_json_file(args.context, "context") if args.context is not None else None
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)

    try:
        result = execute(endpoint, args.assistant, args.subject, graph_input,
                         request_context=request_context, timeout=args.timeout)
    except ValueError as exc:
        return _fail(str(exc), EXIT_USAGE)
    except RuntimeError as exc:
        return _fail(str(exc), EXIT_FAILURE)

    print(json.dumps(result))
    return EXIT_OK if result["status"] == "success" else EXIT_FAILURE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cord", description=__doc__)
    parser.add_argument("--board", help="Board directory holding .cordboard/ (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Register an existing deployment")
    add.add_argument("alias")
    add.add_argument("endpoint", help="Deployment base URL (http/https, no userinfo)")
    add.add_argument("--replace", action="store_true", help="Replace an existing alias")
    add.set_defaults(func=cmd_add)

    list_cmd = sub.add_parser("list", help="Show reachability and graphs for registered deployments")
    list_cmd.add_argument("alias", nargs="?", default=None)
    list_cmd.set_defaults(func=cmd_list)

    run = sub.add_parser("run", help="Invoke a graph on a registered deployment")
    run.add_argument("alias")
    run.add_argument("assistant", help="Explicit assistant id or graph id")
    run.add_argument("subject", help="Required opaque Subject string")
    run.add_argument("input", type=Path, help="JSON file with graph input")
    run.add_argument("--context", type=Path, default=None,
                     help="Optional JSON file with public request context")
    run.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for completion")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
