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
FLAG_ARTIFACT_INTEGRITY = "artifact_integrity"
FLAG_REQUIRED_SECTION = "required_section_missing"
# Terminal rubric split: the judge's FORM dimension is bad — the deliverable
# visibly shipped in a form other than the one explicitly requested in the
# initial input. HARD flag: it pages tier2 even when the CONTENT verdict is ok
# (a form-only miss used to reach tier2 only via sampling — a judge false
# negative on format left the run unanalyzed at production sample rates).
FLAG_TERMINAL_FORM = "terminal_form_breach"
# The deliverable was read and graded, but part of what it delivers lives in
# images nobody opened (scoring.ARTIFACT_PARTIAL). SOFT flag: it is a recorded
# LIMIT on the verdict, not a defect — an illustrated dossier is not a fault and
# must not page. It exists so "verified on the text only" survives as structured
# data instead of living solely in a sentence appended to the reasoning; two out
# of two real production runs delivered work whose value was partly in
# photographs, so this is the normal multimodal case, not an edge one.
FLAG_UNINSPECTED_MEDIA = "uninspected_media"


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
    # Raw ``agent_detective.artifact_meta`` span attribute (JSON array string)
    # landed by ingest. OUT-OF-BAND by design: payload text is forgeable by
    # document content, span attributes are not — integrity checks read ONLY
    # this field, never the payload (docs/deterministic-signals.md).
    artifact_meta: str | None = None
    # Compact JSON digest of the run's TOOL spans, derived by otel_mapper and
    # landed by ingest (migration 0007): array of {"name", "args_sha",
    # "status"} in execution order. None when the run had no TOOL spans.
    # Text on purpose — checks parse it tolerantly.
    tool_calls: str | None = None
    # Fingerprint of the tool schema the run executed under (migration 0009);
    # completes the per-run identity tuple used by version-diff views.
    tool_schema_hash: str | None = None
    # Raw ``agent_detective.contract_params`` span attribute (JSON object
    # string, migration 0011): parameters the run's input is contractually
    # bound to, declared out-of-band. The convention lane for pipelines whose
    # payloads are prose/code and give the input-side JSON diff nothing to
    # parse. Text on purpose — scoring parses it tolerantly.
    contract_params: str | None = None
    # Loop identity from ``agent_detective.attempt`` / ``.attempt_of``: which
    # pass this run was, and of which agent. Retry attempts must carry distinct
    # agent_names or reconstruction draws no edge between them, so without this
    # pair the graph cannot tell one agent that ran four times from four
    # agents — and the loop check ends up counting cycle SIZE instead of rounds.
    attempt: int | None = None
    attempt_of: str | None = None
    # Declared node kind from ``agent_detective.node_kind`` (migration 0015):
    # how the node works, said by the caller. Role is otherwise inferred from
    # the agent NAME alone, so a plan_node making zero model calls is judged
    # against a planner rubric for prose it never produced. Free text —
    # "deterministic" and "tool" are the ones scoring acts on, and an unknown
    # value from a newer SDK is carried, not rejected. None = UNDECLARED, which
    # is not the same as "llm".
    node_kind: str | None = None


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
    """Baseline statistics for one agent_name within a graph_type.

    The ``*_m2`` fields are Welford running-variance accumulators (sum of
    squared deviations from the running mean, migration 0007); the ``*_std``
    fields remain the derived view (``sqrt(m2 / (n - 1))`` for n > 1). New
    fields are appended with ``None`` defaults so existing keyword and
    positional construction stays valid.
    """

    tokens_out_mean: float | None
    tokens_out_std: float | None
    iterations_mean: float | None
    iterations_std: float | None
    sample_count: int | None
    cost_mean: float | None = None
    cost_std: float | None = None
    tokens_out_m2: float | None = None
    cost_m2: float | None = None
    iterations_m2: float | None = None


@dataclass(frozen=True)
class CheckRule:
    """One registered deterministic requirement (``check_rules`` row).

    ``agent_name`` / ``graph_type`` are None for "applies to any"; ``kind``
    is one of 'required_section' | 'sum_invariant' | 'tool_schema' (DB CHECK
    constraint); ``spec`` holds the kind-specific rule payload.
    """

    id: int
    agent_name: str | None
    graph_type: str | None
    kind: str
    spec: dict[str, Any]


@dataclass(frozen=True)
class OutputContract:
    agent_name: str | None
    agent_version_pattern: str | None
    json_schema: dict[str, Any]


@dataclass(frozen=True)
class Tier1Verdict:
    """A tier1 verdict row (upsert payload and read result)."""

    graph_id: UUID
    terminal_judge_verdict: str  # 'ok' | 'bad' | 'not_checkable' | 'error'
    terminal_judge_score: float | None
    terminal_judge_reasoning: str | None
    flags: list[str]
    flagged: bool
    sampled: bool
    # Fingerprint of the rule set + check settings the verdict was computed
    # under (signals.check_rules_fingerprint, migration 0008). Lets a later
    # reconciliation distinguish "rules changed" from "artifact/payload
    # diverged" with certainty. None on verdicts that predate stamping.
    check_rules_hash: str | None = None
    # Fingerprint of the worker's OWN judge prompts (12 hex of sha256 over the
    # sorted worker/prompts/*.md bytes, migration 0009) so calibration can be
    # sliced by judge-prompt version. The judge MODEL is not recorded — a
    # known limitation. None on verdicts that predate stamping.
    judge_prompt_hash: str | None = None
    # Terminal rubric split (migration 0010): the judge's FORM dimension —
    # {"verdict": "ok|bad|not_applicable", "requirement": <verbatim quote from
    # the initial input or None>, "observed": ..., "reasoning": ...}. The
    # verdict/score/reasoning columns above are the CONTENT dimension only.
    # None on verdicts that predate the split (legacy single-verdict rows).
    terminal_form: dict | None = None


@dataclass(frozen=True)
class NodeScoreRow:
    """Per-node scoring result persisted to agent_runs."""

    run_id: UUID
    quality_score: float | None
    score_components: dict[str, float | None]
    unscored_reason: str | None
    input_flawed: bool | None
    # What produced the number (migration 0014): the weights AFTER
    # renormalization, and the model behind the judge component. None on every
    # unscored path — nothing was blended, so there is nothing to attribute.
    score_weights: dict[str, float] | None = None
    judge_model: str | None = None


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
    downstream_cost_usd: float | None
    unscored_run_ids: list[UUID]
    evidence: dict[str, Any]
    # Judge-prompt fingerprint the blame analysis ran under (migration 0009)
    # and the model that answered it (0014) — the calibration slice key.
    judge_prompt_hash: str | None = None
    judge_model: str | None = None
    # {"priced": n, "total": m} behind downstream_cost_usd: a total over 6 of 28
    # priced runs is a lower bound, and bare it reads as the price of the run.
    cost_coverage: dict[str, Any] | None = None


@dataclass(frozen=True)
class PolicyRule:
    """One enabled ``policy_rules`` row (shadow policy gate, roadmap 2.2).

    ``predicate`` is the JSONB DSL v1 dict; ``action`` is 'warn' | 'block';
    ``shadow`` is always true in v1 — rules annotate, they never intercept.
    """

    id: int
    name: str
    predicate: dict[str, Any]
    action: str
    shadow: bool
    enabled: bool


@dataclass(frozen=True)
class PolicyDecision:
    """One rule firing on a graph. ``decision`` is 'would_block' |
    'would_warn' — the names keep the honesty requirement (shadow mode
    records what WOULD have happened; nothing was blocked)."""

    rule_name: str
    decision: str
    detail: str | None


@dataclass(frozen=True)
class BreakerState:
    """One recorded circuit-breaker decision (``breaker_state`` row).

    A RECORD of a decision, not an enforcement: Agent Detective observes and
    cannot stop anything — enforcement only happens if the integration polls
    this state.
    """

    scope_kind: str  # 'agent_name' | 'agent_version'
    scope_value: str
    state: str  # 'open' | 'closed'
    reason: str | None


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
