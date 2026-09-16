"""`cord`: connect and run existing deployments (#12).

Exit statuses: 0 success, 2 invalid usage/input, 1 connection/execution
failure (including a "waiting" Run: a non-success status that still carries
its identities). See docs/cord-cli.md for the full command reference.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

import requests

from cord_runtime.aegra_client import execute
from cord_runtime.archive_query import ArchiveError, read_spans
from cord_runtime.connections import InvalidConnection, add_connection, load_connections
from cord_runtime.router import route_signal
from cord_runtime.rules import InvalidRule, SIGNAL_TYPES, add_rule
from cord_runtime.signals import InvalidSignal, file_signal, manual_signal, schedule_signal
from cord_runtime.topology import check_freshness
from cord_runtime.viewer import build_catalog, format_catalog_text

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


def cmd_view(args: argparse.Namespace) -> int:
    board_dir = _board_dir(args)
    try:
        connections = load_connections(board_dir)
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    if args.alias is not None:
        if args.alias not in connections:
            return _fail(f"unknown alias '{args.alias}'", EXIT_USAGE)
        connections = {args.alias: connections[args.alias]}

    probed = {}
    topology = {}
    for alias, info in connections.items():
        endpoint = info["endpoint"]
        status = _probe(endpoint)
        probed[alias] = {"endpoint": endpoint, "reachable": status["reachable"], "graphs": status["graphs"]}
        topology[endpoint] = check_freshness(board_dir, endpoint, timeout=_PROBE_TIMEOUT)

    try:
        spans = read_spans(args.archive) if args.archive else {}
        catalog = build_catalog(probed, topology, spans)
    except ArchiveError as exc:
        return _fail(str(exc), EXIT_USAGE)

    print(json.dumps(catalog) if args.json else format_catalog_text(catalog))
    return EXIT_OK


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


def _parse_match_kv(pairs: list[str]) -> dict:
    match = {}
    for item in pairs:
        if "=" not in item:
            raise InvalidRule(f"expected KEY=VALUE for --match, got '{item}'")
        key, _, raw = item.partition("=")
        try:
            match[key] = json.loads(raw)
        except ValueError:
            match[key] = raw
    return match


def _parse_input_kv(pairs: list[str]) -> dict:
    mapping = {}
    for item in pairs:
        if "=" not in item:
            raise InvalidRule(f"expected DEST=PATH for --input, got '{item}'")
        key, _, path = item.partition("=")
        mapping[key] = path
    return mapping


def cmd_rule_add(args: argparse.Namespace) -> int:
    try:
        rule = {
            "name": args.name,
            "signal_type": args.signal_type,
            "connection": args.connection,
            "assistant": args.assistant,
            "subject": args.subject,
            "match": _parse_match_kv(args.match),
            "input": _parse_input_kv(args.input),
        }
        add_rule(_board_dir(args), rule, replace=args.replace)
    except InvalidRule as exc:
        return _fail(str(exc), EXIT_USAGE)
    print(f"added rule '{args.name}'")
    return EXIT_OK


def _route_and_report(board_dir: Path, signal: dict, timeout: float) -> int:
    outcome = route_signal(board_dir, signal, timeout=timeout)
    print(json.dumps(outcome))
    if outcome["status"] != "routed":
        return EXIT_FAILURE
    return EXIT_OK if outcome["result"]["status"] == "success" else EXIT_FAILURE


def cmd_signal_manual(args: argparse.Namespace) -> int:
    try:
        payload = _read_json_file(args.input, "signal input")
        signal = manual_signal(payload, signal_id=args.id)
    except (InvalidConnection, InvalidSignal) as exc:
        return _fail(str(exc), EXIT_USAGE)
    return _route_and_report(_board_dir(args), signal, args.timeout)


def cmd_signal_file(args: argparse.Namespace) -> int:
    try:
        signal = file_signal(args.path)
    except InvalidSignal as exc:
        return _fail(str(exc), EXIT_USAGE)
    return _route_and_report(_board_dir(args), signal, args.timeout)


def cmd_signal_schedule(args: argparse.Namespace) -> int:
    try:
        at = datetime.fromisoformat(args.at)
    except ValueError:
        return _fail(f"invalid --at timestamp '{args.at}'", EXIT_USAGE)
    try:
        signal = schedule_signal(args.name, at)
    except InvalidSignal as exc:
        return _fail(str(exc), EXIT_USAGE)
    return _route_and_report(_board_dir(args), signal, args.timeout)


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

    view = sub.add_parser("view", help="Show the catalog: Graphs, topology, and recorded execution")
    view.add_argument("alias", nargs="?", default=None)
    view.add_argument("--archive", type=Path, action="append", default=[],
                      help="Archive directory or *.otlp.jsonl[.gz] file to read recorded execution from (repeatable)")
    view.add_argument("--json", action="store_true", help="Print the catalog as one JSON object instead of text")
    view.set_defaults(func=cmd_view)

    run = sub.add_parser("run", help="Invoke a graph on a registered deployment")
    run.add_argument("alias")
    run.add_argument("assistant", help="Explicit assistant id or graph id")
    run.add_argument("subject", help="Required opaque Subject string")
    run.add_argument("input", type=Path, help="JSON file with graph input")
    run.add_argument("--context", type=Path, default=None,
                     help="Optional JSON file with public request context")
    run.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for completion")
    run.set_defaults(func=cmd_run)

    rule = sub.add_parser("rule", help="Manage declarative Signal routing rules")
    rule_sub = rule.add_subparsers(dest="rule_command", required=True)

    rule_add = rule_sub.add_parser("add", help="Add a Rule mapping a Signal type to an Assistant")
    rule_add.add_argument("name")
    rule_add.add_argument("signal_type", choices=SIGNAL_TYPES)
    rule_add.add_argument("connection", help="Registered connection alias")
    rule_add.add_argument("assistant", help="Explicit assistant id or graph id")
    rule_add.add_argument("subject", help="Dot-path into the Signal payload for the required Subject")
    rule_add.add_argument("--match", action="append", default=[],
                          help="KEY=VALUE equality filter on the Signal payload (repeatable)")
    rule_add.add_argument("--input", action="append", default=[],
                          help="DEST=PATH mapping into the Assistant input (repeatable)")
    rule_add.add_argument("--replace", action="store_true", help="Replace an existing rule of the same name")
    rule_add.set_defaults(func=cmd_rule_add)

    signal = sub.add_parser("signal", help="Fire a Signal through the declared Rule routing path")
    signal_sub = signal.add_subparsers(dest="signal_command", required=True)

    signal_manual = signal_sub.add_parser("manual", help="Fire a manual Signal")
    signal_manual.add_argument("input", type=Path, help="JSON file with the Signal payload")
    signal_manual.add_argument("--id", default=None, help="Explicit Signal id (default: manual:<timestamp>)")
    signal_manual.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for completion")
    signal_manual.set_defaults(func=cmd_signal_manual)

    signal_file = signal_sub.add_parser("file", help="Fire a Signal from one file's current JSON content")
    signal_file.add_argument("path", type=Path)
    signal_file.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for completion")
    signal_file.set_defaults(func=cmd_signal_file)

    signal_schedule = signal_sub.add_parser("schedule", help="Fire a Signal for one schedule tick")
    signal_schedule.add_argument("name")
    signal_schedule.add_argument("at", help="ISO 8601 timestamp for this schedule tick")
    signal_schedule.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for completion")
    signal_schedule.set_defaults(func=cmd_signal_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
