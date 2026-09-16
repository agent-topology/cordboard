"""Black-box browser check against an installed candidate in a Testbed environment.

Run with the Testbed's Python (playwright installed), while its synthetic Aegra
and Collector are running. This script imports no Cordboard implementation.
"""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time

import requests
from agent_topology.spec import finalize_document
from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cord", type=Path, required=True)
parser.add_argument("--archive", type=Path, required=True)
parser.add_argument("--evidence", type=Path, required=True)
parser.add_argument("--endpoint", default="http://127.0.0.1:52026")
args = parser.parse_args()
args.evidence.mkdir(parents=True, exist_ok=False)
board = args.evidence / "board"
board.mkdir()
commands = []


def cord(*arguments):
    command = [str(args.cord), "--board", str(board), *map(str, arguments)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    commands.append({"command": command, "returncode": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr)
    return result.stdout


document = finalize_document({
    "topologyVersion": "0.1",
    "provenance": {"generatedAt": "2026-09-16T00:00:00Z",
                   "producer": {"name": "browser-acceptance", "version": "1"},
                   "framework": {"name": "synthetic", "version": "1"}},
    "producerLimitations": [],
    "completeness": {"status": "complete", "gaps": []},
    "graphs": [{"id": "main", "structure": {
        "nodes": [{"id": "start"}, {"id": "finish"}],
        "edges": [{"id": "edge", "source": "start", "target": "finish", "kind": "direct"}],
        "joins": [], "entryNodeIds": ["start"], "exitNodeIds": ["finish"]}}]})


class IdlePublisher(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        payload = (document if self.path == "/.well-known/agent-topology.manifest.json"
                   else {"assistants": [{"graph_id": "idle"}]} if self.path == "/assistants"
                   else {})
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


with ThreadingHTTPServer(("127.0.0.1", 0), IdlePublisher) as publisher:
    threading.Thread(target=publisher.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{publisher.server_port}"
    cord("add", "minimal", args.endpoint)
    cord("add", "idle-a", endpoint)
    cord("add", "idle-b", endpoint)
    command = [str(args.cord), "--board", str(board), "serve", "--archive", str(args.archive)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        base = process.stdout.readline().strip().split()[-1].rstrip("/")
        assert base.startswith("http://127.0.0.1:")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            expect(page.get_by_role("heading", name="Deployment overview")).to_be_visible()
            for alias in ("idle-a", "idle-b"):
                page.goto(base + f"/connections/{alias}/graphs/idle")
                expect(page.get_by_role("img", name="Published topology for idle")).to_be_visible()
                assert "No recorded Runs" in page.content()
            page.screenshot(path=str(args.evidence / "idle-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 667})
            page.screenshot(path=str(args.evidence / "idle-narrow.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            results = {}
            for scenario in ("success", "retry", "interrupt"):
                graph = "interrupt" if scenario == "interrupt" else "deterministic"
                # Keep the real page open while new executions arrive.
                page.goto(base + f"/connections/minimal/graphs/{graph}")
                input_path = args.evidence / f"{scenario}.json"
                input_path.write_text(json.dumps({"value": 1, "scenario": scenario}))
                result = json.loads(cord("run", "minimal", graph, f"browser-{scenario}", input_path, "--timeout", "10"))
                results[scenario] = result
                run_id = result["run_id"]
                expect(page.get_by_role("link", name="Run " + run_id, exact=True)).to_be_visible(timeout=20000)
                page.get_by_role("link", name="Run " + run_id, exact=True).click()
                if scenario == "interrupt":
                    expect(page.get_by_text("Live identity and status only.", exact=False)).to_be_visible()
                    expect(page.get_by_text("ingestion_pending", exact=False)).to_be_visible(timeout=12000)
                else:
                    expect(page.get_by_role("heading", name="Execution timeline")).to_be_visible(timeout=15000)
                    payload = requests.get(page.url + "?format=json", timeout=10).json()
                    assert len(payload["steps"][0]["attempts"]) == (2 if scenario == "retry" else 1)
                    assert payload["end_ns"] >= payload["start_ns"]
                    if scenario == "retry":
                        assert [a["outcome"] for a in payload["steps"][0]["attempts"]] == ["failed", "passed"]
                for width, height, label in [(1280, 900, "desktop"), (375, 667, "narrow")]:
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.evidence / f"{scenario}-{label}.png"), full_page=True)
                page.keyboard.press("Tab")
                assert page.locator(":focus").count() == 1
            page.goto(base + "/connections/minimal/graphs/deterministic")
            page.get_by_label("Subject", exact=True).fill("browser-retry")
            page.get_by_role("button", name="Apply filters").click()
            expect(page.get_by_role("link", name="Run " + results["retry"]["run_id"], exact=True)).to_be_visible()
            assert page.get_by_role("link", name="Run " + results["success"]["run_id"], exact=True).count() == 0
            process.terminate()
            process.wait(timeout=10)
            expect(page.locator("#connection-alert")).to_contain_text("Disconnected", timeout=10000)
            page.screenshot(path=str(args.evidence / "disconnected-narrow.png"), full_page=True)
            assert errors == []
            browser.close()
        (args.evidence / "results.json").write_text(json.dumps({
            "commands": commands, "serve_command": command, "runs": results,
            "browser_errors": errors, "result": "passed",
            "scope": "Installed wheel; real Testbed Aegra/Postgres/Collector; independent idle publisher. "
                     "Interrupt fixture has no telemetry: live interrupted/ingestion pending, no invented timing."
        }, indent=2))
        print("Installed browser acceptance passed:", args.evidence)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        publisher.shutdown()
