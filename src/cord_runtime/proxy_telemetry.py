"""Public LiteLLM callback: metadata-only model spans, redacted before export."""

import asyncio
import logging
import os

from litellm.integrations.custom_logger import CustomLogger
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from cord_runtime.telemetry import RedactingOTLPExporter


class ProxyTelemetry(CustomLogger):
    def __init__(self, endpoint):
        super().__init__()
        self.provider = TracerProvider(resource=Resource({"service.name": "cordboard-proxy"}))
        self.provider.add_span_processor(SimpleSpanProcessor(RedactingOTLPExporter(endpoint)))
        self.tracer = self.provider.get_tracer("cordboard.proxy")

    def emit(self, kwargs, response, start_time, end_time, failed):
        try:
            params = kwargs.get("litellm_params", {})
            headers = params.get("proxy_server_request", {}).get("headers", {})
            context = TraceContextTextMapPropagator().extract(headers, context=Context())
            if not trace.get_current_span(context).get_span_context().is_valid:
                raise ValueError
            attrs = {"gen_ai.request.model": kwargs["model"], "gen_ai.operation.name": "chat"}
            if not failed:
                attrs["gen_ai.response.model"] = response.model
                if response.usage:
                    attrs["gen_ai.usage.input_tokens"] = response.usage.prompt_tokens
                    attrs["gen_ai.usage.output_tokens"] = response.usage.completion_tokens
            span = self.tracer.start_span("chat", context=context, kind=SpanKind.CLIENT,
                                          attributes=attrs, start_time=int(start_time.timestamp() * 1e9))
            span.set_status(Status(StatusCode.ERROR if failed else StatusCode.OK))
            span.end(end_time=int(end_time.timestamp() * 1e9))
        except Exception:
            # Never log callback kwargs, provider exceptions, or model content.
            logging.getLogger(__name__).warning("proxy telemetry failed; check trace context and callback contract")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await asyncio.to_thread(self.emit, kwargs, response_obj, start_time, end_time, False)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        await asyncio.to_thread(self.emit, kwargs, response_obj, start_time, end_time, True)


handler = ProxyTelemetry(os.environ.get("CORD_COLLECTOR_URL", "http://127.0.0.1:4318/v1/traces"))
