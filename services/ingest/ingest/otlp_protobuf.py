"""OTLP/HTTP protobuf request decoding.

Standard OTLP exporters speak protobuf-first; this converts an
``application/x-protobuf`` ExportTraceServiceRequest body into the same
camelCase JSON dict shape ``otel_mapper.flatten_export_request`` consumes, so
both wire formats share one pipeline.

The one lossy spot in the generic proto->JSON conversion is ids:
``MessageToDict`` renders protobuf ``bytes`` fields as base64, while OTLP/JSON
(and everything downstream: uuid5 run/graph ids, ClickHouse rows) uses
lowercase hex. Ids are re-encoded here — without this, the same trace would
get DIFFERENT run/graph uuids depending on which wire format delivered it.
"""

from __future__ import annotations

import base64
from typing import Any

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

_ID_KEYS = ("traceId", "spanId", "parentSpanId")


def parse_protobuf_traces(body: bytes) -> dict[str, Any]:
    """Decode a protobuf ExportTraceServiceRequest into OTLP/JSON dict shape.

    Raises ``google.protobuf.message.DecodeError`` (or ValueError) on a body
    that is not a valid ExportTraceServiceRequest.
    """
    message = ExportTraceServiceRequest()
    message.ParseFromString(body)
    # Integer enums match what OTLP/JSON exporters emit for kind/status.code;
    # the mapper parses both ints and enum names, but ints round-trip through
    # the raw-span store unambiguously.
    payload = MessageToDict(message, use_integers_for_enums=True)
    for resource_spans in payload.get("resourceSpans") or []:
        for scope_spans in resource_spans.get("scopeSpans") or []:
            for span in scope_spans.get("spans") or []:
                for key in _ID_KEYS:
                    value = span.get(key)
                    if value:
                        span[key] = base64.b64decode(value).hex()
    return payload
