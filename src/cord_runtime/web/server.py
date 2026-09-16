"""Small read-only HTTP router; mutations and graph state are not exposed."""
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .observation import Observation
from .presentation import TOPOLOGY_LABELS, duration, graph_url, layout, matches, run_url, timeline

ROOT = files("cord_runtime.web")
ENV = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=select_autoescape())
ENV.globals.update(graph_url=graph_url, run_url=run_url, topology_labels=TOPOLOGY_LABELS,
                   layout=layout, timeline=timeline)
ENV.filters["duration"] = duration


def select(catalog, path):
    # Split before decoding: '/' inside an alias/graph/run remains one segment.
    parts = [unquote(part) for part in path.split("/")[1:]]
    if path == "/":
        return None, None, False
    events = parts[-1:] == ["events"]
    if events:
        parts.pop()
    for graph in catalog["graphs"]:
        prefix = [unquote(p) for p in graph_url(graph).split("/")[1:]]
        if parts == prefix and not events:
            return graph, None, False
        if parts[:len(prefix)] == prefix and len(parts) == len(prefix) + 2 and parts[-2] == "runs":
            for run in graph["runs"] + graph["live_runs"]:
                if run["run_id"] == parts[-1]:
                    return graph, run, events
    raise LookupError("Not found")


def make_server(observation, host="127.0.0.1", port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request URLs can contain opaque Subject identifiers.

        def reply(self, body, content_type, status=200):
            encoded = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            as_json = query.get("format") == ["json"] or "application/json" in self.headers.get("Accept", "")
            try:
                if url.path in ("/static/viewer.css", "/static/viewer.js"):
                    asset = url.path.rsplit("/", 1)[1]
                    self.reply((ROOT / "static" / asset).read_bytes(),
                               "text/css" if asset.endswith(".css") else "text/javascript")
                    return
                catalog = observation.snapshot()
                graph, run, events = select(catalog, url.path)
                if events:
                    self.stream(run["run_id"])
                    return
                payload = run if run is not None else graph if graph is not None else catalog
                if as_json:
                    self.reply(json.dumps(payload), "application/json")
                    return
                subject, status = query.get("subject", [""])[0], query.get("status", [""])[0]
                runs = [] if graph is None else [
                    r for r in graph["runs"] + graph["live_runs"] if matches(r, subject, status)]
                self.reply(ENV.get_template("page.html").render(
                    catalog=catalog, graph=graph, run=run, runs=runs, subject=subject, status=status,
                    diagnostics=observation.diagnostics(), error=None), "text/html; charset=utf-8")
            except LookupError:
                self.error(404, "View not found", as_json)
            except (ValueError, OSError, RuntimeError):
                self.error(503, "Observation unavailable. Check connections and archive; retry this page.", as_json)

        def error(self, status, message, as_json):
            body = json.dumps({"error": message}) if as_json else ENV.get_template("page.html").render(
                catalog={"graphs": []}, graph=None, run=None, diagnostics=[], error=message)
            self.reply(body, "application/json" if as_json else "text/html; charset=utf-8", status)

        def stream(self, run_id):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                sequence = max(0, int(self.headers.get("Last-Event-ID", "0")))
            except ValueError:
                sequence = 0
            previous = None
            try:
                for update in observation.run_updates(run_id):
                    # Elapsed wall-clock diagnostics are not a lifecycle change.
                    comparable = json.loads(json.dumps(update))
                    if comparable["run"]:
                        comparable["run"].pop("seconds_since_completion", None)
                    text = json.dumps(comparable, sort_keys=True)
                    if text != previous:
                        sequence += 1
                        self.wfile.write(f"id: {sequence}\nevent: snapshot\ndata: {text}\n\n".encode())
                        previous = text
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (OSError, ValueError, RuntimeError):
                return

    return ThreadingHTTPServer((host, port), Handler)


def serve(board, archives=(), *, alias=None, host="127.0.0.1", port=0, watch=()):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("--host must be a loopback IP address") from None
    if not address.is_loopback or address.version != 4:
        raise ValueError("--host must be an IPv4 loopback address")
    if not 0 <= port <= 65535:
        raise ValueError("--port must be between 0 and 65535")
    observation = Observation(board, archives, alias=alias, watch=watch)
    connections = observation.connections()
    if any(target[0] not in connections for target in watch):
        raise ValueError("Unknown --watch connection")
    try:
        with make_server(observation, host, port) as server:
            print(f"Cordboard: http://{host}:{server.server_port}/", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        observation.close()
    return 0
