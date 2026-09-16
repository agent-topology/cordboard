"""`cord`: connect and run existing deployments (#12).

Exit statuses: 0 success, 2 invalid usage/input, 1 connection/execution
failure (including a "waiting" Run: a non-success status that still carries
its identities). See docs/cord-cli.md for the full command reference.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import shlex
import sys
import threading
import time

import requests

from cord_runtime.aegra_client import execute
from cord_runtime.archive_query import ArchiveError, read_spans
from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.connections import InvalidConnection, add_connection, load_connections, set_graph_map
from cord_runtime.deployment_lifecycle import stop_idle
from cord_runtime.live_reconciliation import LiveRun, await_convergence, reconcile
from cord_runtime.router import route_signal
from cord_runtime.rules import InvalidRule, SIGNAL_TYPES, add_rule
from cord_runtime.run_continuity import UnknownThread, logical_run, record_submission
from cord_runtime.signals import InvalidSignal, file_signal, manual_signal, schedule_signal
from cord_runtime.topology import VALID, check_freshness, refresh_snapshot
from cord_runtime.viewer import build_catalog, build_execution_tree, format_catalog_text

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
    launch = shlex.split(args.launch) if args.launch else None
    try:
        add_connection(_board_dir(args), args.alias, args.endpoint,
                       launch=launch, idle_after=args.idle_after, replace=args.replace)
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    suffix = " (managed)" if launch else ""
    print(f"added '{args.alias}' -> {args.endpoint}{suffix}")
    return EXIT_OK


def cmd_deployment_sweep(args: argparse.Namespace) -> int:
    board_dir = _board_dir(args)
    try:
        connections = load_connections(board_dir)
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    stopped = stop_idle(board_dir, connections, now=datetime.now(timezone.utc).timestamp())
    print("stopped: " + ", ".join(sorted(stopped)) if stopped else "no idle managed deployments")
    return EXIT_OK


def cmd_graph_map(args: argparse.Namespace) -> int:
    try:
        set_graph_map(_board_dir(args), args.alias, args.document_graph_id, args.graph_id)
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    print(f"mapped '{args.alias}' document graph '{args.document_graph_id}' -> '{args.graph_id}'")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    board_dir = _board_dir(args)
    try:
        connections = load_connections(board_dir)
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    if args.alias is not None:
        if args.alias not in connections:
            return _fail(f"unknown alias '{args.alias}'", EXIT_USAGE)
        connections = {args.alias: connections[args.alias]}
    if not connections:
        print("no connections registered")
        return EXIT_OK
    all_valid = True
    for alias in sorted(connections):
        endpoint = connections[alias]["endpoint"]
        reading = refresh_snapshot(board_dir, endpoint, timeout=_PROBE_TIMEOUT)
        all_valid = all_valid and reading.status == VALID
        print(f"{alias}\t{endpoint}\t{reading.status}")
    return EXIT_OK if all_valid else EXIT_FAILURE


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


def _parse_watch_target(raw: str) -> tuple[str, str, str]:
    parts = raw.split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise InvalidConnection(f"--watch '{raw}' must be ALIAS:THREAD_ID:RUN_ID")
    alias, thread_id, run_id = parts
    return alias, thread_id, run_id


def _resolve_logical_run_id(board_dir: Path, alias: str, thread_id: str, run_id: str) -> str:
    """The identity a watched Run's `LiveRun` is keyed by (#46 AC2).

    `run_continuity`'s logical Run ID stays stable across a resume's fresh
    API Run ID and matches the same `cord.run.id` the archive records (the
    Run's own OTel span never reopens on resume -- see
    `cord_runtime.execution.resume_run`), so watching either the original or
    a resumed invocation's Run ID converges on one catalog entry instead of
    forking it. A Thread `run_continuity` has never recorded (e.g. a Run
    submitted outside `cord`) falls back to the watched Run ID itself rather
    than blocking or guessing (ADR-0015).
    """
    try:
        return logical_run(board_dir, alias, thread_id)["run_id"]
    except UnknownThread:
        return run_id


def _watch_worker(alias: str, endpoint: str, thread_id: str, run_id: str, *, timeout: float,
                   board_dir: Path, messages: "queue.Queue[tuple]") -> None:
    """Runs in its own thread (#46 AC1): resolves one --watch target's
    identity and streams its lifecycle events onto a shared queue instead of
    returning only after the whole stream drains, so N watched targets make
    progress concurrently -- none blocks behind another's completion -- and
    the consumer can render each update as it arrives rather than only a
    final snapshot.

    Depends on the ExecutionBackend boundary (#42) rather than a concrete
    Aegra transport function, so a future backend only needs to satisfy the
    same `describe_run`/`describe_assistant`/`watch` shape.
    """
    # Every path through this function ends in exactly one "done" (#46 AC1):
    # an early "failed" return must still signal completion, or the
    # consumer's `while remaining > 0` loop in cmd_view waits forever for a
    # thread that already exited.
    try:
        _watch_worker_body(alias, endpoint, thread_id, run_id, timeout=timeout,
                            board_dir=board_dir, messages=messages)
    finally:
        messages.put(("done", thread_id, run_id, None))


def _watch_worker_body(alias: str, endpoint: str, thread_id: str, run_id: str, *, timeout: float,
                        board_dir: Path, messages: "queue.Queue[tuple]") -> None:
    backend = AegraExecutionBackend(endpoint)
    try:
        identity = backend.describe_run(thread_id, run_id)
    except (RuntimeError, ValueError) as exc:
        messages.put(("failed", thread_id, run_id, f"cord: --watch {thread_id}/{run_id}: {exc}"))
        return
    try:
        graph_id = backend.describe_assistant(identity["assistant_id"])["graph_id"]
    except (RuntimeError, ValueError):
        graph_id = None
    if graph_id is None:
        messages.put(("failed", thread_id, run_id,
                       f"cord: --watch {thread_id}/{run_id}: could not resolve a Graph for assistant "
                       f"'{identity['assistant_id']}'; omitted from the catalog"))
        return

    logical_id = _resolve_logical_run_id(board_dir, alias, thread_id, run_id)
    messages.put(("identified", logical_id, thread_id,
                  {"graph_id": graph_id, "assistant_id": identity["assistant_id"], "subject": identity["subject"],
                   "status": identity["status"]}))
    try:
        for event, data, event_id in backend.watch(thread_id, run_id, timeout=timeout):
            messages.put(("event", logical_id, thread_id, (event, data, event_id)))
    except RuntimeError as exc:
        messages.put(("failed", thread_id, run_id, f"cord: --watch {thread_id}/{run_id}: {exc}"))


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

    try:
        watch_targets = [_parse_watch_target(raw) for raw in args.watch]
    except InvalidConnection as exc:
        return _fail(str(exc), EXIT_USAGE)
    for alias, _thread_id, _run_id in watch_targets:
        if alias not in connections:
            return _fail(f"unknown alias '{alias}' in --watch", EXIT_USAGE)

    probed = {}
    topology = {}
    for alias, info in connections.items():
        endpoint = info["endpoint"]
        status = _probe(endpoint)
        probed[alias] = {"endpoint": endpoint, "reachable": status["reachable"], "graphs": status["graphs"],
                          "graph_map": info.get("graph_map") or {}}
        topology[endpoint] = check_freshness(board_dir, endpoint, timeout=_PROBE_TIMEOUT)

    live_by_id: dict[str, LiveRun] = {}
    last_printed: list[str | None] = [None]

    def read_archive() -> dict:
        return read_spans(args.archive) if args.archive else {}

    def recorded_run_ids() -> set[str]:
        return {run["run_id"] for run in build_execution_tree(read_archive())}

    def render() -> None:
        """Re-read the archive and print the catalog if it changed since the
        last render (#46 AC1/AC4): every render reflects the archive as of
        right now, so a Run that got recorded mid-watch is dropped from
        `live_by_id`'s view and shown as recorded instead, in the same print
        that would otherwise have still called it live."""
        spans = read_archive()
        views = reconcile(live_by_id, {run["run_id"] for run in build_execution_tree(spans)},
                          now=time.monotonic())
        catalog = build_catalog(probed, topology, spans, live_runs=views)
        text = json.dumps(catalog) if args.json else format_catalog_text(catalog)
        if text != last_printed[0]:
            print(text)
            last_printed[0] = text

    try:
        if watch_targets:
            messages: "queue.Queue[tuple]" = queue.Queue()
            threads = [
                threading.Thread(
                    target=_watch_worker, args=(alias, connections[alias]["endpoint"], thread_id, run_id),
                    kwargs={"timeout": args.watch_timeout, "board_dir": board_dir, "messages": messages},
                    daemon=True)
                for alias, thread_id, run_id in watch_targets
            ]
            for t in threads:
                t.start()

            # One consumer loop applies every thread's events to shared state
            # and renders (#46 AC1): each --watch target's network I/O runs
            # concurrently on its own thread, so none blocks behind another's
            # completion, while state mutation/printing stays single-threaded.
            remaining = len(threads)
            while remaining > 0:
                kind, logical_id, thread_id, payload = messages.get()
                if kind == "identified":
                    live = LiveRun(logical_id, thread_id, graph_id=payload["graph_id"],
                                   assistant_id=payload["assistant_id"], subject=payload["subject"])
                    if payload["status"] in ("pending", "running", "interrupted"):
                        live.status = payload["status"]
                    live_by_id[logical_id] = live
                    render()
                elif kind == "event":
                    event, data, event_id = payload
                    live_by_id[logical_id].apply(event, data, event_id, now=time.monotonic())
                    render()
                elif kind == "failed":
                    print(payload, file=sys.stderr)
                elif kind == "done":
                    remaining -= 1
            for t in threads:
                t.join()

        if args.archive:
            # Bounded post-watch convergence (#46 AC3/AC4): keep re-reading
            # the archive until every completed watched Run is replaced by
            # its durable record or reaches the ingestion-failed deadline.
            # Skipped with no --archive: there is nothing to converge
            # against, and this is `cord view`'s existing single-shot shape.
            for _ in await_convergence(live_by_id, recorded_run_ids,
                                        now_fn=time.monotonic, sleep_fn=time.sleep):
                render()
        else:
            render()
    except ArchiveError as exc:
        return _fail(str(exc), EXIT_USAGE)

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

    if result.get("thread_id") and result.get("run_id"):
        # Anchor this submission as its Thread's first (#46 AC2): a later
        # resume (`approval_inbox.submit_response` -> `backend.resume`)
        # already calls `record_submission` on its own fresh API Run ID, but
        # only this original submission fixes *which* Run ID that resume
        # gets grouped under instead of becoming a logical Run of its own.
        record_submission(_board_dir(args), args.alias, result["thread_id"], result["run_id"])

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
            "input": _parse_input_kv(args.input),
        }
        if args.signal_type == "run.finished":
            rule["when"] = _parse_match_kv(args.when)
        else:
            rule["match"] = _parse_match_kv(args.match)
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
    add.add_argument("--launch", default=None,
                     help="Operator-supplied command that starts this Deployment's own entrypoint; "
                          "presence opts the Deployment into managed startup/idle shutdown (#17)")
    add.add_argument("--idle-after", type=float, default=None, dest="idle_after",
                     help="Seconds of no active claim before a managed Deployment is stopped (default: 600)")
    add.add_argument("--replace", action="store_true", help="Replace an existing alias")
    add.set_defaults(func=cmd_add)

    deployment = sub.add_parser("deployment", help="Manage managed Deployment lifecycle")
    deployment_sub = deployment.add_subparsers(dest="deployment_command", required=True)
    deployment_sweep = deployment_sub.add_parser(
        "sweep", help="Stop every managed Deployment idle beyond its declared idle_after")
    deployment_sweep.set_defaults(func=cmd_deployment_sweep)

    graph_map = sub.add_parser(
        "graph-map", help="Store an explicit document-local graph -> cord.graph.id association (#43)")
    graph_map.add_argument("alias")
    graph_map.add_argument("document_graph_id", help="graphs[].id inside the connection's published topology document")
    graph_map.add_argument("graph_id", help="The recorded cord.graph.id this document-local graph corresponds to")
    graph_map.set_defaults(func=cmd_graph_map)

    sync = sub.add_parser("sync", help="Explicitly refresh a connection's published topology snapshot")
    sync.add_argument("alias", nargs="?", default=None)
    sync.set_defaults(func=cmd_sync)

    list_cmd = sub.add_parser("list", help="Show reachability and graphs for registered deployments")
    list_cmd.add_argument("alias", nargs="?", default=None)
    list_cmd.set_defaults(func=cmd_list)

    view = sub.add_parser("view", help="Show the catalog: Graphs, topology, and recorded execution")
    view.add_argument("alias", nargs="?", default=None)
    view.add_argument("--archive", type=Path, action="append", default=[],
                      help="Archive directory or *.otlp.jsonl[.gz] file to read recorded execution from (repeatable)")
    view.add_argument("--watch", action="append", default=[], metavar="ALIAS:THREAD_ID:RUN_ID",
                      help="Follow a Run's live Aegra SSE stream and show it alongside recorded execution, "
                           "until it reaches a terminal status or --watch-timeout elapses (repeatable, #20)")
    view.add_argument("--watch-timeout", type=float, default=120.0, dest="watch_timeout",
                      help="Seconds to follow each --watch target before giving up (default: 120)")
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
    rule_add.add_argument("--when", action="append", default=[],
                          help="KEY=VALUE required condition for a run.finished rule (repeatable)")
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
