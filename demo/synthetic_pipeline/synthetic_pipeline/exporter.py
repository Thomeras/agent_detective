"""OTLP/HTTP JSON export for the demo pipeline.

The stock OpenTelemetry OTLP/HTTP exporter speaks protobuf; the Agent Detective
ingest endpoint (build spec 6.2) consumes OTLP/HTTP *JSON*
(ExportTraceServiceRequest). This module collects finished ``ReadableSpan``s and
serializes them to that JSON shape, matching ``packages/otel_mapper/testdata``:
attributes as the OTLP key/value array form, timestamps as unix-nanosecond
strings, span kind as the OTLP integer.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any, Sequence

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import SpanKind, StatusCode

_OTLP_SPAN_KIND = {
    SpanKind.INTERNAL: 1,
    SpanKind.SERVER: 2,
    SpanKind.CLIENT: 3,
    SpanKind.PRODUCER: 4,
    SpanKind.CONSUMER: 5,
}

_OTLP_STATUS_CODE = {
    StatusCode.UNSET: "STATUS_CODE_UNSET",
    StatusCode.OK: "STATUS_CODE_OK",
    StatusCode.ERROR: "STATUS_CODE_ERROR",
}


class CollectingSpanExporter(SpanExporter):
    """Buffers finished spans so they can be sent as one OTLP request."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class DeterministicIdGenerator(IdGenerator):
    """Sequential trace/span ids for reproducible fixture payloads.

    Ids are derived from a per-graph ``seed`` so distinct graphs (e.g. the
    happy vs faulted scenarios) never share trace/span ids. Ingest maps span
    ids to globally-unique run ids, so if two graphs reused the same ids the
    second one ingested would lose its runs to ON CONFLICT DO NOTHING. The
    derivation is a pure hash, so payloads stay byte-for-byte reproducible.
    """

    def __init__(self, seed: str = "") -> None:
        trace_digest = hashlib.sha256(f"trace:{seed}".encode()).digest()
        # 128-bit, top bit forced on so the id is non-zero and full width.
        self._trace_id = int.from_bytes(trace_digest[:16], "big") | (1 << 127)
        span_digest = hashlib.sha256(f"span:{seed}".encode()).digest()
        # Per-graph 16-bit prefix in the high bits; sequential counter below.
        self._span_prefix = (int.from_bytes(span_digest[:2], "big") | 0xA000) << 48
        self._span_counter = itertools.count(1)

    def generate_span_id(self) -> int:
        return self._span_prefix | next(self._span_counter)

    def generate_trace_id(self) -> int:
        return self._trace_id


def _attr_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_attr_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _attrs_to_kv(attrs: Any) -> list[dict[str, Any]]:
    return [{"key": str(k), "value": _attr_value(v)} for k, v in (attrs or {}).items()]


def _span_to_json(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    out: dict[str, Any] = {
        "traceId": f"{ctx.trace_id:032x}",
        "spanId": f"{ctx.span_id:016x}",
        "name": span.name,
        "kind": _OTLP_SPAN_KIND.get(span.kind, 1),
        "startTimeUnixNano": str(span.start_time),
        "endTimeUnixNano": str(span.end_time),
        "attributes": _attrs_to_kv(span.attributes),
        "status": {"code": _OTLP_STATUS_CODE.get(span.status.status_code, "STATUS_CODE_UNSET")},
    }
    if span.parent is not None:
        out["parentSpanId"] = f"{span.parent.span_id:016x}"
    return out


def build_export_request(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    """Assemble an OTLP ExportTraceServiceRequest from collected spans."""
    if not spans:
        return {"resourceSpans": []}
    resource = spans[0].resource
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attrs_to_kv(dict(resource.attributes))},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "openinference.instrumentation.demo",
                            "version": "0.1.0",
                        },
                        "spans": [_span_to_json(s) for s in spans],
                    }
                ],
            }
        ]
    }


def post_traces(ingest_url: str, payload: dict[str, Any], timeout_s: float = 15.0) -> None:
    """POST an OTLP payload to the ingest ``/v1/traces`` endpoint."""
    url = ingest_url.rstrip("/") + "/v1/traces"
    resp = httpx.post(url, json=payload, timeout=timeout_s)
    resp.raise_for_status()


def write_payload(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
