"""Small, credential-free execution; no graph implementation or model service."""

import argparse

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from cord_runtime.execution import AttemptOutcome, StepOutcome, run
from cord_runtime.telemetry import RedactingOTLPExporter


def emit(endpoint="http://127.0.0.1:4318/v1/traces"):
    provider = TracerProvider(resource=Resource({"service.name": "cordboard-fixture"}))
    provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(endpoint)))
    tracer = provider.get_tracer("cordboard.fixture", "0.1.0")
    with run(tracer, "urn:cordboard:fixture:5", "fixture") as execution:
        with execution.step("draft", StepOutcome.PASSED) as step:
            for number, tier, outcome in (
                (1, "fast", AttemptOutcome.FAILED),
                (2, "fast", AttemptOutcome.ESCALATED),
                (3, "deep", AttemptOutcome.PASSED),
            ):
                with step.attempt(number, tier, outcome):
                    pass
    provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4318/v1/traces")
    emit(parser.parse_args().endpoint)
