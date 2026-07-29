"""A real OpenTelemetry SDK pipeline writing OTLP/HTTP JSON to a file.

Why not ``detective_sdk``: the whole point of this corpus is to exercise the
mapper against spans it did NOT produce. ``detective_sdk`` hand-builds the OTLP
payload dict, so a trace from it can only ever contain the shapes that module
knows how to write — which makes it useless for finding mapper bugs. Here the
spans come out of the stock ``opentelemetry-sdk``: real ``SpanContext`` ids,
real parent links through context propagation, a real ``BatchSpanProcessor``,
resource attributes attached the way the SDK attaches them, and (when an
auto-instrumentor is active) real LLM child spans under each agent span.

The exporter serializes to the OTLP/HTTP **JSON** encoding rather than
protobuf, because that is what ``detective analyze`` reads. Everything above
the exporter is stock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


def _attr_value(value: Any) -> dict:
    """One attribute value in the OTLP JSON encoding.

    The SDK hands back native Python types; OTLP wants a tagged union. Numbers
    are kept as numbers rather than stringified — the mapper reads
    ``gen_ai.usage.*`` numerically, and a trace that stringifies them is a
    different trace from the one a real collector would deliver.
    """
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_attr_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _attrs(mapping: Any) -> list[dict]:
    return [{"key": k, "value": _attr_value(v)} for k, v in (mapping or {}).items()]


def _span_to_otlp(span: ReadableSpan) -> dict:
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "traceId": format(ctx.trace_id, "032x"),
        "spanId": format(ctx.span_id, "016x"),
        "parentSpanId": format(parent.span_id, "016x") if parent else "",
        "name": span.name,
        "kind": int(span.kind.value) if span.kind is not None else 1,
        "startTimeUnixNano": str(span.start_time),
        "endTimeUnixNano": str(span.end_time),
        "attributes": _attrs(span.attributes),
        # status_code 0=UNSET 1=OK 2=ERROR in the OTLP wire encoding, which is
        # also what the SDK's StatusCode values map onto.
        "status": {"code": int(span.status.status_code.value)} if span.status else {},
    }


class JsonFileSpanExporter(SpanExporter):
    """Accumulates spans and writes one OTLP export object on ``flush``.

    Batched exports arrive in several calls; the mapper is happy with either a
    single object or a list, but a single object keeps the recorded artifact
    easy to read by hand, which matters for a corpus people are meant to
    inspect.
    """

    def __init__(self, path: str | Path, service_name: str) -> None:
        self._path = Path(path)
        self._service_name = service_name
        self._spans: list[dict] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._spans.extend(_span_to_otlp(s) for s in spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.flush()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def flush(self) -> None:
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": _attrs({"service.name": self._service_name})},
                    "scopeSpans": [
                        {"scope": {"name": "agent-detective-corpus"}, "spans": self._spans}
                    ],
                }
            ]
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def build_tracer_provider(trace_file: str | Path, service_name: str):
    """Stock ``TracerProvider`` + a simple processor feeding the JSON exporter.

    SimpleSpanProcessor, not Batch: a recorder is a short-lived process and a
    dropped batch would silently produce a corpus entry with holes in it. The
    realism this corpus needs is in the span SHAPES, not in the export timing.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = JsonFileSpanExporter(trace_file, service_name)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter
