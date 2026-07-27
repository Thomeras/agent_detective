"""OTLP export payload -> the ``GraphBundle`` objects tier1/tier2 consume.

This is the local-mode stand-in for what the ingest service does with a POST to
``/v1/traces``: run the payload through ``otel_mapper``, hash the mapper's
stable keys into UUIDs, and assemble one bundle per graph. What it deliberately
does NOT do is the half of ingest that only a deployment needs — raw span rows
for ClickHouse, payload overflow into an object store, quiescence-based
finalization. Local analysis holds one finished trace in memory: every payload
stays inline and every graph in the file is already complete.

Accepted input (``load_trace``): the OTLP/HTTP **JSON** encoding of
``ExportTraceServiceRequest`` — either one object, or a JSON array / JSON-lines
file of them, which is how exporters that flush in batches write to disk.
Protobuf is not accepted here, the same limitation the ingest endpoint has.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otel_mapper import (
    AgentRunCandidate,
    MappingResult,
    flatten_export_request,
    graph_id_from_str,
    map_spans,
    run_id_from_key,
)
from worker.types import EdgeRecord, GraphBundle, RunRecord


class TraceFormatError(ValueError):
    """The file is not a readable OTLP/HTTP JSON export."""


def load_trace(path: Path) -> list[dict[str, Any]]:
    """Read one OTLP/HTTP JSON file into a list of export requests.

    Tolerates the three shapes a captured trace actually arrives in: a single
    export object, a JSON array of them, and JSON-lines (one export per line,
    what a batching exporter appends).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceFormatError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        raise TraceFormatError(f"{path} is empty")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # JSON-lines: every non-blank line must parse on its own, otherwise the
        # file is simply malformed and we say so with the first line's error.
        exports: list[dict[str, Any]] = []
        for lineno, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                exports.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise TraceFormatError(
                    f"{path} is neither JSON nor JSON-lines (line {lineno}: {exc.msg})"
                ) from exc
        return exports

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    raise TraceFormatError(
        f"{path}: expected an OTLP export object or a list of them, got {type(parsed).__name__}"
    )


def _epoch(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _run_record(candidate: AgentRunCandidate) -> RunRecord:
    """One mapper candidate as the row tier1/tier2 would have read from Postgres.

    Payloads stay INLINE (no overflow ref): there is no object store locally,
    and ``resolve_payload`` returns the inline text untouched.
    """
    output = candidate.output
    return RunRecord(
        run_id=run_id_from_key(candidate.run_key),
        graph_id=graph_id_from_str(candidate.graph_id),
        agent_name=candidate.agent_name,
        agent_version=candidate.agent_version,
        status=candidate.status,
        input_inline=candidate.input,
        input_overflow_ref=None,
        output_inline=output,
        output_overflow_ref=None,
        output_bytes=len(output.encode("utf-8")) if output is not None else 0,
        cost_usd=candidate.cost_usd,
        tokens_in=candidate.tokens_in,
        tokens_out=candidate.tokens_out,
        started_at=_epoch(candidate.start_time),
        ended_at=_epoch(candidate.end_time),
        artifact_meta=candidate.artifact_meta,
        tool_calls=candidate.tool_calls,
        tool_schema_hash=candidate.tool_schema_hash,
    )


def bundles_from_mapping(result: MappingResult) -> list[GraphBundle]:
    """Group a mapping result into one ``GraphBundle`` per graph.

    Graph order, and run order within a graph, follow the mapper's own
    deterministic ordering, so two runs over the same file produce identical
    bundles — the precondition for the analysis being reproducible at all.
    """
    runs_by_graph: dict[str, list[RunRecord]] = {}
    for candidate in result.runs:
        runs_by_graph.setdefault(candidate.graph_id, []).append(_run_record(candidate))

    # The mapper never invents edge endpoints, so both run keys resolve here;
    # an edge is filed under its TARGET's graph (the mapper derives no edges
    # across graphs, so source and target agree).
    run_graph = {r.run_key: r.graph_id for r in result.runs}
    edges_by_graph: dict[str, list[EdgeRecord]] = {}
    for edge in result.edges:
        graph = run_graph.get(edge.to_run_key)
        if graph is None:
            continue
        edges_by_graph.setdefault(graph, []).append(
            EdgeRecord(
                from_run_id=run_id_from_key(edge.from_run_key),
                to_run_id=run_id_from_key(edge.to_run_key),
                type=edge.type.value,
            )
        )

    bundles: list[GraphBundle] = []
    for graph_key in sorted(runs_by_graph):
        runs = runs_by_graph[graph_key]
        costs = [r.cost_usd for r in runs if r.cost_usd is not None]
        bundles.append(
            GraphBundle(
                graph_id=graph_id_from_str(graph_key),
                # The deployed graph row has no name either — the UI shows the
                # graph_type. Naming it here would invent evidence.
                name=None,
                graph_type=result.graph_types.get(graph_key),
                total_cost_usd=sum(costs) if costs else None,
                run_count=len(runs),
                runs=runs,
                edges=edges_by_graph.get(graph_key, []),
            )
        )
    return bundles


def bundles_from_exports(
    exports: list[dict[str, Any]], *, a2a_detection: bool = False
) -> list[GraphBundle]:
    """Map a batch of OTLP export payloads into graph bundles.

    Spans from every export are flattened together before mapping, so a trace
    split across several export calls still reconstructs as one graph.
    """
    spans = [span for export in exports for span in flatten_export_request(export)]
    return bundles_from_mapping(map_spans(spans, a2a_detection=a2a_detection))
