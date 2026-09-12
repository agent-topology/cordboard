"""Two minimal compatibility requests; success never depends on a model failing."""

import argparse
import json
from pathlib import Path
import time

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from cord_runtime.archive_check import check
from cord_runtime.archive_query import read_spans, query
from cord_runtime.execution import AttemptOutcome, StepOutcome, run
from cord_runtime.telemetry import RedactingOTLPExporter
from examples.minimal_graph import Tier
from examples.proxy_graph import ProxyModel


def smoke(proxy, collector, archive, cli):
    provider = TracerProvider(resource=Resource({"service.name": "cordboard-provider-smoke"}))
    provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(collector)))
    model = ProxyModel(proxy)
    try:
        with run(provider.get_tracer("cordboard.smoke"), "urn:cordboard:smoke:8", "fixture",
                 graph_id="provider-smoke") as active:
            run_id = active.attributes["cord.run.id"]
            for tier in Tier:
                with active.step(tier.value, StepOutcome.PASSED) as step:
                    with step.attempt(1, tier.value, AttemptOutcome.PASSED):
                        # Only schema/transport compatibility: do not judge random output.
                        model("hello", tier, 1)
    finally:
        provider.shutdown()
    deadline = time.monotonic() + 10
    while True:
        try:
            spans = [value[0] for value in read_spans([archive]).values()]
            own = [s for s in spans if s["attributes"].get("cord.run.id") == run_id]
            attempts = [s for s in own if s["name"] == "attempt"]
            models = [s for s in spans if "gen_ai.request.model" in s["attributes"]
                      and s.get("parentSpanId") in {a["spanId"] for a in attempts}]
            if len(own) == 5 and len(attempts) == len(models) == 2:
                break
        except ValueError:
            pass  # An active Collector may still be flushing its first line.
        if time.monotonic() >= deadline:
            raise RuntimeError("smoke archive incomplete: check callback, propagation and Collector gate")
        time.sleep(.1)
    if (len({s["traceId"] for s in own + models}) != 1
            or {s["parentSpanId"] for s in models} != {s["spanId"] for s in attempts}
            or not all(s["attributes"].get("cord.redacted") is True for s in own + models)
            or any(row["graph"] == "provider-smoke" for row in query([archive], reference=time.time_ns()))):
        raise RuntimeError("smoke archive trace contract failed")
    if check([archive], cli) != 0:
        raise RuntimeError("smoke archive scan failed")
    return {"compatible": True, "aliases": 2, "attempts": 2, "model_spans": 2, "escalations": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="http://127.0.0.1:4000")
    parser.add_argument("--collector", default="http://127.0.0.1:4318/v1/traces")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--cli", required=True)
    args = parser.parse_args()
    try:
        result = smoke(args.proxy, args.collector, args.archive, args.cli)
    except Exception:
        parser.exit(1, "provider smoke failed: check readiness, mapping, response schema and archive gate\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
