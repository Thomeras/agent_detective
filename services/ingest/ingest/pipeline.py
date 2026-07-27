"""POST /v1/traces processing: OTLP payload -> span rows + ingest batch.

Pure orchestration over otel_mapper and the payload store; no framework or
client imports, so it is exercised directly in tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from otel_mapper import flatten_export_request, map_spans

from .config import Settings
from .spans import span_row
from .store import ObjectStore, store_payload
from .types import EdgeRow, GraphRow, IngestBatch, RunRow, SpanRow, graph_id_from_str, run_id_from_key

if TYPE_CHECKING:
    from uuid import UUID

    from .repository import Repo
    from .spans import SpanSink


async def build_batch(
    payload: dict[str, Any],
    settings: Settings,
    store: ObjectStore,
) -> tuple[list[SpanRow], IngestBatch]:
    """Turn one ExportTraceServiceRequest into raw span rows and a DB batch.

    Run and graph UUIDs are uuid5 hashes of the mapper's stable keys, so
    redelivering the same spans produces the same rows and the repository's
    ON CONFLICT handling makes it a no-op.
    """
    spans = flatten_export_request(payload)
    span_rows = [row for span in spans if (row := span_row(span)) is not None]
    batch = await batch_from_spans(spans, settings, store)
    return span_rows, batch


async def batch_from_spans(
    spans: list[dict[str, Any]],
    settings: Settings,
    store: ObjectStore,
) -> IngestBatch:
    """Map a flattened span set into the graphs/runs/edges DB batch."""
    result = map_spans(spans, a2a_detection=settings.a2a_detection)

    runs: list[RunRow] = []
    graph_bounds: dict[Any, dict[str, Any]] = {}
    for candidate in result.runs:
        graph_id = graph_id_from_str(candidate.graph_id)
        run_id = run_id_from_key(candidate.run_key)
        input_payload = await store_payload(
            store,
            settings.minio_bucket,
            graph_id,
            run_id,
            "input",
            candidate.input,
            settings.payload_inline_max_bytes,
        )
        output_payload = await store_payload(
            store,
            settings.minio_bucket,
            graph_id,
            run_id,
            "output",
            candidate.output,
            settings.payload_inline_max_bytes,
        )
        runs.append(
            RunRow(
                run_id=run_id,
                graph_id=graph_id,
                agent_name=candidate.agent_name,
                agent_version=candidate.agent_version,
                model_name=candidate.model_name,
                prompt_hash=candidate.prompt_hash,
                tool_schema_hash=candidate.tool_schema_hash,
                artifact_meta=candidate.artifact_meta,
                tool_calls=candidate.tool_calls,
                contract_params=candidate.contract_params,
                trace_id=candidate.trace_id,
                status=candidate.status,
                input_inline=input_payload.inline,
                input_overflow_ref=input_payload.overflow_ref,
                input_bytes=input_payload.nbytes,
                output_inline=output_payload.inline,
                output_overflow_ref=output_payload.overflow_ref,
                output_bytes=output_payload.nbytes,
                input_summary=input_payload.summary,
                output_summary=output_payload.summary,
                cost_usd=candidate.cost_usd,
                tokens_in=candidate.tokens_in,
                tokens_out=candidate.tokens_out,
                started_at=candidate.start_time,
                ended_at=candidate.end_time,
            )
        )
        bounds = graph_bounds.setdefault(
            graph_id,
            {
                "started_at": candidate.start_time,
                "ended_at": candidate.end_time,
                # Cohort key from the OTLP resource service.name (mapper-derived
                # per graph); NULL when the resource had no service.name.
                "graph_type": result.graph_types.get(candidate.graph_id),
            },
        )
        # Widest time bounds over the batch's runs; None-aware.
        if candidate.start_time is not None and (
            bounds["started_at"] is None or candidate.start_time < bounds["started_at"]
        ):
            bounds["started_at"] = candidate.start_time
        if candidate.end_time is not None and (
            bounds["ended_at"] is None or candidate.end_time > bounds["ended_at"]
        ):
            bounds["ended_at"] = candidate.end_time

    graphs = [
        GraphRow(
            graph_id=gid,
            started_at=b["started_at"],
            ended_at=b["ended_at"],
            graph_type=b["graph_type"],
        )
        for gid, b in sorted(graph_bounds.items(), key=lambda item: str(item[0]))
    ]
    # The mapper never invents edge endpoints: both run keys are in the run
    # list. Endpoint graphs can differ only across header-correlated traces,
    # which the mapper deliberately derives no edges from — so the target
    # run's graph is unambiguous here.
    run_graph = {r.run_key: graph_id_from_str(r.graph_id) for r in result.runs}
    edge_rows = [
        EdgeRow(
            graph_id=run_graph[edge.to_run_key],
            from_run_id=run_id_from_key(edge.from_run_key),
            to_run_id=run_id_from_key(edge.to_run_key),
            type=edge.type.value,
            detection_method=edge.detection_method,
        )
        for edge in result.edges
    ]
    return IngestBatch(graphs=graphs, runs=runs, edges=edge_rows)


class TraceRemapper:
    """Rebuild a graph from its FULL stored span set at finalization.

    Per-request mapping cannot see cross-batch structure: an edge whose
    endpoint spans arrived in different POSTs, a root span shipped in a later
    batch, a run whose identity/model spans trailed its opener. Standard OTLP
    BatchSpanProcessors flush on a timer, so multi-batch traces are the NORM
    for foreign agents, not an edge case. Before a graph is finalized (and
    analysis triggered), this re-runs the mapper over every stored span of the
    graph's trace(s) and upserts the result, refreshing run rows — the full
    set is strictly better informed than any single batch.

    Only traces that already produced a run are discoverable (the uuid5
    graph key is one-way); spans of a trace with no run yet stay invisible,
    which per-request mapping could not use either.
    """

    def __init__(
        self,
        repo: "Repo",
        span_sink: "SpanSink",
        settings: Settings,
        store: ObjectStore,
    ) -> None:
        self._repo = repo
        self._span_sink = span_sink
        self._settings = settings
        self._store = store

    async def remap(self, graph_id: "UUID") -> None:
        trace_ids = await self._repo.trace_ids_for_graph(graph_id)
        if not trace_ids:
            return
        spans = await self._span_sink.select_spans(trace_ids)
        if not spans:
            return
        batch = await batch_from_spans(spans, self._settings, self._store)
        await self._repo.upsert_batch(batch, refresh_runs=True)
