"""Public OTel encoding, upstream redaction, then OTLP/HTTP in one process.

Install this as the provider's only exporter. A second exporter would bypass
the boundary. No SDK ReadableSpan or shared Resource is mutated.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import logging

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
import redact_secret
import requests

logger = logging.getLogger(__name__)


class BlockedSpan(Exception):
    """Fixed, value-free internal control flow."""


def _text(value: str) -> str:
    result = redact_secret.scan_and_redact(value)
    if any(finding.action == "block" for finding in result.findings):
        raise BlockedSpan
    return result.text


def _sanitize(message: Message) -> None:
    """Walk the complete OTLP envelope, including nested AnyValues.

    Opaque protocol IDs remain bytes; AnyValue bytes must be valid UTF-8 or
    processing fails closed. Scanning happens before JSON escapes/base64 can
    conceal text, including multiline private keys.
    """
    for field, value in message.ListFields():
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            if field.is_repeated:
                for child in value:
                    _sanitize(child)
            else:
                _sanitize(value)
        elif field.type == FieldDescriptor.TYPE_STRING:
            setattr(message, field.name, _text(value))
        elif field.type == FieldDescriptor.TYPE_BYTES and field.name == "bytes_value":
            setattr(message, field.name, _text(value.decode("utf-8")).encode("utf-8"))


def redact_request(request: ExportTraceServiceRequest) -> ExportTraceServiceRequest:
    """Return independently sanitized span envelopes; suppress blocks/errors.

    Shared resource/scope data is scanned for each span. A block there suppresses
    every affected span. Existing stamps are discarded and never trusted.
    """
    accepted = ExportTraceServiceRequest()
    for resource in request.resource_spans:
        for scope in resource.scope_spans:
            for span in scope.spans:
                candidate = ExportTraceServiceRequest()
                copied_resource = candidate.resource_spans.add()
                copied_resource.CopyFrom(resource)
                del copied_resource.scope_spans[:]
                copied_scope = copied_resource.scope_spans.add()
                copied_scope.CopyFrom(scope)
                del copied_scope.spans[:]
                copied_span = copied_scope.spans.add()
                copied_span.CopyFrom(span)
                for i in reversed(range(len(copied_span.attributes))):
                    if copied_span.attributes[i].key == "cord.redacted":
                        del copied_span.attributes[i]
                try:
                    _sanitize(candidate)
                except BlockedSpan:
                    logger.warning("telemetry span suppressed: block finding")
                    continue
                except Exception:
                    # Never render exceptions, findings, or payloads in diagnostics.
                    logger.warning("telemetry span suppressed: processing failed")
                    continue
                stamp = copied_span.attributes.add(key="cord.redacted")
                stamp.value.bool_value = True
                accepted.resource_spans.add().CopyFrom(copied_resource)
    return accepted


#: Fixed, value-free reasons `ExportOutcome.reason` takes -- never a response
#: body, header, or URL (#45 AC2/AC5).
UNREACHABLE = "unreachable"
HTTP_ERROR = "http_error"
REJECTED_SPANS = "rejected_spans"
PROCESSING_FAILED = "processing_failed"


@dataclass(frozen=True)
class ExportOutcome:
    """The most recent export attempt's delivery outcome, kept alongside the
    OTel SDK's own `SpanExportResult` rather than in place of it, so a
    caller can distinguish *why* delivery failed instead of a bare boolean
    (#45 AC2). `rejected_spans` is populated only when the Collector's own
    OTLP `partial_success` explicitly reported a count; it is never invented
    for the other failure reasons, which the pinned Collector cannot supply
    a count for."""

    delivered: bool
    reason: str | None = None
    rejected_spans: int | None = None


class RedactingOTLPExporter(SpanExporter):
    """Synchronous, bounded-time exporter for the local archive slice.

    No disk queue or automatic retry retains unprocessed telemetry. The HTTP
    session ignores environment proxies and never follows redirects.
    """

    def __init__(self, endpoint: str = "http://127.0.0.1:4318/v1/traces"):
        self.endpoint = endpoint
        self.session = requests.Session()
        self.session.trust_env = False
        self.last_export: ExportOutcome | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        reason = None
        rejected_spans = None
        try:
            safe = redact_request(encode_spans(spans))
            if not safe.resource_spans:
                self.last_export = ExportOutcome(delivered=True)
                return SpanExportResult.SUCCESS
            try:
                response = self.session.post(
                    self.endpoint,
                    data=safe.SerializeToString(),
                    headers={"Content-Type": "application/x-protobuf"},
                    timeout=10,
                    allow_redirects=False,
                )
            except requests.RequestException:
                reason = UNREACHABLE
                raise
            if response.status_code != 200:
                reason = HTTP_ERROR
                raise RuntimeError
            result = ExportTraceServiceResponse.FromString(response.content)
            if result.partial_success.rejected_spans:
                reason = REJECTED_SPANS
                rejected_spans = result.partial_success.rejected_spans
                raise RuntimeError
        except Exception:
            reason = reason or PROCESSING_FAILED
            self.last_export = ExportOutcome(delivered=False, reason=reason, rejected_spans=rejected_spans)
            logger.warning("telemetry export failed: %s", reason)
            return SpanExportResult.FAILURE
        self.last_export = ExportOutcome(delivered=True)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.session.close()
