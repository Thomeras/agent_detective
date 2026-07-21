"""Plain data records passed across the worker's testability seams.

Everything here is immutable and dependency-free so in-memory test fakes can
construct and assert on the same shapes the SQL / Redis / MinIO implementations
use. Column and stream names mirror db/alembic/versions/0001_initial_schema.py
and build spec section 4.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

# Redis streams and consumer groups (build spec section 4.1).
STREAM_GRAPHS_COMPLETED = "ad.graphs.completed"
STREAM_GRAPHS_TIER2 = "ad.graphs.tier2"
STREAM_INCIDENTS_CREATED = "ad.incidents.created"

GROUP_TIER1 = "tier1"
GROUP_TIER2 = "tier2"
GROUP_ALERTERS = "alerters"

# Deterministic tier1 flag names persisted to tier1_verdicts.flags.
FLAG_FAILED_RUNS = "failed_runs"
FLAG_COST_OVERRUN = "cost_overrun"
FLAG_LOOP_ANOMALY = "loop_anomaly"
FLAG_SCHEMA_VIOLATION = "schema_violation"
FLAG_DEGENERATE_OUTPUT = "degenerate_output"


@dataclass(frozen=True)
class RunRecord:
    """One ``agent_runs`` row as read for scoring and blame."""

    run_id: UUID
    graph_id: UUID
    agent_name: str | None
    agent_version: str | None
    status: str  # 'ok' | 'degraded' | 'failed'
    input_inline: str | None
    input_overflow_ref: str | None
    output_inline: str | None
    output_overflow_ref: str | None
    output_bytes: int | None
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    started_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True)
class EdgeRecord:
    from_run_id: UUID
    to_run_id: UUID
    type: str


@dataclass(frozen=True)
class GraphBundle:
    """Everything tier1/tier2 need for one graph."""

    graph_id: UUID
    name: str | None
    graph_type: str | None
    total_cost_usd: float | None
    run_count: int | None
    runs: list[RunRecord]
    edges: list[EdgeRecord]


@dataclass(frozen=True)
class AgentStat:
    """Baseline statistics for one agent_name within a graph_type."""

    tokens_out_mean: float | None
    tokens_out_std: float | None
    iterations_mean: float | None
    iterations_std: float | None
    sample_count: int | None


@dataclass(frozen=True)
class OutputContract:
    agent_name: str | None
    agent_version_pattern: str | None
    json_schema: dict[str, Any]


@dataclass(frozen=True)
class Tier1Verdict:
    """A tier1 verdict row (upsert payload and read result)."""

    graph_id: UUID
    terminal_judge_verdict: str  # 'ok' | 'bad' | 'error'
    terminal_judge_score: float | None
    terminal_judge_reasoning: str | None
    flags: list[str]
    flagged: bool
    sampled: bool


@dataclass(frozen=True)
class NodeScoreRow:
    """Per-node scoring result persisted to agent_runs."""

    run_id: UUID
    quality_score: float | None
    score_components: dict[str, float | None]
    unscored_reason: str | None
    input_flawed: bool | None


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of claiming a tier2 job by dedup_key."""

    claimed: bool
    status: str | None  # existing status when not claimed


@dataclass(frozen=True)
class IncidentUpsert:
    """Result of upserting an incident row."""

    incident_id: int
    is_new: bool


@dataclass(frozen=True)
class BlameDraft:
    """A blame report's content before it is attached to an incident row."""

    report_type: str
    culprit_run_ids: list[UUID]
    propagation_path: list[UUID]
    confidence: float
    downstream_cost_usd: float
    unscored_run_ids: list[UUID]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Tier2Outcome:
    """Result of committing a tier2 run's scores, incident and blame report."""

    incident_id: int | None
    is_new: bool
    blame_report_id: int | None


@dataclass(frozen=True)
class AlertContext:
    """Denormalized incident view used to render an alert."""

    incident_id: int
    graph_id: UUID
    trigger: str
    report_type: str | None
    culprit_run_ids: list[UUID]
    confidence: float | None
    downstream_cost_usd: float | None


@dataclass(frozen=True)
class StreamMessage:
    """One consumed stream entry: its id and decoded JSON payload."""

    id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PendingEntry:
    """One XPENDING row: message id and its delivery count."""

    id: str
    delivery_count: int


@dataclass
class Tier2Message:
    """Parsed ``ad.graphs.tier2`` message (spec 4.1)."""

    graph_id: str
    trigger: str
    dedup_key: str
    tier1_verdict_ref: str | None = None
    requested_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
