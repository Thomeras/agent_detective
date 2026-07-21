"""Entry point that runs the synthetic pipeline and exports its OTLP payload."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .config import Settings
from .exporter import build_export_request, post_traces, write_payload
from .llm_client import LLMClient
from .scenario import build_agent_specs, build_spans, deterministic_base_nanos

logger = logging.getLogger(__name__)


def _resolve_graph_id(settings: Settings) -> str:
    if settings.graph_id:
        return settings.graph_id
    if settings.deterministic:
        return "demo-graph-deterministic"
    return f"demo-{uuid.uuid4().hex[:12]}"


def build_payload_only(settings: Settings) -> dict[str, Any]:
    """Build the OTLP ExportTraceServiceRequest without POSTing it.

    Useful for tests, fixture generation, and cross-checking against
    ``otel_mapper``.
    """
    graph_id = _resolve_graph_id(settings)
    llm = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_timeout_s)
    base_nanos = (
        deterministic_base_nanos() if settings.deterministic else time.time_ns()
    )
    specs = build_agent_specs(settings.scraper_hallucinate)
    spans = build_spans(
        specs,
        graph_id,
        llm,
        deterministic=settings.deterministic,
        base_nanos=base_nanos,
    )
    return build_export_request(spans)


def build_and_run(settings: Settings) -> dict[str, Any]:
    """Run the pipeline and export its traces per the settings.

    Returns the OTLP payload. Depending on settings it is written to a file,
    POSTed to the ingest endpoint, or neither (dry run).
    """
    graph_id = _resolve_graph_id(settings)
    payload = build_payload_only(settings)
    span_count = sum(
        len(ss.get("spans", []))
        for rs in payload.get("resourceSpans", [])
        for ss in rs.get("scopeSpans", [])
    )

    mode = "hallucinate" if settings.scraper_hallucinate else "clean"
    logger.info("built %d spans for graph %s (mode=%s)", span_count, graph_id, mode)

    if settings.capture_file:
        write_payload(settings.capture_file, payload)
        logger.info("wrote OTLP payload to %s", settings.capture_file)
        return payload

    if not settings.ingest_url:
        logger.info("dry run: no ingest URL, payload not sent")
        return payload

    post_traces(settings.ingest_url, payload)
    logger.info("posted OTLP payload to %s/v1/traces", settings.ingest_url.rstrip("/"))
    return payload
