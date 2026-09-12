"""Run the existing graph against the local, validated LiteLLM proxy."""

import argparse
import json

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
import requests

from cord_runtime.telemetry import RedactingOTLPExporter
from examples.minimal_graph import Tier, build_graph, run


class ProxyModel:
    def __init__(self, endpoint="http://127.0.0.1:4000"):
        self.endpoint = endpoint.rstrip("/") + "/v1/chat/completions"

    def __call__(self, source: str, tier: Tier, attempt: int) -> str:
        headers = {}
        TraceContextTextMapPropagator().inject(headers)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(
                    self.endpoint, headers=headers,
                    json={"model": tier.value, "messages": [
                        {"role": "user", "content": "Copy exactly: " + source}],
                        "stream": False, "max_tokens": 16},
                    timeout=60, allow_redirects=False,
                )
                if response.status_code != 200:
                    raise ValueError
                content = response.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content:
                    raise ValueError
                return content
        except Exception:
            raise RuntimeError("model request failed; check proxy readiness and alias mapping") from None


def execute(endpoint, collector):
    provider = TracerProvider(resource=Resource({"service.name": "cordboard-graph"}))
    provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(collector)))
    try:
        return run(build_graph(ProxyModel(endpoint)), subject="urn:cordboard:proxy:8",
                   tracer=provider.get_tracer("cordboard.graph"))
    finally:
        provider.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="http://127.0.0.1:4000")
    parser.add_argument("--collector", default="http://127.0.0.1:4318/v1/traces")
    args = parser.parse_args()
    try:
        result = execute(args.proxy, args.collector)
    except Exception:
        parser.exit(1, "graph failed; check proxy readiness and alias mapping\n")
    print(json.dumps({"outcome": result.steps[1].outcome.value,
                      "attempts": len(result.steps[1].attempts),
                      "escalations": result.escalation_count}))


if __name__ == "__main__":
    main()
