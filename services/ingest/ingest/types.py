"""Plain data records passed across ingest's testability seams.

The processing pipeline builds these from otel_mapper candidates; the
repository / span sink / object store / stream publisher seams consume them.
Everything here is immutable and dependency-free so test fakes can construct
and assert on the same shapes the SQL/HTTP implementations use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

# Re-exported: the uuid5 derivation moved next to the keys it hashes
# (otel_mapper.ids) so ingest and the local-mode CLI cannot drift into
# assigning different ids to the same span.
from otel_mapper import graph_id_from_str, run_id_from_key

__all__ = [
    "STREAM_GRAPHS_COMPLETED",
    "SUMMARY_CHARS",
    "EdgeRow",
    "GraphActivity",
    "GraphRow",
    "IngestBatch",
    "FinalizeResult",
    "RunRow",
    "SpanRow",
    "StoredPayload",
    "graph_id_from_str",
    "run_id_from_key",
]

# Redis stream the finalizer publishes to (build spec section 4.1).
STREAM_GRAPHS_COMPLETED = "ad.graphs.completed"

# Length of the derived input/output summaries kept for UI display.
SUMMARY_CHARS = 500


@dataclass(frozen=True)
class SpanRow:
    """One raw span row for the ClickHouse ``otel_spans`` table."""

    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: str
    start_time: datetime
    end_time: datetime
    attributes: str  # raw JSON attributes payload
    status_code: str
    # Flattened OTLP resource attributes (JSON object). Stored so the
    # finalization re-map can rebuild the exact map_spans input — without it,
    # re-mapped runs would lose resource-derived identity (agent name,
    # service.name graph_type, model).
    resource_attributes: str = "{}"


@dataclass(frozen=True)
class StoredPayload:
    """Result of routing one input/output payload (inline vs overflow)."""

    inline: str | None
    overflow_ref: str | None
    nbytes: int
    summary: str | None


@dataclass(frozen=True)
class GraphRow:
    graph_id: UUID
    started_at: datetime | None
    ended_at: datetime | None
    # Cohort key (execution_graphs.graph_type): the OTLP resource service.name
    # of the graph's runs, e.g. "generative-simon". None when the resource
    # carried no service.name.
    graph_type: str | None = None


@dataclass(frozen=True)
class RunRow:
    """One ``agent_runs`` row. Scoring columns are left to the worker (M4)."""

    run_id: UUID
    graph_id: UUID
    agent_name: str | None
    agent_version: str | None
    model_name: str | None
    prompt_hash: str | None
    tool_schema_hash: str | None  # agent_detective.tool_schema_hash identity attribute
    artifact_meta: str | None  # raw agent_detective.artifact_meta opener-span attribute
    tool_calls: str | None  # compact JSON digest of the run's TOOL spans (mapper-derived)
    contract_params: str | None  # raw agent_detective.contract_params opener-span attribute
    # agent_detective.attempt / .attempt_of: which pass this run was, and of
    # which agent. Attempts need distinct agent names or reconstruction draws
    # no edge between them, so without this pair the loop check cannot tell
    # rounds from cycle size.
    attempt: int | None
    attempt_of: str | None
    trace_id: str
    status: str  # 'ok' | 'failed' (schema also allows 'degraded', set downstream)
    input_inline: str | None
    input_overflow_ref: str | None
    input_bytes: int | None
    output_inline: str | None
    output_overflow_ref: str | None
    output_bytes: int | None
    input_summary: str | None
    output_summary: str | None
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    started_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True)
class EdgeRow:
    graph_id: UUID
    from_run_id: UUID
    to_run_id: UUID
    type: str  # 'SPAWN' | 'A2A_MESSAGE' | 'TOOL_DELEGATION'
    detection_method: str


@dataclass(frozen=True)
class IngestBatch:
    """Everything one POST /v1/traces writes to Postgres, in one transaction."""

    graphs: list[GraphRow] = field(default_factory=list)
    runs: list[RunRow] = field(default_factory=list)
    edges: list[EdgeRow] = field(default_factory=list)

    @property
    def graph_ids(self) -> set[UUID]:
        return {g.graph_id for g in self.graphs}


@dataclass(frozen=True)
class GraphActivity:
    """Finalizer view of one active graph.

    ``last_activity`` is the DB fallback for last-seen: the max of run
    start/end timestamps (None when the graph has no runs yet). ``created_at``
    is the graph row creation time, the ultimate fallback. ``root_ended`` is
    True when a run with no incoming edge already has an end timestamp.
    """

    graph_id: UUID
    last_activity: datetime | None
    created_at: datetime | None
    root_ended: bool


@dataclass(frozen=True)
class FinalizeResult:
    graph_id: UUID
    finalized_at: datetime
    run_count: int
