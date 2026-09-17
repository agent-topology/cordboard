"""Browser observation and controls, delegating execution and approval to domain APIs."""
import hmac
import http.cookies
import ipaddress
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cord_runtime import approval_inbox, run_continuity
from cord_runtime.aegra_client import execute as execute_run
from cord_runtime.backends.aegra import AegraExecutionBackend
from cord_runtime.connection_diagnostics import diagnose_connection
from cord_runtime.entity_auth import HttpEntityAuthBoundary

from .observation import Observation
from .presentation import (
    NODE_STATUS_LABELS,
    TOPOLOGY_LABELS,
    approval_url,
    describe,
    duration,
    execute_url,
    graph_url,
    layout,
    matches,
    run_topology,
    run_url,
    scenario_summary,
    timeline,
)

ROOT = files("cord_runtime.web")
ENV = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=select_autoescape())
ENV.globals.update(graph_url=graph_url, run_url=run_url, execute_url=execute_url, approval_url=approval_url,
                   topology_labels=TOPOLOGY_LABELS, node_status_labels=NODE_STATUS_LABELS,
                   layout=layout, run_topology=run_topology, timeline=timeline,
                   scenario_summary=scenario_summary, describe=describe)
ENV.filters["duration"] = duration
ENV.filters["tojson"] = lambda value, indent=None: json.dumps(value, indent=indent)

_MAX_BODY = 1_000_000  # bounded form submissions; this surface never accepts uploads


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


def _match_execute(path):
    parts = [unquote(p) for p in path.split("/")[1:]]
    if len(parts) == 5 and parts[0] == "connections" and parts[2] == "graphs" and parts[4] == "execute":
        return parts[1], parts[3]
    return None


def _match_approvals(path):
    parts = [unquote(p) for p in path.split("/")[1:]]
    if parts == ["approvals"]:
        return ("list",)
    if len(parts) == 4 and parts[0] == "approvals":
        return ("detail", parts[1], parts[2], parts[3])
    return None


def _waiting_as_dict(item):
    return {"deployment": item.deployment, "assistant": item.assistant, "subject": item.subject,
            "logical_run_id": item.logical_run_id, "thread_id": item.thread_id,
            "interrupt_id": item.interrupt_id, "value": item.value, "revision": item.revision}


def make_server(observation, host="127.0.0.1", port=0, *, reminder_after=300.0, timeout_after=3600.0):
    csrf_token = secrets.token_urlsafe(32)
    nonces: set[str] = set()
    nonces_lock = threading.Lock()

    def new_nonce():
        nonce = secrets.token_urlsafe(16)
        with nonces_lock:
            nonces.add(nonce)
        return nonce

    def consume_nonce(nonce):
        with nonces_lock:
            if nonce in nonces:
                nonces.discard(nonce)
                return True
            return False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request URLs/bodies can contain opaque Subject identifiers and business values.

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
            self.send_header("Set-Cookie", f"cord_csrf={csrf_token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(encoded)

        def error(self, status, message, as_json):
            body = json.dumps({"error": message}) if as_json else ENV.get_template("page.html").render(
                catalog={"graphs": []}, graph=None, run=None, diagnostics=[], error=message, action_slot=None)
            self.reply(body, "application/json" if as_json else "text/html; charset=utf-8", status)

        def _action_slot(self, graph, run):
            alias = graph.get("deployment_alias") if graph else None
            if alias is None or run is None:
                return None
            relevant = run.get("aegra_status") == "interrupted" or any(
                step.get("outcome") == "awaiting_approval" for step in run.get("steps", []))
            if not relevant:
                return None
            try:
                board = observation.board
                connections = observation.connections()
            except AttributeError:
                return None  # An Observation stand-in without connection/board access; nothing to resolve.
            thread_id = run.get("thread_id")
            if thread_id is None:
                thread_id = run_continuity.thread_for_run(board, alias, run["run_id"])
            if thread_id is None:
                return None
            if not connections.get(alias, {}).get("auth_endpoint"):
                return {"status": "no_authority", "thread_id": thread_id}
            waiting = approval_inbox.discover_waiting_for_thread(
                board, alias, thread_id,
                reminder_after=reminder_after, timeout_after=timeout_after, now=time.time())
            if not waiting:
                return {"status": "none", "thread_id": thread_id}
            if len(waiting) > 1:
                return {"status": "ambiguous", "thread_id": thread_id}
            return {"status": "ready", "thread_id": thread_id, "interrupt_id": waiting[0].interrupt_id,
                    "url": approval_url(alias, thread_id, waiting[0].interrupt_id)}

        def _render_execute(self, alias, graph_id, as_json, *, status=200, message=None):
            connection = observation.connections().get(alias)
            if connection is None:
                self.error(404, "Unknown deployment", as_json)
                return
            catalog = observation.snapshot()
            graph = next((g for g in catalog["graphs"]
                         if g["deployment_alias"] == alias and g["graph_id"] == graph_id), None)
            if graph is None:
                self.error(404, "Unknown graph", as_json)
                return
            if as_json:
                self.reply(json.dumps({"error": message} if message else {}), "application/json", status)
                return
            execution = diagnose_connection(observation.board, alias, connection,
                                            archive_paths=tuple(observation.archives)).capabilities.execution
            if execution.state == "ready":
                try:
                    assistants = [item for item in AegraExecutionBackend(connection["endpoint"]).list_assistants()
                                  if item.get("graph_id") == graph_id]
                except RuntimeError:
                    assistants = []
            else:
                # Unavailable execution is diagnosed below the same way `cord diagnose` reports it
                # (#73); the deployment is not re-probed a second time for a form that cannot submit.
                assistants = []
            self.reply(ENV.get_template("execute.html").render(
                catalog=catalog, graph=graph, assistants=assistants, execution=execution, csrf_token=csrf_token,
                nonce=new_nonce(), message=message, diagnostics=observation.diagnostics()),
                "text/html; charset=utf-8", status)

        def _render_approvals_list(self, as_json):
            waiting = approval_inbox.discover_waiting(
                observation.board, reminder_after=reminder_after, timeout_after=timeout_after, now=time.time())
            if as_json:
                self.reply(json.dumps([_waiting_as_dict(item) for item in waiting]), "application/json")
                return
            self.reply(ENV.get_template("approvals.html").render(
                catalog=observation.snapshot(), waiting=waiting, diagnostics=observation.diagnostics()),
                "text/html; charset=utf-8")

        def _render_approval_detail(self, target, as_json, *, status=200, message=None, submission_result=None):
            alias, thread_id, interrupt_id = target
            waiting = approval_inbox.discover_waiting_for_thread(
                observation.board, alias, thread_id,
                reminder_after=reminder_after, timeout_after=timeout_after, now=time.time())
            match = next((item for item in waiting if item.interrupt_id == interrupt_id), None)
            if as_json:
                body = _waiting_as_dict(match) if match else {"error": message or "no longer waiting"}
                self.reply(json.dumps(body), "application/json", status if match else 404)
                return
            has_authority = bool(observation.connections().get(alias, {}).get("auth_endpoint"))
            self.reply(ENV.get_template("approval_detail.html").render(
                catalog=observation.snapshot(), alias=alias, thread_id=thread_id, interrupt_id=interrupt_id,
                waiting=match, has_authority=has_authority, csrf_token=csrf_token, message=message,
                submission_result=submission_result, diagnostics=observation.diagnostics()),
                "text/html; charset=utf-8", status)

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
                execute_match = _match_execute(url.path)
                if execute_match is not None:
                    self._render_execute(*execute_match, as_json)
                    return
                approval_match = _match_approvals(url.path)
                if approval_match is not None:
                    if approval_match[0] == "list":
                        self._render_approvals_list(as_json)
                    else:
                        self._render_approval_detail(approval_match[1:], as_json)
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
                action_slot = self._action_slot(graph, run) if run is not None else None
                self.reply(ENV.get_template("page.html").render(
                    catalog=catalog, graph=graph, run=run, runs=runs, subject=subject, status=status,
                    diagnostics=observation.diagnostics(), error=None, action_slot=action_slot,
                    action_slot_for=self._action_slot),
                    "text/html; charset=utf-8")
            except LookupError:
                self.error(404, "View not found", as_json)
            except (ValueError, OSError, RuntimeError):
                self.error(503, "Observation unavailable. Check connections and archive; retry this page.", as_json)

        def _read_form(self):
            if "application/x-www-form-urlencoded" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("expected application/x-www-form-urlencoded body")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_BODY:
                raise ValueError("request body missing or too large")
            raw = self.rfile.read(length)
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
            return {key: values[0] for key, values in parsed.items()}

        def _origin_and_csrf_ok(self, form):
            host, port = self.server.server_address[:2]
            if self.headers.get("Origin") != f"http://{host}:{port}":
                return False
            cookies = http.cookies.SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except http.cookies.CookieError:
                return False
            cookie_token = cookies["cord_csrf"].value if "cord_csrf" in cookies else ""
            form_token = form.get("csrf_token", "")
            return (bool(cookie_token) and bool(form_token)
                    and hmac.compare_digest(cookie_token.encode(), csrf_token.encode())
                    and hmac.compare_digest(form_token.encode(), csrf_token.encode()))

        def _handle_execute_post(self, target, form, as_json):
            alias, graph_id = target
            connection = observation.connections().get(alias)
            if connection is None:
                self.error(404, "Unknown deployment", as_json)
                return
            if not form.get("nonce") or not consume_nonce(form["nonce"]):
                self._render_execute(alias, graph_id, as_json, status=409,
                                     message="Already submitted, or this form expired. "
                                             "Reload the page to get a fresh submission token.")
                return
            assistants = AegraExecutionBackend(connection["endpoint"]).list_assistants()
            if not any(item.get("assistant_id") == form.get("assistant")
                       and item.get("graph_id") == graph_id for item in assistants):
                self._render_execute(alias, graph_id, as_json, status=400,
                                     message="Select an Assistant belonging to this graph")
                return
            try:
                graph_input = json.loads(form.get("input") or "{}")
                context_raw = (form.get("context") or "").strip()
                request_context = json.loads(context_raw) if context_raw else None
            except ValueError:
                self._render_execute(alias, graph_id, as_json, status=400,
                                     message="Input/context must be valid JSON")
                return
            try:
                result = execute_run(connection["endpoint"], form.get("assistant", ""), form.get("subject", ""),
                                     graph_input, request_context=request_context)
            except ValueError as exc:
                self._render_execute(alias, graph_id, as_json, status=400, message=str(exc))
                return
            except RuntimeError:
                self._render_execute(alias, graph_id, as_json,
                                     message="unknown: submission outcome could not be confirmed. "
                                             "Check the deployment or the Run list before submitting again.")
                return
            if result.get("thread_id") and result.get("run_id"):
                run_continuity.record_submission(observation.board, alias, result["thread_id"], result["run_id"])
            if as_json:
                self.reply(json.dumps(result), "application/json")
                return
            location = run_url({"deployment_alias": alias, "graph_id": graph_id}, result)
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", f"cord_csrf={csrf_token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()

        def _handle_respond_post(self, target, form, as_json):
            alias, thread_id, interrupt_id = target
            connection = observation.connections().get(alias)
            if connection is None:
                self.error(404, "Unknown deployment", as_json)
                return
            try:
                response_value = json.loads(form.get("response_value") or "null")
            except ValueError:
                self._render_approval_detail((alias, thread_id, interrupt_id), as_json, status=400,
                                             message="response_value must be valid JSON")
                return
            auth_endpoint = connection.get("auth_endpoint")
            if not auth_endpoint:
                self._render_approval_detail((alias, thread_id, interrupt_id), as_json, status=409,
                                             message="no authority configured for this deployment; "
                                                     "approval is refused by default")
                return
            boundary = HttpEntityAuthBoundary(auth_endpoint)
            try:
                result = approval_inbox.submit_response(
                    observation.board, deployment=alias, thread_id=thread_id, interrupt_id=interrupt_id,
                    approver=form.get("approver", ""), response_value=response_value,
                    revision=form.get("revision") or None, auth_boundary=boundary, now=time.time())
            except approval_inbox.ApprovalInboxError as exc:
                self._render_approval_detail((alias, thread_id, interrupt_id), as_json, status=404, message=str(exc))
                return
            if as_json:
                self.reply(json.dumps({"status": result.status, "reason": result.reason}), "application/json")
                return
            status_code = 200 if result.status == "resumed" else 409
            self._render_approval_detail((alias, thread_id, interrupt_id), as_json,
                                         status=status_code, submission_result=result)

        def do_POST(self):
            url = urlsplit(self.path)
            as_json = "application/json" in self.headers.get("Accept", "")
            execute_match = _match_execute(url.path)
            approval_match = _match_approvals(url.path)
            is_respond = approval_match is not None and approval_match[0] == "detail"
            if execute_match is None and not is_respond:
                # Every other route stays read-only; there is nothing to parse a body for.
                self.error(404, "View not found", as_json)
                return
            try:
                form = self._read_form()
            except (ValueError, UnicodeDecodeError):
                self.error(400, "Malformed request body", as_json)
                return
            if not self._origin_and_csrf_ok(form):
                self.error(403, "Cross-origin or missing/invalid CSRF token", as_json)
                return
            try:
                if execute_match is not None:
                    self._handle_execute_post(execute_match, form, as_json)
                else:
                    self._handle_respond_post(approval_match[1:], form, as_json)
            except (ValueError, OSError, RuntimeError):
                self.error(503, "Observation unavailable. Check connections and archive; retry.", as_json)

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


def serve(board, archives=(), *, alias=None, host="127.0.0.1", port=0, watch=(),
          reminder_after=300.0, timeout_after=3600.0):
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
        with make_server(observation, host, port,
                         reminder_after=reminder_after, timeout_after=timeout_after) as server:
            print(f"Cordboard: http://{host}:{server.server_port}/", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        observation.close()
    return 0
