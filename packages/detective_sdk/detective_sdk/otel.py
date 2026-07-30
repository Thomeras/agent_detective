"""Bridge for systems that ALREADY emit OpenTelemetry — the three-line version
of what integrations keep hand-writing.

Pointing an existing OTLP exporter at Agent Detective sounds like a config
change, and sometimes is. In practice three things go wrong, and each one fails
*quietly* — you get an empty or misshapen graph, not an error:

1. **Protobuf.** Python's stock ``OTLPSpanExporter`` serializes protobuf; the
   receiver takes OTLP/HTTP **JSON** and answers ``400``.
2. **CHAIN, not AGENT.** Framework auto-instrumentors (e.g.
   ``openinference-instrumentation-langchain``) mark each node ``CHAIN``. A run
   opens only for ``AGENT`` spans, so nothing becomes a node at all.
3. **No edges.** Even promoted, sibling node spans under one root have no
   parent/child relation *between them*, so the graph has nodes and no edges —
   and blame has no path to walk.

``harnesses/crewai_game_builder/run_instrumented.py`` hand-wrote ~60 lines for
exactly this and called it "harness glue … recorded as a product gap". This
module is that gap closed::

    from detective_sdk.otel import collect

    collect(endpoint="http://127.0.0.1:8900",
            promote=lambda s: s.name if s.name in NODES else None,
            chain=True)

**Collecting, not streaming.** Spans buffer until the run ends and go out as one
``ExportTraceServiceRequest``. Promotion and chaining need to see the whole run:
you cannot re-parent node #4 to node #3 in a batch that has not met node #3 yet.

**Importable without OpenTelemetry.** Everything here reads duck-typed span
objects; only :func:`collect` touches the OTel API, and it imports it lazily.
The package core stays dependency-free — instrumentation lives in the agent's
process and must not drag a judge or a database in with it.
"""

from __future__ import annotations

import atexit
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional, Sequence

from .tracing import _encode, _hex, _span_id, deliver

logger = logging.getLogger(__name__)

AGENT_KIND_ATTRIBUTE = "openinference.span.kind"
AGENT_NAME_ATTRIBUTE = "gen_ai.agent.name"

# A promoter is either a callable span -> agent name (or None to leave it), or a
# plain collection of span names to promote under their own name.
Promoter = Callable[["SpanRecord"], Optional[str]] | Iterable[str] | None


@dataclass
class SpanRecord:
    """One span, normalised away from any framework's object model.

    Hex ids and nanosecond ints, i.e. already the wire shape — so the transforms
    below are plain data manipulation and testable without an OTel install.
    """

    trace_id: str
    span_id: str
    name: str
    start_ns: int
    end_ns: int
    parent_id: str = ""
    kind: int = 1
    attributes: dict = field(default_factory=dict)
    error: bool = False
    resource: dict = field(default_factory=dict)
    scope: str = "detective_sdk.otel"

    @property
    def is_agent(self) -> bool:
        return str(self.attributes.get(AGENT_KIND_ATTRIBUTE, "")).upper() == "AGENT"


def normalize(span: Any) -> Optional[SpanRecord]:
    """OTel ``ReadableSpan`` (or anything shaped like one) -> :class:`SpanRecord`.

    Deliberately tolerant: read through ``getattr`` so a version bump in the
    OTel SDK degrades one field instead of losing the whole run.
    """
    try:
        ctx = span.get_span_context()
        parent = getattr(span, "parent", None)
        status = getattr(span, "status", None)
        code = getattr(getattr(status, "status_code", None), "name", "")
        resource = getattr(span, "resource", None)
        scope = getattr(span, "instrumentation_scope", None)
        return SpanRecord(
            trace_id=format(ctx.trace_id, "032x"),
            span_id=format(ctx.span_id, "016x"),
            parent_id=format(parent.span_id, "016x") if parent is not None else "",
            name=str(getattr(span, "name", "") or ""),
            kind=int(getattr(getattr(span, "kind", None), "value", 1) or 1),
            start_ns=int(getattr(span, "start_time", 0) or 0),
            end_ns=int(getattr(span, "end_time", 0) or 0),
            attributes=dict(getattr(span, "attributes", None) or {}),
            error=str(code).upper() == "ERROR",
            resource=dict(getattr(resource, "attributes", None) or {}),
            scope=str(getattr(scope, "name", "") or "detective_sdk.otel"),
        )
    except Exception:  # noqa: BLE001 - a malformed span must not lose the run
        logger.debug("detective_sdk.otel: could not normalize a span", exc_info=True)
        return None


def _promoter(promote: Promoter) -> Callable[[SpanRecord], Optional[str]]:
    if promote is None:
        return lambda record: None
    if callable(promote):
        return promote
    names = {str(n) for n in promote}
    return lambda record: record.name if record.name in names else None


def promote_agents(records: Sequence[SpanRecord], promote: Promoter) -> list[SpanRecord]:
    """Mark chosen spans as agent runs.

    Without ``openinference.span.kind=AGENT`` a span is not a node — it is not a
    *bad* node, it simply does not exist in the graph. Spans already marked AGENT
    are left alone, so this composes with partially-correct instrumentation.
    """
    decide = _promoter(promote)
    out: list[SpanRecord] = []
    for record in records:
        name = None
        if not record.is_agent:
            try:
                name = decide(record)
            except Exception:  # noqa: BLE001 - a user predicate must not break export
                logger.debug("detective_sdk.otel: promote() raised", exc_info=True)
        if name:
            attributes = {
                **record.attributes,
                AGENT_KIND_ATTRIBUTE: "AGENT",
                AGENT_NAME_ATTRIBUTE: str(name),
            }
            out.append(replace(record, attributes=attributes))
        else:
            out.append(record)
    return out


def chain_agents(records: Sequence[SpanRecord]) -> list[SpanRecord]:
    """Re-parent agent spans into execution order, so edges exist.

    Only AGENT spans move. Their children (the LLM/tool spans that carry tokens
    and cost) keep pointing at their original parent, which is what makes usage
    roll up into the right node.

    The first agent span keeps whatever parent it had — that is usually the
    framework's own root, and overwriting it would orphan the graph.
    """
    agents = sorted(
        (r for r in records if r.is_agent), key=lambda r: (r.start_ns, r.span_id)
    )
    if len(agents) < 2:
        return list(records)
    reparented = {}
    for previous, current in zip(agents, agents[1:]):
        reparented[current.span_id] = previous.span_id
    return [
        replace(r, parent_id=reparented[r.span_id]) if r.span_id in reparented else r
        for r in records
    ]


def root_span(
    records: Sequence[SpanRecord],
    *,
    name: str,
    task: Any,
    parent_span_id: str | None = None,
) -> Optional[SpanRecord]:
    """Synthesise the run root that carries the ORIGINAL request.

    The terminal judge cites this as provenance; without it there is nothing to
    check the finished deliverable against, and the verdict degrades to
    "not checkable". Frameworks rarely record the user's ask as a span, so when
    a ``task`` is supplied we add one.
    """
    agents = sorted(
        (r for r in records if r.is_agent), key=lambda r: (r.start_ns, r.span_id)
    )
    if not agents:
        return None
    first = agents[0]
    return SpanRecord(
        trace_id=first.trace_id,
        span_id=_hex(8),
        name=name,
        start_ns=min(r.start_ns for r in agents),
        end_ns=max(r.end_ns for r in agents),
        parent_id=_span_id(parent_span_id),
        attributes={
            AGENT_KIND_ATTRIBUTE: "AGENT",
            AGENT_NAME_ATTRIBUTE: name,
            "input.value": _encode(task),
            "output.value": "",
        },
        resource=first.resource,
        scope=first.scope,
    )


def _attr_value(value: Any) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_attr_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _attrs(mapping: dict) -> list[dict]:
    return [{"key": str(k), "value": _attr_value(v)} for k, v in (mapping or {}).items()]


def to_export_request(records: Sequence[SpanRecord], *, service: str | None = None) -> dict:
    """Records -> one OTLP/HTTP JSON ``ExportTraceServiceRequest``."""
    spans = []
    resource: dict = {}
    scope = "detective_sdk.otel"
    for record in records:
        resource = resource or record.resource
        scope = record.scope or scope
        span = {
            "traceId": record.trace_id,
            "spanId": record.span_id,
            "parentSpanId": record.parent_id,
            "name": record.name,
            "kind": record.kind,
            "startTimeUnixNano": str(record.start_ns),
            "endTimeUnixNano": str(record.end_ns),
            "attributes": _attrs(record.attributes),
            "status": {"code": 2 if record.error else 1},
        }
        spans.append(span)
    if service:
        resource = {**resource, "service.name": service}
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attrs(resource)},
                "scopeSpans": [{"scope": {"name": scope}, "spans": spans}],
            }
        ]
    }


class TraceCollector:
    """Buffers a run's spans, fixes conventions, exports once at the end.

    Duck-types OpenTelemetry's ``SpanExporter`` (``export`` / ``shutdown``)
    without importing it, so this module stays usable — and testable — with no
    OTel installed.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        trace_file: str | None = None,
        promote: Promoter = None,
        chain: bool = False,
        service: str | None = None,
        root: str = "run",
        task: Any = None,
        parent_span_id: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._trace_file = trace_file
        self._promote = promote
        self._chain = chain
        self._service = service
        self._root = root
        self._task = task
        self._parent_span_id = parent_span_id
        self._records: list[SpanRecord] = []
        self._exported = False

    # -- the SpanExporter shape --------------------------------------------- #

    def export(self, spans: Iterable[Any]) -> Any:
        for span in spans:
            record = normalize(span)
            if record is not None:
                self._records.append(record)
        return _export_success()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True

    def shutdown(self) -> None:
        self.flush()

    # -- the interesting part ------------------------------------------------- #

    def build_payload(self) -> dict:
        records = promote_agents(self._records, self._promote)
        if self._chain:
            records = chain_agents(records)
        if self._task is not None:
            root = root_span(
                records,
                name=self._root,
                task=self._task,
                parent_span_id=self._parent_span_id,
            )
            if root is not None:
                # The first agent span hangs off the synthesised root; the rest
                # already chain behind it.
                agents = sorted(
                    (r for r in records if r.is_agent),
                    key=lambda r: (r.start_ns, r.span_id),
                )
                first_id = agents[0].span_id if agents else None
                records = [
                    replace(r, parent_id=root.span_id) if r.span_id == first_id else r
                    for r in records
                ]
                records = [root] + list(records)
        return to_export_request(records, service=self._service)

    def flush(self) -> None:
        """Send what was collected. Idempotent; never raises."""
        if self._exported or not self._records:
            return
        self._exported = True
        try:
            payload = self.build_payload()
        except Exception:  # noqa: BLE001
            logger.debug("detective_sdk.otel: build_payload failed", exc_info=True)
            return
        deliver(payload, endpoint=self._endpoint, trace_file=self._trace_file)


def _export_success() -> Any:
    """``SpanExportResult.SUCCESS`` when OTel is around, else a truthy stand-in."""
    try:
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS
    except Exception:  # noqa: BLE001
        return True


def collect(
    *,
    endpoint: str | None = None,
    trace_file: str | None = None,
    promote: Promoter = None,
    chain: bool = False,
    service: str | None = None,
    root: str = "run",
    task: Any = None,
    parent_span_id: str | None = None,
    provider: Any = None,
    at_exit: bool = True,
) -> TraceCollector:
    """Attach a collector to the active tracer provider and return it.

    ``promote`` takes a callable (``span -> agent name or None``) or a plain list
    of span names. ``chain=True`` re-parents the promoted spans into execution
    order so the pipeline gets edges. ``task`` adds the run root that carries the
    user's original request.

    Registered with a *simple* processor, not a batching one: this collector
    buffers internally anyway, and a batcher would only add a way to lose the
    tail of a run at exit. ``at_exit`` flushes on interpreter shutdown, so a
    script that never calls ``provider.shutdown()`` still exports.
    """
    collector = TraceCollector(
        endpoint=endpoint,
        trace_file=trace_file,
        promote=promote,
        chain=chain,
        service=service,
        root=root,
        task=task,
        parent_span_id=parent_span_id,
    )
    if provider is None:
        from opentelemetry import trace as _trace

        provider = _trace.get_tracer_provider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider.add_span_processor(SimpleSpanProcessor(collector))
    if at_exit:
        atexit.register(collector.flush)
    return collector


__all__ = [
    "collect",
    "TraceCollector",
    "SpanRecord",
    "normalize",
    "promote_agents",
    "chain_agents",
    "root_span",
    "to_export_request",
    "AGENT_KIND_ATTRIBUTE",
    "AGENT_NAME_ATTRIBUTE",
]
