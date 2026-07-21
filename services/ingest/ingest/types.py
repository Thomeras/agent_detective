"""Plain data records passed across ingest's testability seams.

The processing pipeline builds these from otel_mapper candidates; the
repository / span sink / object store / stream publisher seams consume them.
Everything here is immutable and dependency-free so test fakes can construct
and assert on the same shapes the SQL/HTTP implementations use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

# Redis stream the finalizer publishes to (build spec section 4.1).
STREAM_GRAPHS_COMPLETED = "ad.graphs.completed"

# Length of the derived input/output summaries kept for UI display.
SUMMARY_CHARS = 500


def run_id_from_key(run_key: str) -> UUID:
    """Deterministic ``agent_runs.run_id`` for an otel_mapper ``run_key``.

    uuid5 makes redelivery of the same spans upsert idempotently.
    """
    return uuid5(NAMESPACE_URL, run_key)


def graph_id_from_str(graph_id: str) -> UUID:
    """Deterministic ``execution_graphs.graph_id`` for a mapper graph id."""
    return uuid5(NAMESPACE_URL, graph_id)


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


@dataclass(frozen=True)
class RunRow:
    """One ``agent_runs`` row. Scoring columns are left to the worker (M4)."""

    run_id: UUID
    graph_id: UUID
    agent_name: str | None
    agent_version: str | None
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
