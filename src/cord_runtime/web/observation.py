"""Process-scoped observation only; all domain projections remain in viewer."""
from pathlib import Path
import threading
import time

from cord_runtime.archive_health import read_active_spans
from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.connections import load_connections
from cord_runtime.live_reconciliation import LiveRun, reconcile
from cord_runtime.run_continuity import load_run_continuity
from cord_runtime.topology import check_freshness
from cord_runtime.viewer import build_catalog, build_execution_tree


class Observation:
    def __init__(self, board: Path, archives=(), *, alias=None, watch=()):
        self.board, self.archives, self.alias = board, archives, alias
        self.watch = watch
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.live = {}
        self.errors = {}
        self.workers = {}
        self.probed = {}
        self.connection_errors = []
        self.archive_diagnostics = []

    def close(self):
        self.stop.set()

    def connections(self):
        connections = load_connections(self.board)
        if self.alias is not None and self.alias not in connections:
            raise ValueError("Unknown connection alias")
        # Always classify against every deployment before applying the UI scope.
        # Filtering first would falsely assign ambiguous archive evidence.
        return connections

    def discover(self, connections):
        targets = set(self.watch)
        for alias, threads in load_run_continuity(self.board).items():
            for thread, record in threads.items():
                if record.get("api_run_ids"):
                    targets.add((alias, thread, record["api_run_ids"][-1]))
        with self.lock:
            for target in targets:
                alias, thread, invocation = target
                if alias not in connections or target in self.workers:
                    continue
                worker = threading.Thread(target=self._observe,
                                          args=(*target, connections[alias]["endpoint"]),
                                          daemon=True)
                self.workers[target] = worker
                worker.start()

    def _observe(self, alias, thread, invocation, endpoint):
        from cord_runtime.cli import _resolve_logical_run_id
        backend = AegraExecutionBackend(endpoint)
        target = (alias, thread, invocation)
        logical = _resolve_logical_run_id(self.board, alias, thread, invocation)
        while not self.stop.is_set():
            try:
                identity = backend.describe_run(thread, invocation)
                graph = backend.describe_assistant(identity["assistant_id"])["graph_id"]
                with self.lock:
                    live = self.live.get(logical)
                    if live is None or getattr(live, "_invocation", None) != invocation:
                        live = LiveRun(logical, thread, graph_id=graph,
                                       assistant_id=identity["assistant_id"], subject=identity["subject"])
                        live._invocation = invocation
                        self.live[logical] = live
                    live.status = identity["status"]
                    if live.is_terminal and live.completed_at is None:
                        live.completed_at = time.monotonic()
                    self.errors.pop(target, None)
                if live.is_terminal:
                    return
            except (RuntimeError, ValueError, KeyError, TypeError):
                with self.lock:
                    self.errors[target] = "Deployment disconnected; retrying observation"
            if self.stop.wait(1):
                return

    def snapshot(self):
        from cord_runtime.cli import _probe
        connections = self.connections()
        self.discover(connections)
        topology, probed = {}, {}
        for alias, info in connections.items():
            endpoint = info["endpoint"]
            status = _probe(endpoint)
            # Retain the last observed identities during a disconnect so runs
            # cannot jump to a different deployment merely because one is down.
            if not status["reachable"]:
                status["graphs"] = self.probed.get(alias, {}).get("graphs", [])
            probed[alias] = {**info, **status}
            topology[endpoint] = check_freshness(self.board, endpoint, timeout=2)
        self.probed = probed
        self.connection_errors = ["Deployment disconnected: " + alias
                                  for alias, info in probed.items() if not info["reachable"]]
        spans = self.read_archive()
        recorded = {r["run_id"] for r in build_execution_tree(spans)}
        with self.lock:
            views = reconcile(self.live, recorded, now=time.monotonic())
        catalog = build_catalog(probed, topology, spans, live_runs=views)
        if self.alias is not None:
            catalog["graphs"] = [g for g in catalog["graphs"]
                                 if g["deployment_alias"] == self.alias
                                 or self.alias in g["ambiguous_aliases"]]
        return catalog

    def read_archive(self):
        spans, incomplete = read_active_spans(list(self.archives)) if self.archives else ({}, ())
        with self.lock:
            self.archive_diagnostics = (["Archive write incomplete; showing complete records"] if incomplete
                                        else ["No recorded spans yet"] if self.archives and not spans else [])
        return spans

    def diagnostics(self):
        with self.lock:
            return sorted(set(self.errors.values()) | set(self.connection_errors) | set(self.archive_diagnostics))

    def run_updates(self, run_id):
        """No catalog/probes per SSE tick. Re-read trees only on archive change."""
        signature = None
        recorded = {}
        while not self.stop.is_set():
            paths = []
            for root in self.archives:
                paths.extend(root.glob("*.otlp.jsonl*") if root.is_dir() else [root])
            current = tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths))
            if current != signature:
                spans = self.read_archive()
                recorded = {r["run_id"]: r for r in build_execution_tree(spans)}
                signature = current
            with self.lock:
                live = reconcile(self.live, recorded, now=time.monotonic()).get(run_id)
            yield {"run": recorded.get(run_id) or live,
                   "source": "recorded" if run_id in recorded else (live or {}).get("source", "unavailable"),
                   "diagnostics": self.diagnostics()}
            if self.stop.wait(1):
                return
